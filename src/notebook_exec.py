# src/notebook_exec.py
import os, io, time, sys, re, tempfile, zipfile, shutil, json, textwrap
import nbformat
from nbclient import NotebookClient
from nbconvert import HTMLExporter
from jupyter_client.kernelspec import KernelSpecManager, NoSuchKernel
from ipykernel.kernelspec import install as install_ipykernel_spec
from .package_manager import ensure_baseline, ensure_package

class ExecResult:
    def __init__(self, executed_nb, html, duration_s, errors, probe_results):
        self.executed_nb = executed_nb
        self.html = html
        self.duration_s = duration_s
        self.errors = errors              # list[dict]
        self.probe_results = probe_results  # dict[str, any]

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

def _append_probe_cell(nb: nbformat.NotebookNode, probes: dict[str, str]) -> nbformat.NotebookNode:
    """
    Append a cell that evaluates each probe expression safely and prints a JSON blob.
    `probes` is dict: {probe_id: python_expr}
    """
    marker = "__GRADER_PROBES_JSON__"
    code_lines = [
        "import json",
        "def _safe_eval(expr):",
        "    try:",
        "        return eval(expr, globals(), locals())",
        "    except Exception as e:",
        "        return {'__error__': str(e)}",
        f"_probes = {json.dumps(probes)}",
        "_out = {}",
        "for k, expr in _probes.items():",
        "    _out[k] = _safe_eval(expr)",
        f"print('{marker}' + json.dumps(_out))"
    ]
    cell = nbformat.v4.new_code_cell(source="\n".join(code_lines))
    nb.cells.append(cell)
    return nb, marker

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

def _extract_probe_json(executed_nb, marker: str) -> dict:
    payload = {}
    for cell in executed_nb.get("cells", []):
        for out in cell.get("outputs", []) or []:
            text = None
            if out.get("output_type") == "stream":
                text = out.get("text", "")
            elif out.get("output_type") == "display_data":
                text = out.get("data", {}).get("text/plain", "")
            if not text:
                continue
            if isinstance(text, list):  # sometimes list of lines
                text = "".join(text)
            if marker in str(text):
                blob = str(text).split(marker, 1)[1].strip()
                try:
                    payload = json.loads(blob)
                except Exception:
                    pass
    return payload

def run_ipynb_bytes(
    ipynb_bytes: bytes,
    timeout_per_cell: int = 90,
    data_zip: bytes | None = None,
    extra_requirements_txt: bytes | None = None,
    probes: dict[str, str] | None = None,
) -> ExecResult:
    """
    Execute a notebook with:
    - baseline libs ensured
    - optional requirements.txt (pip install)
    - optional data.zip extracted to working dir (so relative file paths resolve)
    - optional `probes` dict of {probe_id: python_expr}; evaluated inside the kernel
    - one retry if ModuleNotFoundError occurs (auto-install that module)
    """
    workdir = tempfile.mkdtemp(prefix="grader_run_")
    try:
        if data_zip:
            with zipfile.ZipFile(io.BytesIO(data_zip)) as z:
                z.extractall(workdir)

        if extra_requirements_txt:
            req_path = os.path.join(workdir, "requirements.txt")
            with open(req_path, "wb") as f:
                f.write(extra_requirements_txt)
            try:
                import subprocess
                subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_path])
            except Exception:
                pass

        ensure_baseline()

        base_nb = nbformat.reads(ipynb_bytes.decode("utf-8"), as_version=4)
        kernel_name = getattr(getattr(base_nb, "metadata", {}), "kernelspec", {}).get("name", None) or "python3"
        kernel_name = _ensure_kernel(kernel_name)

        # append probe cell (if any)
        marker = None
        nb_to_run = nbformat.from_dict(base_nb)
        if probes:
            nb_to_run, marker = _append_probe_cell(nb_to_run, probes)

        executed, html, dur, errs = _run_once(nb_to_run, workdir, timeout_per_cell, kernel_name)

        # retry if missing module
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
                nb_to_run = nbformat.from_dict(base_nb)
                if probes:
                    nb_to_run, marker = _append_probe_cell(nb_to_run, probes)
                executed, html, dur, errs = _run_once(nb_to_run, workdir, timeout_per_cell, kernel_name)
            except Exception:
                pass

        probe_results = _extract_probe_json(executed, marker) if marker else {}
        return ExecResult(executed, html, dur, errs, probe_results)
    finally:
        try:
            shutil.rmtree(workdir)
        except Exception:
            pass