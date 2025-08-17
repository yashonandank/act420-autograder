# src/notebook_exec.py
import os, io, time, sys, re, tempfile, zipfile, shutil
import nbformat
from nbclient import NotebookClient
from nbconvert import HTMLExporter
from jupyter_client.kernelspec import KernelSpecManager, NoSuchKernel
from ipykernel.kernelspec import install as install_ipykernel_spec
from .package_manager import ensure_baseline, ensure_package

class ExecResult:
    def __init__(self, executed_nb, html, duration_s, errors):
        self.executed_nb = executed_nb
        self.html = html
        self.duration_s = duration_s
        self.errors = errors  # list of {"ename","evalue","traceback"}

def _ensure_kernel(kernel_name: str = "python3"):
    ksm = KernelSpecManager()
    try:
        ksm.get_kernel_spec(kernel_name)
        return kernel_name
    except NoSuchKernel:
        install_ipykernel_spec(user=True, name=kernel_name, display_name="Python 3 (App)")
        ksm.get_kernel_spec(kernel_name)
        return kernel_name

_MISSING_MOD_RE = re.compile(r"No module named '([^']+)'")

def _run_once(nb, workdir: str, timeout_per_cell: int, kernel_name: str):
    client = NotebookClient(
        nb,
        timeout=timeout_per_cell,
        kernel_name=kernel_name,
        allow_errors=True,
        resources={"metadata": {"path": workdir}},
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
    return executed, html, dur, errs

def run_ipynb_bytes(
    ipynb_bytes: bytes,
    timeout_per_cell: int = 90,
    data_zip: bytes | None = None,
    extra_requirements_txt: bytes | None = None,
) -> ExecResult:
    """
    Execute a notebook with:
    - baseline libs ensured (numpy/pandas/matplotlib/seaborn/statsmodels/openpyxl)
    - optional requirements.txt (pip install)
    - optional data.zip extracted to working dir so relative file paths resolve.
    - one retry if ModuleNotFoundError occurs (auto-install that module)
    """
    # Prepare working directory
    workdir = tempfile.mkdtemp(prefix="grader_run_")
    try:
        # 1) Write uploaded data (if any)
        if data_zip:
            with zipfile.ZipFile(io.BytesIO(data_zip)) as z:
                z.extractall(workdir)

        # 2) Optional requirements.txt
        if extra_requirements_txt:
            req_path = os.path.join(workdir, "requirements.txt")
            with open(req_path, "wb") as f:
                f.write(extra_requirements_txt)
            # pip install -r requirements.txt (best-effort)
            try:
                import subprocess
                subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_path])
            except Exception:
                pass  # don't block if it fails

        # 3) Ensure baseline common packages
        ensure_baseline()

        # 4) Load notebook
        nb = nbformat.reads(ipynb_bytes.decode("utf-8"), as_version=4)

        # 5) Kernel
        kernel_name = getattr(getattr(nb, "metadata", {}), "kernelspec", {}).get("name", None)
        kernel_name = kernel_name or "python3"
        kernel_name = _ensure_kernel(kernel_name)

        # 6) First run
        executed, html, dur, errs = _run_once(nb, workdir, timeout_per_cell, kernel_name)

        # 7) If missing module error appears, try auto-install once and retry
        missing = None
        for e in errs:
            if e.get("ename") == "ModuleNotFoundError" and e.get("evalue"):
                m = _MISSING_MOD_RE.search(e["evalue"])
                if m:
                    missing = m.group(1)
                    break

        if missing:
            try:
                ensure_package(missing)
                # rerun from scratch (fresh parse)
                nb = nbformat.reads(ipynb_bytes.decode("utf-8"), as_version=4)
                executed, html, dur, errs = _run_once(nb, workdir, timeout_per_cell, kernel_name)
            except Exception:
                # ignore if install fails; keep original errs
                pass

        return ExecResult(executed, html, dur, errs)
    finally:
        # Clean up temp dir
        try:
            shutil.rmtree(workdir)
        except Exception:
            pass