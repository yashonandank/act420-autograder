"""
Split executed notebooks into Q1/Q2/... sections.
Step 2 will implement:
- detect markdown headings "Q1", "Question 1", etc.
- optional cell metadata tags (cell.metadata["question"])
- return spans: {"Q1":{"start":i,"end":j}, ...}
"""
# TODO: implement in Step 2