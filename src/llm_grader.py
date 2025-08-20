# src/llm_grader.py
from __future__ import annotations
import hashlib, json, time
from typing import Any, Dict, List, Tuple

import nbformat

# If you're using OpenAI:
# pip install openai>=1.40
from openai import OpenAI

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
                    "improvement_tip": {"type": "string"}
                },
                "required": ["criterion_id","label","max_points","score","rationale"]
            }
        },
        "overall_comment": {"type": "string"}
    },
    "required": ["section_id","criteria","earned_points","total_points"]
}

def _hash_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

def build_section_context(nb: nbformat.NotebookNode, span: Dict[str, Any], html: str | None) -> Dict[str, Any]:
    """Extract a compact, LLM-friendly view of a section: markdown, code, and textual outputs.
       We avoid shipping raw images; instead we include their captions/alt text if present."""
    md_parts, code_parts, out_texts = [], [], []
    for idx in span["cell_idxs"]:
        cell = nb["cells"][idx]
        if cell["cell_type"] == "markdown":
            md_parts.append(cell.get("source",""))
        elif cell["cell_type"] == "code":
            code_parts.append(cell.get("source",""))
            # gather textual outputs
            for out in cell.get("outputs") or []:
                if out.get("output_type") == "stream":
                    out_texts.append(out.get("text",""))
                elif out.get("output_type") in ("display_data", "execute_result"):
                    # prefer text/plain if present
                    text = (out.get("data") or {}).get("text/plain")
                    if isinstance(text, list): text = "".join(text)
                    if text:
                        out_texts.append(str(text))
    # truncate super long bits (token hygiene)
    def clip(s: str, max_chars=4000):
        return (s[:max_chars] + " …[truncated]") if len(s) > max_chars else s

    return {
        "title": span.get("title",""),
        "markdown": clip("\n\n".join(md_parts), 4000),
        "code": clip("\n\n".join(code_parts), 4000),
        "outputs": clip("\n\n".join(out_texts), 6000),
        "html_hint": "" if not html else "",  # we’re not shipping HTML by default; can be enabled if needed
    }

def _rubric_slice(rubric: Dict[str, Any], section_id: str) -> Dict[str, Any]:
    for s in rubric["sections"]:
        if s["id"] == section_id:
            return s
    return {"id": section_id, "title": section_id, "points": 0, "criteria": []}

def grade_section_llm(
    client: OpenAI,
    model: str,
    rubric: Dict[str, Any],
    section_id: str,
    section_ctx: Dict[str, Any],
    temperature: float = 0.0,
) -> Dict[str, Any]:
    rsec = _rubric_slice(rubric, section_id)
    total_max = float(rsec.get("points", 0) or sum(c.get("max",0) for c in rsec["criteria"]))
    criteria = []
    for c in rsec["criteria"]:
        if c.get("type","llm_grade") != "llm_grade":
            # treat all types as llm_grade now
            pass
        criteria.append({
            "criterion_id": c["id"],
            "label": c.get("criterion",""),
            "max_points": float(c.get("max", 0)),
            "args": c.get("args", {}),
        })

    system = (
        "You are a strict TA for a university analytics course.\n"
        "Grade the student's work for the given section ONLY.\n"
        "Use the rubric exactly. Do not exceed max points per criterion or total.\n"
        "Be concise, specific, and actionable in feedback.\n"
        "Return ONLY valid JSON matching the provided schema."
    )

    user = {
        "section_id": section_id,
        "rubric": {
            "title": rsec.get("title",""),
            "total_points": total_max,
            "criteria": criteria
        },
        "student_section": section_ctx,
        "instructions": (
            "Score each criterion 0..max_points. "
            "If evidence is weak, award partial credit with rationale. "
            "If the output is not present, score 0 and explain."
        ),
        "json_schema": JSON_SCHEMA,
    }

    # Use a response_format to force JSON when available
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[
            {"role":"system","content":system},
            {"role":"user","content":json.dumps(user)}
        ],
    )

    raw = resp.choices[0].message.content
    try:
        data = json.loads(raw)
    except Exception:
        data = {"section_id": section_id, "criteria": [], "earned_points": 0, "total_points": total_max,
                "overall_comment": "Invalid JSON from model."}

    # Clamp scores & compute totals just in case
    earned = 0.0
    cleaned = []
    for c in data.get("criteria", []):
        maxp = next((x["max_points"] for x in criteria if x["criterion_id"] == c.get("criterion_id")), 0.0)
        s = float(c.get("score", 0.0))
        s = max(0.0, min(s, float(maxp)))
        earned += s
        cleaned.append({
            "criterion_id": c.get("criterion_id",""),
            "label": c.get("label",""),
            "max": float(maxp),
            "score": s,
            "rationale": c.get("rationale",""),
            "improvement_tip": c.get("improvement_tip",""),
        })

    earned = min(earned, float(total_max))
    return {
        "section_id": section_id,
        "rubric_title": rsec.get("title",""),
        "total_points": float(total_max),
        "earned_points": float(earned),
        "criteria": cleaned,
        "overall_comment": data.get("overall_comment",""),
    }