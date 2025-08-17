# app.py
import io
import json
import time
import zipfile
import pandas as pd
import streamlit as st

from src.segmentor import split_sections
from src.notebook_exec import run_ipynb_bytes
from src.rubric_schema import load_rubric_json, load_rubric_excel

# ----------------------------
# Page setup
# ----------------------------
st.set_page_config(layout="wide", page_title="Analytics Autograder")

# ----------------------------
# Simple login (plain secrets)
# ----------------------------
SESSION_KEY = "auth"
SESSION_TTL = 60 * 60  # 60 minutes

def check_login() -> bool:
    """Plain username/password auth using st.secrets['users']."""
    auth = st.session_state.get(SESSION_KEY)
    now = time.time()
    if auth and (now - auth["ts"] < SESSION_TTL):
        # refresh TTL
        st.session_state[SESSION_KEY]["ts"] = now
        return True

    st.title("🔐 Login")

    users = st.secrets.get("users", {})
    if not users:
        st.warning(
            "No users found in Secrets. Add a [users] block in "
            "`.streamlit/secrets.toml` (local) or in the Streamlit Cloud Secrets UI."
        )

    with st.form("login"):
        u = st.text_input("Username", value="")
        p = st.text_input("Password", type="password", value="")
        ok = st.form_submit_button("Sign in")

    if ok:
        if u in users and p == str(users[u]):
            st.session_state[SESSION_KEY] = {"user": u, "ts": time.time()}
            st.success(f"Welcome, {u}!")
            st.rerun()
        else:
            st.error("Invalid credentials")
    st.stop()

def logout_button():
    with st.sidebar:
        st.caption("Session")
        if st.button("Log out"):
            st.session_state.pop(SESSION_KEY, None)
            st.rerun()

# Require login
check_login()
logout_button()

# ----------------------------
# Title
# ----------------------------
st.title("📊 Analytics Notebook Autograder")
st.caption("Step 1–2: Login + rubric upload/validation + notebook execution & section detection")

# ----------------------------
# Rubric upload/validate/preview
# ----------------------------
with st.expander("➡️ Excel rubric template format"):
    st.write(
        "- **Sections** sheet: `section_id`, `title`, `points`, `order`\n"
        "- **Criteria** sheet: `section_id`, `criterion_id`, `label`, `type`, `args_json`, `max_points`, `auto_only`, `bonus_cap`\n"
        "- Allowed `type`: `columns`, `row_count`, `stat_range`, `unique_count`, `null_rate`, `table_shape`, `figure_exists`, `llm_feedback`"
    )

st.subheader("Upload Rubric")
rubric_file = st.file_uploader(
    "Upload `rubric.json` **or** an Excel rubric (must include the sheets above)",
    type=["json", "xlsx"],
    key="rubric_uploader"
)

def preview_rubric(rubric: dict):
    # Top summary
    cols = st.columns([1, 2, 1])
    with cols[0]:
        st.metric("Sections", len(rubric["sections"]))
    with cols[2]:
        total = sum(s.get("points", 0) for s in rubric["sections"])
        st.metric("Total Points", total)

    # Per-section table
    for s in rubric["sections"]:
        with st.container(border=True):
            st.markdown(f"**{s['id']}** — {s.get('title','')}")
            st.caption(f"Max points: {s.get('points',0)}")
            df = pd.DataFrame(
                [
                    {
                        "criterion_id": r.get("id"),
                        "label": r.get("criterion"),
                        "type": r.get("type"),
                        "max": r.get("max"),
                        "args": json.dumps(r.get("args", {})),
                        "auto_only": r.get("auto_only"),
                        "bonus_cap": r.get("bonus_cap"),
                    }
                    for r in s["criteria"]
                ]
            )
            st.dataframe(df, use_container_width=True, hide_index=True)

if rubric_file:
    try:
        rubric = load_rubric_json(rubric_file) if rubric_file.name.endswith(".json") else load_rubric_excel(rubric_file)
        st.success("Rubric loaded and validated ✅")
        st.session_state["rubric"] = rubric
        preview_rubric(rubric)
    except Exception as e:
        st.error(f"Failed to load rubric: {e}")

# ----------------------------
# Notebook execution (Step 2)
# ----------------------------
st.divider()
st.subheader("Upload student submissions (.ipynb or .zip of many)")

subs_file = st.file_uploader("Notebook or ZIP", type=["ipynb", "zip"], key="subs")

st.caption("Optional: upload shared data files and per-assignment requirements so relative paths & imports work:")
data_zip_file = st.file_uploader("Data ZIP (e.g., Excel/CSV files students read relatively)", type=["zip"], key="datazip")
reqs_file = st.file_uploader("requirements.txt (optional additions)", type=["txt"], key="reqs")

if "executions" not in st.session_state:
    st.session_state["executions"] = []  # list of dicts per executed notebook

def _collect_ipynbs(upload) -> list[tuple[str, bytes]]:
    """Return list of (name, bytes) from a single .ipynb or a .zip containing many."""
    if upload.name.lower().endswith(".ipynb"):
        return [(upload.name, upload.getvalue())]
    out = []
    with zipfile.ZipFile(io.BytesIO(upload.getvalue())) as z:
        for name in z.namelist():
            if name.lower().endswith(".ipynb") and not name.endswith("/"):
                out.append((name, z.read(name)))
    return out

run_col1, run_col2 = st.columns([1, 4])
with run_col1:
    run_clicked = st.button("▶️ Run notebooks", type="primary", disabled=not subs_file)

if run_clicked and subs_file:
    ipynbs = _collect_ipynbs(subs_file)
    data_zip_bytes = data_zip_file.getvalue() if data_zip_file else None
    req_bytes = reqs_file.getvalue() if reqs_file else None

    if not ipynbs:
        st.warning("No .ipynb files found in the upload.")
    else:
        st.session_state["executions"].clear()
        progress = st.progress(0.0)
        for i, (name, data) in enumerate(ipynbs, start=1):
            res = run_ipynb_bytes(
                data,
                timeout_per_cell=90,
                data_zip=data_zip_bytes,
                extra_requirements_txt=req_bytes,
            )
            spans = split_sections(res.executed_nb)
            st.session_state["executions"].append(
                {
                    "name": name,
                    "html": res.html,
                    "duration_s": res.duration_s,
                    "errors": res.errors,
                    "sections": spans,
                }
            )
            progress.progress(i / len(ipynbs))
        st.success(f"Executed {len(ipynbs)} notebook(s).")

# Preview executions
for info in st.session_state["executions"]:
    st.markdown(f"### 📄 {info['name']}")
    cols = st.columns([1, 2, 2])
    with cols[0]:
        st.caption(f"Ran in {info['duration_s']:.1f}s")
        if info["errors"]:
            st.error(f"Errors: {len(info['errors'])}")
            with st.expander("Show error summaries"):
                for e in info["errors"]:
                    st.code(f"{e.get('ename')}: {e.get('evalue')}", language="text")
        else:
            st.success("No execution errors")
        if info["sections"]:
            st.write("Detected sections:")
            st.code(", ".join(sorted(info["sections"].keys())), language="text")
        else:
            st.write("No Q-sections detected")

    with cols[1]:
        st.markdown("**Executed Notebook Preview**")
        st.components.v1.html(info["html"], height=450, scrolling=True)

    with cols[2]:
        st.markdown("**Raw section spans**")
        st.json(info["sections"])

st.divider()
st.subheader("Next steps")
st.markdown(
    "- **Step 3:** Deterministic grading checks + optional LLM feedback (per-section).\n"
    "- **Step 4:** Gradescope-style review UI and student report export."
)