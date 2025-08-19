# src/feedback_generator.py
from __future__ import annotations
from typing import Dict, Any, List

def feedback_from_scores(section_result: Dict[str, Any]) -> List[str]:
    """
    Turn per-criterion scores into short, actionable bullets.
    """
    bullets = []
    for row in section_result.get("rows", []):
        cid = row["id"]; score = row["score"]; maxp = row["max"]; note = row.get("justification","")
        if score >= maxp:
            bullets.append(f"{cid}: ✅ Met expectations. {note}")
        elif score == 0:
            bullets.append(f"{cid}: ❌ Not met. {note}")
        else:
            bullets.append(f"{cid}: ⚠️ Partial credit ({score}/{maxp}). {note}")
    return bullets