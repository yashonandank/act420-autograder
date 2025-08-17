# src/segmentor.py
import re
from typing import Dict, Any

_HEADING = re.compile(r'^\s*#{1,6}\s*(Q(?:uestion)?\s*\d+)\b', re.I)
_PLAIN   = re.compile(r'^\s*(Q(?:uestion)?\s*\d+)[\.:]?', re.I)

def split_sections(nb: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """
    Returns spans per question: {"Q1":{"start":i,"end":j}, ...}
    Priority:
      1) cell.metadata["question"] if present,
      2) markdown headings like '# Q1' or '# Question 1',
      3) plain text markers 'Q1:', 'Question 1.' in markdown cells.
    """
    sections = {}
    current = None
    for i, cell in enumerate(nb.get("cells", [])):
        label = None

        # metadata tag
        qtag = cell.get("metadata", {}).get("question")
        if isinstance(qtag, str) and qtag.strip():
            label = _normalize(qtag)

        if label is None and cell.get("cell_type") == "markdown":
            txt = "".join(cell.get("source", "")).strip()
            m = _HEADING.match(txt) or _PLAIN.match(txt)
            if m:
                label = _normalize(m.group(1))

        if label:
            current = label
            sections.setdefault(current, {"start": i, "end": i})

        if current:
            sections[current]["end"] = i

    return sections

def _normalize(s: str) -> str:
    n = re.search(r'\d+', s)
    return f"Q{n.group(0)}" if n else s.strip()