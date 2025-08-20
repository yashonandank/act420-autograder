# app.py
import io
import json
import time
import zipfile
from typing import Dict, Any, List

import pandas as pd
import streamlit as st

from src.segmentor import split_sections
from src.notebook_exec import run_ipynb_bytes
from src.rubric_schema import load_rubric_json, load_rubric_excel
from src.llm_grader import build_section_context, grade_section_llm

# ----------------------------
# Page & Session
# ----------------------------
st.set_page_config(layout="wide", page_title="Analytics Autograder")

SESSION_KEY = "auth"
SESSION_TTL = 60 * 60  # 60 minutes

def check_login() -> bool:
    """Plain username/password auth using st.secrets['users']."""
    auth = st.session_state.get(SESSION_KEY)
    now = time.time()
    if auth and (now - auth["ts"] < SESSION_TTL):
        st.session_state[SESSION_KEY]["ts"] = now  # refresh TTL
        return True

    st.title("🔐 Login")

    users = st.secrets.get("users", {})
    if not users:
        st.warning(
            "No users found in Secrets. Add a [users] block in "
            "`.streamlit/secrets.toml` (local) or the Streamlit Cloud Secrets UI."
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

check_login()
logout_button()

# ----------------------------
# Header
# ----------------------------
st.title("📊 Analytics Notebook Autograder")
st.caption("Run notebooks • Detect Q-sections • LLM grading & feedback per section")

# ----------------------------
# Rubric upload/preview
# ----------------------------
with st.expander("➡️ Rubric format (Excel or JSON)"):
    st.write(
        "- **Excel** requires two sheets:\n"
        "  - `Sections`: `section_id`, `title`, `points`, `order`\n"
        "  - `Criteria`: `section_id`, `criterion_id`, `label`, `type` (`llm_grade`), "
        "`args_json` (optional), `max_points`, `auto_only` (ignored), `bonus_cap` (ignored)\n"
        "- **JSON**: `{'sections': [{'id','title','points','criteria':[{'id','criterion','type','args','max'}]}]}`\n"
        "- We ignore non-`llm_grade` types (treated as `llm_grade`)."
    )

st.subheader("Upload Rubric")
rubric_file = st.file_uploader(
    "Upload rubric.xlsx or rubric.json",
    type=["xlsx", "json"],
    key="rubric_uploader"
)

def preview_rubric(rubric: Dict[str, Any]):
    cols = st.columns([1, 2, 1])
    with cols[0]:
        st.metric("Sections", len(rubric["sections"]))
    with cols[2]:
        total = sum(s.get("points", 0) or sum(c.get("max", 0) for c in s.get("criteria", [])) for s in rubric["sections"])
        st.metric("Total Points", total)

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
                    }
                    for r in s.get("criteria", [])
                ]
            )
            st.dataframe(df, use_container_width=True, hide_index=True)

if rubric_file:
    try:
        rubric = load_rubric_json(rubric_file) if rubric_file.name.endswith(".json") else load_rubric_excel(rubric_file)
        st.session_state["rubric"] = rubric
        st.success("Rubric loaded ✅")
        preview_rubric(rubric)
    except Exception as e:
        st.error(f"Failed to load rubric: {e}")

# ----------------------------
# Notebook execution
# ----------------------------
st.divider()
st.subheader("Upload student submissions")

subs_file = st.file_uploader("Notebook (.ipynb) or ZIP of notebooks", type=["ipynb", "zip"], key="subs")

st.caption("Optional: provide shared data & extra requirements (for imports) so students' relative paths work:")
data_zip_file = st.file_uploader("Data ZIP (CSV/Excel/etc.)", type=["zip"], key="datazip")
reqs_file = st.file_uploader("requirements.txt (optional)", type=["txt"], key="reqs")

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

def _collect_ipynbs(upload) -> List[tuple[str, bytes]]:
    if upload.name.lower().endswith(".ipynb"):
        return [(upload.name, upload.getvalue())]
    out = []
    with zipfile.ZipFile(io.BytesIO(upload.getvalue())) as z:
        for name in z.namelist():
            if name.lower().endswith(".ipynb") and not name.endswith("/"):
                out.append((name, z.read(name)))
    return out

run_col1, _ = st.columns([1, 4])
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
            try:
                res = run_ipynb_bytes(
                    data,
                    timeout_per_cell=int(cell_timeout),
                    data_zip=data_zip_bytes,
                    extra_requirements_txt=req_bytes,
                    probes=None,                   # LLM-only mode: no deterministic probes
                    retry_on_timeout=retry_timeout,
                    skip_tags=skip_tags,
                )
            except TypeError:
                # Backward-compat signature without the extra args
                res = run_ipynb_bytes(
                    data,
                    timeout_per_cell=int(cell_timeout),
                    data_zip=data_zip_bytes,
                    extra_requirements_txt=req_bytes,
                )
                res.probe_results = {}

            spans = split_sections(res.executed_nb)
            st.session_state["executions"].append(
                {
                    "name": name,
                    "html": res.html,
                    "duration_s": res.duration_s,
                    "errors": res.errors,
                    "sections": spans,
                    "executed_nb": res.executed_nb,
                }
            )
            progress.progress(i / len(ipynbs))
        st.success(f"Executed {len(ipynbs)} notebook(s).")

# Preview
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
            st.code(", ".join(info["sections"].get("_order", [])) or "—", language="text")
        else:
            st.write("No Q-sections detected")
    with cols[1]:
        st.markdown("**Executed Notebook Preview**")
        st.components.v1.html(info["html"], height=500, scrolling=True)
    with cols[2]:
        st.markdown("**Raw section spans**")
        st.json(info["sections"])

# ----------------------------
# LLM grading per section
# ----------------------------
st.divider()
st.subheader("Grading (LLM per section)")

# Check for OpenAI secrets
OPENAI_OK = "openai" in st.secrets and "api_key" in st.secrets["openai"]
if not OPENAI_OK:
    st.info(
        "Set your OpenAI key in secrets:\n\n"
        "```toml\n[openai]\napi_key = \"sk-...\"\nmodel = \"gpt-4o-mini\"\n```"
    )

grade_disabled = (not st.session_state.get("rubric")
                  or not st.session_state.get("executions")
                  or not OPENAI_OK)

if st.button("🤖 Grade all (LLM)", disabled=grade_disabled, type="primary"):
    from openai import OpenAI
    client = OpenAI(api_key=st.secrets["openai"]["api_key"])
    model = st.secrets["openai"].get("model", "gpt-4o-mini")
    rubric = st.session_state["rubric"]

    results = []
    for info in st.session_state["executions"]:
        spans = info["sections"]
        nb = info["executed_nb"]
        html = info["html"]
        per_sections = []
        for qid in spans.get("_order", []):
            ctx = build_section_context(nb, spans[qid], html)
            graded = grade_section_llm(client, model, rubric, qid, ctx, temperature=0.0)
            per_sections.append(graded)
        total_max = sum(s["total_points"] for s in per_sections)
        total_earned = sum(s["earned_points"] for s in per_sections)
        results.append({
            "name": info["name"],
            "sections": per_sections,
            "total": {"max": total_max, "earned": total_earned},
        })
    st.session_state["llm_results"] = results
    st.success("LLM grading complete.")

# Results viewer + simple overrides
if st.session_state.get("llm_results"):
    for r in st.session_state["llm_results"]:
        st.markdown(f"### 🧾 {r['name']}")
        # running total with possible overrides
        total_earned_override = 0.0
        total_max = r["total"]["max"]

        for sec in r["sections"]:
            with st.expander(f"{sec['section_id']} — {sec['earned_points']:.2f} / {sec['total_points']:.2f}", expanded=False):
                st.markdown(f"**{sec.get('rubric_title','')}**")
                # Per-criterion view
                df = pd.DataFrame(
                    [{
                        "criterion_id": c["criterion_id"],
                        "label": c["label"],
                        "score": c["score"],
                        "max": c["max"],
                        "why": c["rationale"],
                        "tip": c.get("improvement_tip",""),
                    } for c in sec["criteria"]]
                )
                st.dataframe(df, hide_index=True, use_container_width=True)

                st.markdown("**Overall comment**")
                st.write(sec.get("overall_comment",""))

                # Optional manual override at section level
                if "overrides" not in st.session_state:
                    st.session_state["overrides"] = {}
                key = f"{r['name']}::{sec['section_id']}"
                default_val = float(sec["earned_points"])
                new_val = st.number_input(
                    f"Override score for {sec['section_id']} (0–{sec['total_points']})",
                    min_value=0.0,
                    max_value=float(sec["total_points"]),
                    value=st.session_state["overrides"].get(key, default_val),
                    step=0.5,
                    key=f"override_{key}"
                )
                st.session_state["overrides"][key] = new_val
                total_earned_override += float(new_val)

        st.caption(
            f"Total (LLM): **{r['total']['earned']:.2f} / {total_max:.2f}**  •  "
            f"Total (with overrides): **{total_earned_override:.2f} / {total_max:.2f}**"
        )

st.divider()
st.subheader("Next")
st.markdown(
    "- Export per-student reports (PDF/HTML) and a CSV of final scores.\n"
    "- Add rubric-level few-shots in `args_json` to tighten consistency.\n"
    "- Optional: add discrepancy flags if you later enable any deterministic checks."
)