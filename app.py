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
from src.grader import build_probes_from_rubric, evaluate
from src.feedback_generator import feedback_from_scores

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
st.caption("Run notebooks • Detect sections • Deterministic grading (LLM pass next)")

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
# Notebook execution
# ----------------------------
st.divider()
st.subheader("Upload student submissions (.ipynb or .zip of many)")

subs_file = st.file_uploader("Notebook or ZIP", type=["ipynb", "zip"], key="subs")

st.caption("Optional: upload shared data files and per-assignment requirements so relative paths & imports work:")
data_zip_file = st.file_uploader("Data ZIP (e.g., Excel/CSV files students read relatively)", type=["zip"], key="datazip")
reqs_file = st.file_uploader("requirements.txt (optional additions)", type=["txt"], key="reqs")

# Execution options
st.caption("Execution options")
colA, colB, colC = st.columns([1, 1, 2])
with colA:
    cell_timeout = st.number_input("Cell timeout (sec)", min_value=30, max_value=600, value=120, step=10)
with colB:
    retry_timeout = st.checkbox("Auto-retry on timeout (2×)", value=True)
with colC:
    skip_tags_str = st.text_input("Skip cells with tags (comma-separated)", value="skip_autograde,long")
skip_tags = [s.strip() for s in skip_tags_str.split(",") if s.strip()]

if "executions" not in st.session_state:
    st.session_state["executions"] = []  # list of dicts per executed notebook

def _collect_ipynbs(upload):
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
    probes = build_probes_from_rubric(st.session_state.get("rubric")) if st.session_state.get("rubric") else {}

    if not ipynbs:
        st.warning("No .ipynb files found in the upload.")
    else:
        st.session_state["executions"].clear()
        progress = st.progress(0.0)
        for i, (name, data) in enumerate(ipynbs, start=1):
            try:
                res = run_ipynb_bytes(
                    data,
                    timeout_per_cell=int(cell_timeout),
                    data_zip=data_zip_bytes,
                    extra_requirements_txt=req_bytes,
                    probes=probes,
                    retry_on_timeout=retry_timeout,
                    skip_tags=skip_tags,
                )
            except TypeError:
                # Older notebook_exec without probes/controls support
                res = run_ipynb_bytes(
                    data,
                    timeout_per_cell=int(cell_timeout),
                    data_zip=data_zip_bytes,
                    extra_requirements_txt=req_bytes,
                )
                # synthesize empty probe results so downstream code doesn’t break
                res.probe_results = {}
            spans = split_sections(res.executed_nb)
            st.session_state["executions"].append(
                {
                    "name": name,
                    "html": res.html,
                    "duration_s": res.duration_s,
                    "errors": res.errors,
                    "sections": spans,
                    "probe_results": getattr(res, "probe_results", {}),
                    # keep the executed notebook around in case we want artifact counts later
                    "executed_nb": res.executed_nb,
                }
            )
            progress.progress(i / len(ipynbs))
        st.success(f"Executed {len(ipynbs)} notebook(s).")

# ---- Preview executions (basic, clean) ----
for info in st.session_state["executions"]:
    st.markdown(f"### 📄 {info['name']}")
    cols = st.columns([1, 2, 2])
    with cols[0]:
        st.caption(f"⏱ Ran in {info['duration_s']:.1f}s")
        if info["errors"]:
            st.error(f"⚠️ {len(info['errors'])} execution errors")
            with st.expander("Show error summaries"):
                for e in info["errors"]:
                    st.code(f"{e.get('ename')}: {e.get('evalue')}", language="text")
        else:
            st.success("✅ No execution errors")
        if info["sections"]:
            st.write("Detected sections:")
            st.code(", ".join(sorted(info["sections"].keys())), language="text")
        else:
            st.write("No Q-sections detected")
    with cols[1]:
        st.markdown("**Executed Notebook Preview**")
        st.components.v1.html(info["html"], height=500, scrolling=True)
    with cols[2]:
        st.markdown("**Raw section spans**")
        st.json(info["sections"])

# ----------------------------
# Deterministic grading
# ----------------------------
st.divider()
st.subheader("Grading (deterministic checks)")

grade_disabled = not st.session_state.get("rubric") or not st.session_state.get("executions")
grade_clicked = st.button("🧮 Grade all (deterministic)", disabled=grade_disabled, type="primary")

if grade_clicked:
    rubric = st.session_state.get("rubric")
    results = []
    for info in st.session_state["executions"]:
        det = evaluate(rubric, info.get("probe_results", {}))  # (figure_exists via HTML can be added later)
        # attach feedback bullets per section
        for sec in det["sections"]:
            sec["feedback"] = feedback_from_scores(sec)
        results.append({"name": info["name"], "deterministic": det})
    st.session_state["grading_results"] = results
    st.success("Grading complete.")

# ---- Results viewer ----
if st.session_state.get("grading_results"):
    for r in st.session_state["grading_results"]:
        st.markdown(f"### 🧾 {r['name']}")
        det = r["deterministic"]
        st.caption(f"Total: **{det['total']['earned']:.2f} / {det['total']['max']:.2f}**")
        for sec in det["sections"]:
            with st.expander(f"{sec['id']} — {sec['earned']:.2f} / {sec['max']:.2f}", expanded=False):
                st.markdown("**Criteria**")
                st.dataframe(
                    [{
                        "criterion_id": row["id"],
                        "score": row["score"],
                        "max": row["max"],
                        "why": row["justification"],
                    } for row in sec["rows"]],
                    hide_index=True, use_container_width=True
                )
                st.markdown("**Feedback**")
                for b in sec.get("feedback", []):
                    st.write("- " + b)

st.divider()
st.subheader("Next steps")
st.markdown(
    "- **LLM grading pass** (compare vs deterministic and flag mismatches).\n"
    "- **Editable scores & comments** before finalizing.\n"
    "- **Student report export** (HTML/PDF) and CSV of grades."
)