# src/segmentor.py
from __future__ import annotations
import re
from typing import Dict, List, Any
import nbformat

_Q_HDR_RE = re.compile(
    r"""^                # start of line
        \s{0,3}          # optional leading spaces
        (?:\#{1,6}\s*)?  # optional markdown heading marks like #, ##, ...
        (?:Q|Question)\s* # Q or Question
        (?P<num>\d{1,2})  # the number
        \b                # word boundary
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Also catch code/comment forms like "# Q3", "# Question 7", "#--- Q5 ---"
_Q_CODE_RE = re.compile(
    r"""(?:
            ^\s*#\s*                 # start of line, comment
         |  ^\s*['"]{3}.*?\n         # or start of a triple-quoted block (rare)
        )
        \s*(?:Q|Question)\s*(?P<num>\d{1,2})\b
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)

def _cell_has_q_mark(cell, regex) -> int | None:
    src = (cell.get("source") or "").strip()
    if not src:
        return None
    m = regex.search(src.splitlines()[0])  # check first line for headings
    if m:
        try:
            return int(m.group("num"))
        except Exception:
            return None
    return None

def _cell_has_q_comment(cell, regex) -> int | None:
    src = (cell.get("source") or "")
    if not src:
        return None
    m = regex.search(src)
    if m:
        try:
            return int(m.group("num"))
        except Exception:
            return None
    return None

def split_sections(nb: nbformat.NotebookNode) -> Dict[str, Any]:
    """
    Scan a (possibly executed) notebook and return a dict:
    {
      "Q1": {"title": "Q1 ...", "start": 3, "end": 9, "cell_idxs": [3..9]},
      "Q2": {...},
      ...
      "_order": ["Q1","Q2",...]
    }

    Rules:
    - A section starts at a Markdown heading like '# Q3 ...' or '## Question 3 ...'
      OR at a code cell starting with a comment '# Q3'.
    - A section ends right before the next section's start (or at the last cell).
    - Titles are pulled from the heading line when present; otherwise 'Qn'.
    """
    if not nb or "cells" not in nb:
        return {}

    boundaries: List[tuple[int, str, str]] = []  # (cell_idx, qid, title)
    for i, cell in enumerate(nb["cells"]):
        ctype = cell.get("cell_type")
        if ctype == "markdown":
            num = _cell_has_q_mark(cell, _Q_HDR_RE)
            if num:
                line0 = (cell.get("source") or "").splitlines()[0].strip()
                title = line0.lstrip("#").strip() or f"Q{num}"
                boundaries.append((i, f"Q{num}", title))
                continue
        elif ctype == "code":
            num = _cell_has_q_comment(cell, _Q_CODE_RE)
            if num:
                title = f"Q{num}"
                boundaries.append((i, f"Q{num}", title))
                continue

    # If nothing found, return empty
    if not boundaries:
        return {}

    # Deduplicate in case of repeated markers
    deduped: List[tuple[int, str, str]] = []
    seen = set()
    for tup in boundaries:
        if (tup[0]) in seen:
            continue
        deduped.append(tup)
        seen.add(tup[0])

    deduped.sort(key=lambda x: x[0])

    # Build spans
    out: Dict[str, Any] = {"_order": []}
    for idx, (start_idx, qid, title) in enumerate(deduped):
        end_idx = (deduped[idx + 1][0] - 1) if (idx + 1 < len(deduped)) else (len(nb["cells"]) - 1)
        cell_idxs = list(range(start_idx, end_idx + 1))
        # If the first cell is the heading itself, keep it; graders may read it for context.
        out[qid] = {
            "title": title,
            "start": start_idx,
            "end": end_idx,
            "cell_idxs": cell_idxs,
        }
        out["_order"].append(qid)

    return out