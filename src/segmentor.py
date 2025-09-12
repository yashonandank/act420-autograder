import re

def split_sections(nb, rubric=None):
    """
    Split notebook cells into sections based on rubric-defined section IDs.
    
    Args:
        nb: executed notebook dict
        rubric: rubric dict loaded by load_rubric_json/load_rubric_excel
    
    Returns:
        dict: {section_id: [cells]}
    """
    spans = {}
    current = None

    # Build regex patterns directly from rubric section IDs if provided
    section_ids = []
    if rubric:
        section_ids = [s["id"] for s in rubric.get("sections", [])]

    for cell in nb["cells"]:
        if cell["cell_type"] == "markdown":
            text = "".join(cell["source"]).strip()

            # Match rubric-defined section IDs
            for sec in section_ids:
                # Allow slight markdown variations (#, ##, bold, etc.)
                if re.search(rf"\b{re.escape(sec)}\b", text, re.IGNORECASE):
                    current = sec
                    if current not in spans:
                        spans[current] = []
                    break

        if current:
            spans[current].append(cell)

    return spans