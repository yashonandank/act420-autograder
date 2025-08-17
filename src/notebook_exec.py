# src/notebook_exec.py
import time
import nbformat
from nbclient import NotebookClient
from nbconvert import HTMLExporter

# new imports ↓
from jupyter_client.kernelspec import KernelSpecManager, NoSuchKernel
from ipykernel.kernelspec import install as install_ipykernel_spec

class ExecResult:
    def __init__(self, executed_nb, html, duration_s, errors):
        self.executed_nb = executed_nb
        self.html = html
        self.duration_s = duration_s
        self.errors = errors  # list of {"ename","evalue","traceback"}

def _ensure_kernel(kernel_name: str = "python3"):
    """Make sure a kernelspec exists; install one under the user if missing."""
    ksm = KernelSpecManager()
    try:
        ksm.get_kernel_spec(kernel_name)
        return kernel_name
    except NoSuchKernel:
        # Register a kernelspec for the current env
        install_ipykernel_spec(user=True, name=kernel_name, display_name="Python 3 (App)")
        # Recheck
        ksm.get_kernel_spec(kernel_name)
        return kernel_name

def run_ipynb_bytes(ipynb_bytes: bytes, timeout_per_cell: int = 90) -> ExecResult:
    """
    Execute a notebook (bytes) and return executed nb + HTML preview + timing + error summaries.
    Designed to run on Streamlit Cloud (no Docker).
    """
    text = ipynb_bytes.decode("utf-8")
    nb = nbformat.reads(text, as_version=4)

    # pick kernel name from notebook metadata if present; fallback to python3
    kernel_name = None
    try:
        kernel_name = nb.metadata.kernelspec.name  # type: ignore[attr-defined]
    except Exception:
        pass
    kernel_name = kernel_name or "python3"
    kernel_name = _ensure_kernel(kernel_name)

    client = NotebookClient(
        nb,
        timeout=timeout_per_cell,
        kernel_name=kernel_name,
        allow_errors=True,  # don't stop on first error; we'll collect them
        resources={"metadata": {"path": "."}},
    )

    t0 = time.time()
    executed = client.execute()
    dur = time.time() - t0

    errs = []
    for cell in executed.get("cells", []):
        for out in cell.get("outputs", []) or []:
            if out.get("output_type") == "error":
                errs.append({
                    "ename": out.get("ename"),
                    "evalue": out.get("evalue"),
                    "traceback": out.get("traceback", []),
                })

    html, _ = HTMLExporter().from_notebook_node(executed)
    return ExecResult(executed, html, dur, errs)