# src/rubric_schema.py
import json
import pandas as pd

# Legacy deterministic types still accepted (you may ignore them in LLM-only mode)
_ALLOWED_TYPES = {
    "columns","row_count","stat_range","unique_count",
    "null_rate","table_shape","figure_exists",
    "llm_feedback","llm_grade"
}

_ALIASES_TO_LLM_GRADE = {"llm_grader","llm","llmgrade","freeform","feedback"}

def _normalize_type(t: str) -> str:
    if not t:
        return "llm_grade"
    t = str(t).strip().lower()
    if t in _ALIASES_TO_LLM_GRADE:
        return "llm_grade"
    if t == "llm_feedback":
        # unify to llm_grade so downstream only handles one
        return "llm_grade"
    return t

def _normalize_inplace(rubric: dict):
    for s in rubric.get("sections", []):
        for c in s.get("criteria", []):
            c["type"] = _normalize_type(c.get("type",""))

def _validate(rubric: dict):
    assert "sections" in rubric and isinstance(rubric["sections"], list), "Rubric missing `sections`"
    for s in rubric["sections"]:
        assert "id" in s and "criteria" in s, f"Section invalid: {s}"
        for c in s["criteria"]:
            ctype = c.get("type","")
            if ctype not in _ALLOWED_TYPES:
                raise ValueError(
                    f"Criterion type '{ctype}' not supported. Allowed: {sorted(_ALLOWED_TYPES)}"
                )

def load_rubric_json(file) -> dict:
    data = json.load(file)
    _normalize_inplace(data)
    _validate(data)
    return data

def load_rubric_excel(file) -> dict:
    xls = pd.ExcelFile(file)
    sections = pd.read_excel(xls, "Sections")
    criteria = pd.read_excel(xls, "Criteria")
    out = {"sections": []}
    by_sec = criteria.groupby("section_id")

    for _, srow in sections.sort_values("order").iterrows():
        sid = str(srow["section_id"])
        sec = {
            "id": sid,
            "title": srow.get("title",""),
            "points": float(srow.get("points", 0) or 0),
            "criteria": []
        }
        if sid in by_sec.groups:
            for _, crow in by_sec.get_group(sid).iterrows():
                # parse args_json (optional)
                args = {}
                raw = crow.get("args_json", "")
                if isinstance(raw, str) and raw.strip():
                    try:
                        args = json.loads(raw)
                    except Exception:
                        args = {}
                ctype = _normalize_type(crow.get("type",""))
                sec["criteria"].append({
                    "id": str(crow["criterion_id"]),
                    "criterion": crow.get("label",""),
                    "type": ctype,
                    "args": args,
                    "max": float(crow.get("max_points", 0) or 0),
                })
        out["sections"].append(sec)

    _normalize_inplace(out)
    _validate(out)
    return out