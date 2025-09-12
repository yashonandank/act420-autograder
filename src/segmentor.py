def split_sections(nb, rubric=None):
    """
    Split a notebook into sections by looking for rubric section IDs in markdown cells.
    Falls back to Q1/Q2 detection if rubric is not provided.
    """
    spans = {}
    current = None

    for cell in nb["cells"]:
        if cell["cell_type"] == "markdown":
            txt = "".join(cell.get("source", []))
            if rubric:
                # Look for exact rubric section IDs
                for sec in rubric["sections"]:
                    if sec["id"] in txt:
                        current = sec["id"]
                        spans[current] = []
            else:
                # Fallback heuristic (Q1, Q2, etc.)
                if txt.strip().startswith("Q"):
                    current = txt.strip().split()[0]
                    spans[current] = []
        if current:
            spans[current].append(cell)
    return spans