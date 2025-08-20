# app.py
from __future__ import annotations
import io
import json
import time
import zipfile
import re
from typing import Dict, Any, List, Tuple

import pandas as pd
import streamlit as st

from src.rubric_schema import load_rubric_json, load_rubric_excel
from src.segmentor import split_sections
from src.notebook_exec import run_ipynb_bytes

# ----------------------------
# Page, session, and login
# ----------------------------
st.set_page_config(layout="wide", page_title="Analytics Autograder")

SESSION_KEY = "auth"
SESSION_TTL = 60 * 60  # 60 minutes

def _require_login():
    auth = st.session_state.get(SESSION_KEY)
    now = time.time()
    if auth and (now - auth["ts"] < SESSION_TTL):
        st.session_state[SESSION_KEY]["ts"] = now
        return True

    st.title("🔐 Login")
    users = st.secrets.get("users", {})
    with st.form("login"):
        u = st.text_input("Username", value="")
        p = st.text_input("Password", type="password", value="")
        ok = st.form_submit_button("Sign in")
    if ok and u in users and p == str(users[u]):
        st.session_state[SESSION_KEY] = {"user": u, "ts": time.time()}
        st.success(f"Welcome, {u}!")
        st.rerun()
    st.stop()

_require_login()
with st.sidebar:
    st.caption("Session")
    if st.button("Log out"):
        st.session_state.pop(SESSION_KEY, None)
        st.rerun()

st.title("📊 Analytics Notebook Autograder")

# ----------------------------
# Shared state helpers
# ----------------------------
def ss_get(key, default):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]

# central stores
rubric: Dict[str, Any] = ss_get("rubric", {})
files_buf: Dict[str, bytes] = ss_get("files_buf", {})        # filename -> bytes
roster_df: pd.DataFrame | None = ss_get("roster_df", None)   # optional mapping
mapping_df: pd.DataFrame = ss_get("mapping_df", pd.DataFrame(columns=["filename","student_id","student_name"]))
executions: Dict[str, Dict[str, Any]] = ss_get("executions", {})  # student_id -> exec info
llm_results: Dict[str, Dict[str, Any]] = ss_get("llm_results", {})# student_id -> graded
overrides: Dict[str, float] = ss_get("overrides", {})             # f"{student_id}::{section_id}" -> score

# ----------------------------
# Utilities
# ----------------------------
_FILENAME_STUDENT_PATTERNS = [
    # Try common LMS exports:
    r"^(?P<last>[^,_-]+),\s*(?P<first>[^_-]+)\s*-\s*(?P<id>[A-Za-z0-9._-]+).*\.ipynb$",
    r"^(?P<id>\d+)[-_].*\.ipynb$",
    r"^(?P<first>[A-Za-z]+)[-_](?P<last>[A-Za-z]+)[-_](?P<id>[A-Za-z0-9]+).*\.ipynb$",
    r"^(?P<id>[A-Za-z0-9._-]+).*\.ipynb$",
]

def guess_student_from_filename(name: str) -> Tuple[str,str]:
    """Return (student_id, student_name) best-effort from filename."""
    for pat in _FILENAME_STUDENT_PATTERNS:
        m = re.match(pat, name, flags=re.IGNORECASE)
        if m:
            gid = (m.groupdict().get("id") or "").strip()
            first = (m.groupdict().get("first") or "").strip().title()
            last  = (m.groupdict().get("last") or "").strip().title()
            sname = " ".join(x for x in [first, last] if x).strip()
            if not sname:
                sname = gid
            if not gid:
                gid = sname
            return (gid, sname)
    base = name.rsplit("/",1)[-1]
    return (base.replace(".ipynb",""), base.replace(".ipynb",""))

def collect_ipynbs(upload) -> List[tuple[str, bytes]]:
    if upload.name.lower().endswith(".ipynb"):
        return [(upload.name, upload.getvalue())]
    out = []
    with zipfile.ZipFile(io.BytesIO(upload.getvalue())) as z:
        for nm in z.namelist():
            if nm.lower().endswith(".ipynb") and not nm.endswith("/"):
                out.append((nm.split("/")[-1], z.read(nm)))
    return out

def ensure_openai_client():
    if "openai" not in st.secrets or "api_key" not in st.secrets["openai"]:
        st.error("OpenAI key missing in secrets. Add [openai] api_key.")
        st.stop()
    from openai import OpenAI
    return OpenAI(api_key=st.secrets["openai"]["api_key"]), st.secrets["openai"].get("model","gpt-4o-mini")

# ----------------------------
# Tabs
# ----------------------------
tab_rubric, tab_subs, tab_run, tab_grade, tab_reports, tab_analytics = st.tabs(
    ["📋 Rubric", "📥 Submissions", "⚙️ Run", "🧠 Grade", "📄 Reports", "📈 Analytics"]
)

# --------- Rubric tab ----------
with tab_rubric:
    st.subheader("Upload / Edit Rubric")
    st.caption("Upload Excel (Sections & Criteria sheets) or JSON.")
    up = st.file_uploader("Rubric file", type=["xlsx","json"], key="rubric_file")
    if up:
        try:
            data = load_rubric_json(up) if up.name.endswith(".json") else load_rubric_excel(up)
            st.session_state["rubric"] = data
            rubric = data
            st.success("Rubric loaded ✅")
        except Exception as e:
            st.error(f"Failed to load rubric: {e}")

    if rubric:
        cols = st.columns([1,2,1])
        with cols[0]:
            st.metric("Sections", len(rubric["sections"]))
        with cols[2]:
            total = sum(s.get("points",0) or sum(c.get("max",0) for c in s.get("criteria",[])) for s in rubric["sections"])
            st.metric("Total Points", total)

        # lightweight in-app edit: rename titles and point caps
        editable = []
        for s in rubric["sections"]:
            editable.append({
                "section_id": s["id"],
                "title": s.get("title",""),
                "points": float(s.get("points",0) or 0.0),
                "criteria_count": len(s.get("criteria",[]))
            })
        edf = st.data_editor(pd.DataFrame(editable), hide_index=True, key="rubric_editor")
        if st.button("Apply changes to rubric titles/points"):
            # merge back
            by_id = {row["section_id"]: row for _, row in edf.iterrows()}
            for s in rubric["sections"]:
                if s["id"] in by_id:
                    s["title"] = by_id[s["id"]]["title"]
                    s["points"] = float(by_id[s["id"]]["points"])
            st.success("Applied edits.")

        st.download_button(
            "⬇️ Download rubric (JSON snapshot)",
            data=json.dumps(rubric, indent=2).encode("utf-8"),
            file_name="rubric_snapshot.json",
            mime="application/json"
        )

# --------- Submissions tab ----------
with tab_subs:
    st.subheader("Upload student notebooks (single or zip)")
    up_nb = st.file_uploader("Notebook or ZIP", type=["ipynb","zip"], key="subs_file")
    if up_nb:
        pairs = collect_ipynbs(up_nb)
        # store in files_buf
        for nm, b in pairs:
            files_buf[nm] = b
        st.success(f"Loaded {len(pairs)} notebook(s).")
    st.caption("Optional: roster CSV with columns `student_id, student_name` for mapping/validation.")
    up_roster = st.file_uploader("Roster CSV", type=["csv"], key="roster_csv")
    if up_roster:
        try:
            df = pd.read_csv(up_roster)
            assert {"student_id","student_name"}.issubset(df.columns)
            st.session_state["roster_df"] = df.copy()
            roster_df = df
            st.success(f"Roster loaded ({len(df)} students).")
        except Exception as e:
            st.error(f"Roster parse failed: {e}")

    # Build/refresh mapping table
    if files_buf:
        rows = []
        for fn in sorted(files_buf.keys()):
            sid, sname = guess_student_from_filename(fn)
            rows.append({"filename": fn, "student_id": sid, "student_name": sname})
        mdf = pd.DataFrame(rows)
        # If roster provided, try to align names/ids
        if roster_df is not None:
            # basic left join by student_id if it looks present
            merged = mdf.merge(roster_df, on="student_id", how="left", suffixes=("","_roster"))
            merged["student_name"] = merged["student_name_roster"].fillna(merged["student_name"])
            merged = merged.drop(columns=[c for c in merged.columns if c.endswith("_roster")])
            mdf = merged

        st.markdown("#### Map files to students")
        mapping_df = st.data_editor(mdf, num_rows="dynamic", use_container_width=True, key="map_editor")
        st.session_state["mapping_df"] = mapping_df

        # quick validation
        dup_ids = mapping_df["student_id"][mapping_df["student_id"].duplicated(keep=False)]
        if len(dup_ids) > 0:
            st.warning(f"Duplicate student_id values found: {sorted(set(dup_ids))}. You can still proceed, but reports/CSV will merge by the last run.")

# --------- Run tab ----------
with tab_run:
    st.subheader("Execute notebooks")
    if not mapping_df.empty:
        st.caption("Optional: assignment data & requirements for imports/relative paths.")
        data_zip_file = st.file_uploader("Data ZIP", type=["zip"], key="datazip")
        reqs_file = st.file_uploader("requirements.txt", type=["txt"], key="reqs")

        colA, colB, colC = st.columns([1,1,2])
        with colA:
            cell_timeout = st.number_input("Cell timeout (sec)", min_value=30, max_value=600, value=120, step=10)
        with colB:
            retry_timeout = st.checkbox("Auto-retry on timeout", value=True)
        with colC:
            skip_tags_str = st.text_input("Skip cells with tags (comma-separated)", value="skip_autograde,long")
        skip_tags = [s.strip() for s in skip_tags_str.split(",") if s.strip()]

        if st.button("▶️ Run all mapped notebooks", type="primary"):
            data_zip_bytes = data_zip_file.getvalue() if data_zip_file else None
            req_bytes = reqs_file.getvalue() if reqs_file else None
            executions.clear()
            progress = st.progress(0.0)
            items = list(mapping_df.to_dict(orient="records"))
            for i, row in enumerate(items, start=1):
                fn = row["filename"]; sid = str(row["student_id"]); sname = row["student_name"]
                raw = files_buf.get(fn)
                if not raw:
                    continue
                try:
                    res = run_ipynb_bytes(
                        raw,
                        timeout_per_cell=int(cell_timeout),
                        data_zip=data_zip_bytes,
                        extra_requirements_txt=req_bytes,
                        probes=None,
                        retry_on_timeout=retry_timeout,
                        skip_tags=skip_tags,
                    )
                except TypeError:
                    res = run_ipynb_bytes(raw, timeout_per_cell=int(cell_timeout))
                    res.probe_results = {}

                spans = split_sections(res.executed_nb)
                executions[sid] = {
                    "student_id": sid,
                    "student_name": sname,
                    "filename": fn,
                    "duration_s": res.duration_s,
                    "errors": res.errors,
                    "html": res.html,
                    "sections": spans,
                    "executed_nb": res.executed_nb,
                }
                progress.progress(i/len(items))
            st.success(f"Executed {len(executions)} notebook(s).")

    # quick preview
    if executions:
        sid = st.selectbox("Preview student", options=list(executions.keys()), format_func=lambda k: f"{executions[k]['student_name']} ({k})")
        info = executions[sid]
        cols = st.columns([1,2,2])
        with cols[0]:
            st.caption(f"⏱ {info['duration_s']:.1f}s")
            if info["errors"]:
                st.error(f"{len(info['errors'])} error(s)")
            else:
                st.success("No execution errors")
            st.write("Sections detected:")
            st.code(", ".join(info["sections"].get("_order", [])) or "—", language="text")
        with cols[1]:
            st.markdown("**Notebook preview**")
            st.components.v1.html(info["html"], height=500, scrolling=True)
        with cols[2]:
            st.markdown("**Raw section spans**")
            st.json(info["sections"])

# --------- Grade tab ----------
with tab_grade:
    st.subheader("LLM grading per section")
    if not rubric:
        st.info("Upload a rubric in the **Rubric** tab.")
    elif not executions:
        st.info("Run notebooks in the **Run** tab.")
    else:
        OPENAI_OK = "openai" in st.secrets and "api_key" in st.secrets["openai"]
        if not OPENAI_OK:
            st.warning("Add your OpenAI key in secrets: [openai] api_key")
        grade_disabled = not OPENAI_OK

        if st.button("🤖 Grade all students (LLM)", type="primary", disabled=grade_disabled):
            client, model = ensure_openai_client()
            import importlib
            llm_grader = importlib.import_module("src.llm_grader")
            build_section_context = llm_grader.build_section_context
            grade_section_llm   = llm_grader.grade_section_llm

            llm_results.clear()
            students = list(executions.values())
            progress = st.progress(0.0)
            for i, info in enumerate(students, start=1):
                spans = info["sections"]
                nb = info["executed_nb"]; html = info["html"]
                per_sections = []
                for qid in spans.get("_order", []):
                    ctx = build_section_context(nb, spans[qid], html)
                    graded = grade_section_llm(client, model, rubric, qid, ctx, temperature=0.0)
                    per_sections.append(graded)
                total_max = sum(s["total_points"] for s in per_sections)
                total_earned = sum(s["earned_points"] for s in per_sections)
                llm_results[info["student_id"]] = {
                    "student_id": info["student_id"],
                    "student_name": info["student_name"],
                    "filename": info["filename"],
                    "sections": per_sections,
                    "total": {"max": total_max, "earned": total_earned},
                }
                progress.progress(i/len(students))
            st.success(f"Graded {len(llm_results)} student(s).")

        # viewer + edits
        if llm_results:
            sid = st.selectbox("Review student", options=list(llm_results.keys()),
                               format_func=lambda k: f"{llm_results[k]['student_name']} ({k})", key="grade_review_sid")
            res = llm_results[sid]
            st.caption(f"Total (LLM): **{res['total']['earned']:.2f} / {res['total']['max']:.2f}**")

            tot_override = 0.0
            for sec in res["sections"]:
                with st.expander(f"{sec['section_id']} — {sec['earned_points']:.2f} / {sec['total_points']:.2f}", expanded=False):
                    df = pd.DataFrame([{
                        "criterion_id": c["criterion_id"],
                        "label": c["label"],
                        "score": c["score"],
                        "max": c["max"],
                        "why": c["rationale"],
                        "tip": c.get("improvement_tip",""),
                    } for c in sec["criteria"]])
                    st.dataframe(df, hide_index=True, use_container_width=True)
                    st.markdown("**Overall comment**")
                    st.write(sec.get("overall_comment",""))

                    key = f"{sid}::{sec['section_id']}"
                    default_val = float(sec["earned_points"])
                    new_val = st.number_input(
                        f"Override score for {sec['section_id']} (0–{sec['total_points']})",
                        min_value=0.0, max_value=float(sec["total_points"]),
                        value=overrides.get(key, default_val), step=0.5, key=f"ov_{key}"
                    )
                    overrides[key] = float(new_val)
                    tot_override += float(new_val)

            st.caption(
                f"Total (with overrides): **{tot_override:.2f} / {res['total']['max']:.2f}**"
            )

# --------- Reports tab ----------
with tab_reports:
    st.subheader("Per-student reports & class CSV")
    if not llm_results:
        st.info("Grade students in the **Grade** tab first.")
    else:
        # Build data for CSV and enable per-student HTML download
        rows = []
        for sid, res in llm_results.items():
            total_override = 0.0
            for sec in res["sections"]:
                key = f"{sid}::{sec['section_id']}"
                total_override += float(overrides.get(key, sec["earned_points"]))
            rows.append({
                "student_id": sid,
                "student_name": res["student_name"],
                "filename": res["filename"],
                "score_llm": res["total"]["earned"],
                "score_override": total_override,
                "score_max": res["total"]["max"],
            })
        csv_df = pd.DataFrame(rows).sort_values(["student_name","student_id"])
        st.dataframe(csv_df, use_container_width=True, hide_index=True)

        st.download_button(
            "⬇️ Download class scores (CSV)",
            data=csv_df.to_csv(index=False).encode("utf-8"),
            file_name="class_scores.csv",
            mime="text/csv"
        )

        st.markdown("---")
        sid = st.selectbox("Generate report for student", options=list(llm_results.keys()),
                           format_func=lambda k: f"{llm_results[k]['student_name']} ({k})", key="report_sid")
        res = llm_results[sid]
        # assemble simple HTML
        html_parts = [
            f"<h2>Report — {res['student_name']} ({sid})</h2>",
            f"<p><b>File:</b> {res['filename']}</p>",
            f"<p><b>Total (LLM):</b> {res['total']['earned']:.2f} / {res['total']['max']:.2f}</p>",
        ]
        total_override = 0.0
        for sec in res["sections"]:
            key = f"{sid}::{sec['section_id']}"
            sec_score = overrides.get(key, sec["earned_points"])
            total_override += float(sec_score)
            html_parts.append(f"<h3>{sec['section_id']} — {sec_score:.2f}/{sec['total_points']:.2f}</h3>")
            html_parts.append("<ul>")
            for c in sec["criteria"]:
                html_parts.append(
                    f"<li><b>{c['label']}</b>: {c['score']:.2f}/{c['max']:.2f}<br/><i>{c['rationale']}</i></li>"
                )
            html_parts.append("</ul>")
            if sec.get("overall_comment"):
                html_parts.append(f"<p><b>Overall:</b> {sec['overall_comment']}</p>")
        html_parts.append(f"<p><b>Total (with overrides):</b> {total_override:.2f} / {res['total']['max']:.2f}</p>")
        html_report = "\n".join(html_parts)
        st.download_button(
            "⬇️ Download student report (HTML)",
            data=html_report.encode("utf-8"),
            file_name=f"report_{sid}.html",
            mime="text/html"
        )

# --------- Analytics tab ----------
with tab_analytics:
    st.subheader("Class-wide analytics")
    if not llm_results:
        st.info("Grade students in the **Grade** tab.")
    else:
        rows = []
        for sid, res in llm_results.items():
            for sec in res["sections"]:
                key = f"{sid}::{sec['section_id']}"
                score = overrides.get(key, sec["earned_points"])
                rows.append({
                    "student_id": sid,
                    "student_name": res["student_name"],
                    "section_id": sec["section_id"],
                    "score": float(score),
                    "max": float(sec["total_points"]),
                })
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        # quick aggregates
        agg = df.groupby("section_id").agg(
            n=("score","count"),
            mean=("score","mean"),
            max=("max","first")
        ).reset_index()
        agg["pct_mean"] = (agg["mean"] / agg["max"]) * 100.0
        st.markdown("#### Section summary")
        st.dataframe(agg, use_container_width=True, hide_index=True)

        # streamlit’s built-in charts for quick viz
        st.markdown("#### Mean % by section")
        st.bar_chart(agg.set_index("section_id")["pct_mean"])