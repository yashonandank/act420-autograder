# src/segmentor.py
from __future__ import annotations
import re
from typing import Dict, Any, List
import nbformat

# Very tolerant markers
_Q_LINE_RE = re.compile(
    r"""
    ^\s{0,4}                # optional leading spaces
    (?:\#{1,6}\s*)?         # optional markdown heading marks
    (?:>+\s*)?              # optional blockquote
    (?:[-*]\s*)?            # optional bullet
    (?:\*\*|__)?\s*         # optional bold open
    (?:Q|Question)\s*       # Q or Question
    (?P<num>\d{1,2})        # number
    [):.\-\s]*              # trailing punctuation/space
    (?:\*\*|__)?\s*$        # optional bold close, optional trailing space
    """,
    re.IGNORECASE | re.VERBOSE,
)

_Q_CODE_RE = re.compile(
    r"""
    ^\s*#\s*
    (?:
        AUTOGRADE:\s*(?:Q|Question)\s*(?P<num1>\d{1,2})
        |
        (?:Q|Question)\s*(?P<num2>\d{1,2})
    )
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)

def _find_q_in_markdown(src: str) -> int | None:
    if not src:
        return None
    # check first up to 3 non-empty lines
    lines = [ln.strip() for ln in src.splitlines()]
    picked = []
    for ln in lines:
        if ln.strip():
            picked.append(ln)
        if len(picked) >= 3:
            break
    for ln in picked:
        m = _Q_LINE_RE.match(ln)
        if m:
            n = m.group("num")
            try:
                return int(n)
            except Exception:
                return None
    return None

def _find_q_in_code(src: str) -> int | None:
    if not src:
        return None
    for ln in src.splitlines():
        m = _Q_CODE_RE.match(ln)
        if m:
            num = m.group("num1") or m.group("num2")
            try:
                return int(num)
            except Exception:
                return None
    return None

def split_sections(nb: nbformat.NotebookNode) -> Dict[str, Any]:
    """
    Return:
    {
      "Q1": {"title": "Q1 …", "start": i, "end": j, "cell_idxs": [...]} ,
      ...
      "_order": ["Q1","Q2",...]
    }
    """
    if not nb or "cells" not in nb:
        return {}

    boundaries: List[tuple[int, str, str]] = []
    for i, cell in enumerate(nb["cells"]):
        ctype = cell.get("cell_type")
        src = cell.get("source") or ""
        num = None
        title = None

        if ctype == "markdown":
            num = _find_q_in_markdown(src)
            if num:
                # title: prefer the first non-empty line
                first_nonempty = next((ln.strip() for ln in src.splitlines() if ln.strip()), "")
                # strip leading markdown symbols
                title = re.sub(r"^\s*(?:\#{1,6}\s*|>+\s*|[-*]\s*)", "", first_nonempty).strip()
                if not title:
                    title = f"Q{num}"
        elif ctype == "code":
            num = _find_q_in_code(src)
            if num:
                title = f"Q{num}"

        if num:
            boundaries.append((i, f"Q{num}", title or f"Q{num}"))

    if not boundaries:
        return {}

    boundaries.sort(key=lambda x: x[0])

    # coalesce duplicates if someone marks the same Q multiple times
    dedup: List[tuple[int, str, str]] = []
    seen_qids: set[str] = set()
    for b in boundaries:
        if b[1] in seen_qids:
            continue
        dedup.append(b)
        seen_qids.add(b[1])

    out: Dict[str, Any] = {"_order": []}
    for idx, (start_idx, qid, title) in enumerate(dedup):
        end_idx = dedup[idx + 1][0] - 1 if idx + 1 < len(dedup) else (len(nb["cells"]) - 1)
        out[qid] = {
            "title": title,
            "start": start_idx,
            "end": end_idx,
            "cell_idxs": list(range(start_idx, end_idx + 1)),
        }
        out["_order"].append(qid)

    return out