# src/notebook_exec.py
import time
import nbformat
from nbclient import NotebookClient
from nbconvert import HTMLExporter

class ExecResult:
    def __init__(self, executed_nb, html, duration_s, errors):
        self.executed_nb = executed_nb
        self.html = html
        self.duration_s = duration_s
        self.errors = errors  # list of {"ename","evalue","traceback"}

def run_ipynb_bytes(ipynb_bytes: bytes, timeout_per_cell: int = 90) -> ExecResult:
    """
    Execute a notebook (bytes) and return executed nb + HTML preview + timing + error summaries.
    Designed to run on Streamlit Cloud (no Docker).
    """
    # decode to str -> nbformat
    text = ipynb_bytes.decode("utf-8")
    nb = nbformat.reads(text, as_version=4)

    client = NotebookClient(
        nb,
        timeout=timeout_per_cell,
        kernel_name="python3",
        allow_errors=True,  # don't stop on first error; we'll collect them
        resources={"metadata": {"path": "."}},
    )

    t0 = time.time()
    executed = client.execute()
    dur = time.time() - t0

    # collect simple error summaries
    errs = []
    for cell in executed["cells"]:
        if cell.get("outputs"):
            for out in cell["outputs"]:
                if out.get("output_type") == "error":
                    errs.append({
                        "ename": out.get("ename"),
                        "evalue": out.get("evalue"),
                        "traceback": out.get("traceback", []),
                    })

    html, _ = HTMLExporter().from_notebook_node(executed)
    return ExecResult(executed, html, dur, errs)