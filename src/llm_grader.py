# src/llm_grader.py
from __future__ import annotations
import json
import hashlib
from typing import Any, Dict, List

import nbformat
from openai import OpenAI


# ---------- helpers ----------

def _clip(s: str, max_chars: int) -> str:
    if s is None:
        return ""
    return s if len(s) <= max_chars else (s[:max_chars] + " …[truncated]")

def _sha16(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]

def _rubric_slice(rubric: Dict[str, Any], section_id: str) -> Dict[str, Any]:
    for s in rubric.get("sections", []):
        if s.get("id") == section_id or s.get("title") == section_id:
            return s
    # fallback if section not in rubric
    return {"id": section_id, "title": section_id, "points": 0.0, "criteria": []}


# ---------- public: context builder ----------

def build_section_context(
    nb: nbformat.NotebookNode,
    span: Dict[str, Any],
    html: str | None = None,
) -> Dict[str, Any]:
    """
    Create a compact, LLM-friendly snapshot of the student's work for a given section:
      - markdown text
      - code cells' source
      - textual outputs (stdout and text/plain results)

    Images are not shipped; if you later want, you can add HTML heuristics for captions.
    """
    md_parts: List[str] = []
    code_parts: List[str] = []
    out_texts: List[str] = []

    cell_idxs = span.get("cell_idxs", [])
    for idx in cell_idxs:
        if idx < 0 or idx >= len(nb.get("cells", [])):
            continue
        cell = nb["cells"][idx]
        ctype = cell.get("cell_type")
        if ctype == "markdown":
            md_parts.append(cell.get("source", "") or "")
        elif ctype == "code":
            code_parts.append(cell.get("source", "") or "")
            for out in cell.get("outputs") or []:
                ot = out.get("output_type")
                if ot == "stream":
                    out_texts.append(out.get("text", "") or "")
                elif ot in ("display_data", "execute_result"):
                    data = out.get("data") or {}
                    txt = data.get("text/plain")
                    if isinstance(txt, list):
                        txt = "".join(txt)
                    if txt:
                        out_texts.append(str(txt))

    md = _clip("\n\n".join(md_parts), 4000)
    code = _clip("\n\n".join(code_parts), 4000)
    outputs = _clip("\n\n".join(out_texts), 6000)

    return {
        "title": span.get("title", ""),
        "cell_range": [span.get("start", 0), span.get("end", 0)],
        "hash": _sha16(md + code + outputs),
        "markdown": md,
        "code": code,
        "outputs": outputs,
        # Not sending HTML by default to keep prompts small; add if you need figure captions later.
        "html_hint": "",
    }


# ---------- model I/O schema ----------

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "section_id": {"type": "string"},
        "rubric_title": {"type": "string"},
        "total_points": {"type": "number"},
        "earned_points": {"type": "number"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion_id": {"type": "string"},
                    "label": {"type": "string"},
                    "max_points": {"type": "number"},
                    "score": {"type": "number"},
                    "rationale": {"type": "string"},
                    "improvement_tip": {"type": "string"},
                },
                "required": ["criterion_id", "label", "max_points", "score", "rationale"],
            },
        },
        "overall_comment": {"type": "string"},
    },
    "required": ["section_id", "criteria", "earned_points", "total_points"],
}


# ---------- public: LLM grading ----------

def grade_section_llm(
    client: OpenAI,
    model: str,
    rubric: Dict[str, Any],
    section_id: str,
    section_ctx: Dict[str, Any],
    temperature: float = 0.0,
) -> Dict[str, Any]:
    """
    Grade a single section via LLM. Returns:
      {
        "section_id": str,
        "rubric_title": str,
        "total_points": float,
        "earned_points": float,
        "criteria": [
            {"criterion_id","label","max","score","rationale","improvement_tip"}
        ],
        "overall_comment": str
      }
    """
    rsec = _rubric_slice(rubric, section_id)

    # Normalize criteria: treat any type as LLM-gradeable
    raw_criteria = []
    for c in rsec.get("criteria", []):
        raw_criteria.append({
            "criterion_id": str(c.get("id", "")),
            "label": str(c.get("criterion", "")),
            "max_points": float(c.get("max", 0.0) or 0.0),
            "args": c.get("args", {}),  # optional guidance
        })

    # Total points: prefer section.points else sum of criteria max
    total_max = float(rsec.get("points", 0.0) or sum(c["max_points"] for c in raw_criteria))

    system_msg = (
        "You are a strict teaching assistant for a university analytics course. "
        "Grade ONLY the provided section of the student's notebook, according to the rubric. "
        "Be precise and concise. Award partial credit where evidence supports it. "
        "Never exceed any per-criterion max nor the section total. "
        "Return ONLY valid JSON that conforms to the provided schema."
    )

    user_payload = {
        "section_id": section_id,
        "rubric": {
            "title": rsec.get("title", ""),
            "total_points": total_max,
            "criteria": raw_criteria,
        },
        "student_section": section_ctx,
        "instructions": (
            "For each criterion:\n"
            "- Score 0..max_points, using partial credit when appropriate.\n"
            "- Base scores strictly on evidence in the section (markdown/code/outputs).\n"
            "- Provide a short rationale and, if helpful, an improvement tip."
        ),
        "json_schema": JSON_SCHEMA,
    }

    # Call the model with JSON response enforced
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": json.dumps(user_payload)},
        ],
    )

    raw = resp.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except Exception:
        # Fallback: empty but well-formed
        parsed = {
            "section_id": section_id,
            "rubric_title": rsec.get("title", ""),
            "total_points": total_max,
            "earned_points": 0.0,
            "criteria": [],
            "overall_comment": "Model returned invalid JSON.",
        }

    # Sanitize & clamp outputs
    cleaned_criteria: List[Dict[str, Any]] = []
    earned_sum = 0.0

    # Index for max lookup
    max_by_id = {c["criterion_id"]: c["max_points"] for c in raw_criteria}

    for c in parsed.get("criteria", []):
        cid = str(c.get("criterion_id", ""))
        label = str(c.get("label", ""))
        max_points = float(max_by_id.get(cid, 0.0))
        score = float(c.get("score", 0.0))
        # clamp 0..max
        score = max(0.0, min(score, max_points))

        cleaned_criteria.append({
            "criterion_id": cid,
            "label": label,
            "max": max_points,
            "score": score,
            "rationale": str(c.get("rationale", "")),
            "improvement_tip": str(c.get("improvement_tip", "")),
        })
        earned_sum += score

    # Clamp to section total
    earned_sum = max(0.0, min(float(earned_sum), float(total_max)))

    safe_section_id = str(parsed.get("section_id") or section_id or "Unknown")

    return {
        "section_id": safe_section_id,
        "rubric_title": rsec.get("title", ""),
        "total_points": float(total_max),
        "earned_points": float(earned_sum),
        "criteria": cleaned_criteria,
        "overall_comment": str(parsed.get("overall_comment", "")),
    }