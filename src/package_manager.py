# src/package_manager.py
import sys, subprocess, importlib
from functools import lru_cache

BASELINE = [
    "numpy>=1.26",
    "pandas>=2.2",
    "matplotlib>=3.8",
    "seaborn>=0.13",
    "statsmodels>=0.14",
    "openpyxl>=3.1",
    # add more common student libs here if needed
]

def _pip_install(package: str):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

@lru_cache(maxsize=None)
def ensure_package(mod_or_spec: str, pip_spec: str | None = None) -> bool:
    """
    Try to import a module; if missing, pip install then import again.
    Returns True if import succeeds.
    """
    try:
        importlib.import_module(mod_or_spec)
        return True
    except ImportError:
        _pip_install(pip_spec or mod_or_spec)
        importlib.invalidate_caches()
        importlib.import_module(mod_or_spec)
        return True

def ensure_baseline():
    for spec in BASELINE:
        # If given as 'pkg>=ver' use the left part for import when possible
        pip_spec = spec
        mod = spec.split(">=")[0].split("==")[0].strip()
        try:
            ensure_package(mod, pip_spec)
        except Exception:
            # don't fail the whole run if a baseline extra fails; continue
            pass