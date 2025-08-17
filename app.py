# app.py
import json
import time
import pandas as pd
import streamlit as st
from jsonschema import validate, ValidationError

# ----------------------------
# Page setup
# ----------------------------
st.set_page_config(layout="wide", page_title="Analytics Autograder — Step 1")

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
            "`.streamlit/secrets.toml` (local) or the Cloud Secrets UI."
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
        if st.button("Log out"):
            st.session_state.pop(SESSION_KEY, None)
            st.rerun()

# Require login
check_login()
logout_button()

# ----------------------------
# Rubric schema + loaders
# ----------------------------
RUBRIC_SCHEMA = {
    "type": "object",
    "required": ["sections"],
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "points", "criteria"],
                "properties": {
                    "id": {"type": "string", "pattern": "^Q\\d+$"},
                    "title": {"type": "string"},
                    "points": {"type": "number", "minimum": 0},
                    "criteria": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "type", "max"],
                            "properties": {
                                "id": {"type": "string"},
                                "criterion": {"type": "string"},
                                "type": {
                                    "enum": [
                                        "columns",
                                        "row_count",
                                        "stat_range",
                                        "unique_count",
                                        "null_rate",
                                        "table_shape",
                                        "figure_exists",
                                        "llm_feedback",
                                    ]
                                },
                                "args": {"type": "object"},
                                "max": {"type": "number", "minimum": 0},
                                "auto_only": {"type": ["boolean", "null"]},
                                "bonus_cap": {"type": ["number", "null"]},
                            },
                        },
                    },
                },
            },
        }
    },
}

def load_rubric_json(file_obj):
    data = json.load(file_obj)
    validate(instance=data, schema=RUBRIC_SCHEMA)
    return data

def load_rubric_excel(file_obj):
    x = pd.ExcelFile(file_obj)
    sections = pd.read_excel(x, "Sections").fillna("")
    criteria = pd.read_excel(x, "Criteria").fillna("")
    sections["section_id"] = sections["section_id"].astype(str).str.strip()
    criteria["section_id"] = criteria["section_id"].astype(str).str.strip()

    out = {"sections": []}
    if "order" in sections.columns:
        sections = sections.sort_values("order")

    for _, s in sections.iterrows():
        sec_id = str(s["section_id"])
        pts = float(s.get("points", 0) or 0)
        title = str(s.get("title", ""))
        subset = criteria[criteria["section_id"] == sec_id]
        rows = []
        for _, c in subset.iterrows():
            args = {}
            sargs = str(c.get("args_json", "")).strip()
            if sargs:
                try:
                    args = json.loads(sargs)
                except Exception as e:
                    raise ValidationError(
                        f"Invalid args_json for criterion_id={c.get('criterion_id')}: {e}"
                    )
            rows.append(
                {
                    "id": str(c.get("criterion_id")),
                    "criterion": str(c.get("label", "")),
                    "type": str(c.get("type")),
                    "args": args,
                    "max": float(c.get("max_points", 0) or 0),
                    "auto_only": bool(c.get("auto_only"))
                    if str(c.get("auto_only", "")).strip() != ""
                    else None,
                    "bonus_cap": float(c.get("bonus_cap", 0))
                    if str(c.get("bonus_cap", "")).strip() != ""
                    else None,
                }
            )
        out["sections"].append(
            {"id": sec_id, "title": title, "points": pts, "criteria": rows}
        )

    validate(instance=out, schema=RUBRIC_SCHEMA)
    return out

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

# ----------------------------
# Main UI
# ----------------------------
st.title("📊 Analytics Notebook Autograder — Step 1")
st.caption("Login + rubric upload/validate/preview. (Execution & grading come next.)")

with st.expander("➡️ Excel rubric template (what columns it expects)"):
    st.write(
        "- **Sections** sheet: `section_id`, `title`, `points`, `order`\n"
        "- **Criteria** sheet: `section_id`, `criterion_id`, `label`, `type`, `args_json`, `max_points`, `auto_only`, `bonus_cap`\n"
        "- Allowed `type`: `columns`, `row_count`, `stat_range`, `unique_count`, `null_rate`, `table_shape`, `figure_exists`, `llm_feedback`"
    )

st.subheader("Upload Rubric")
rubric_file = st.file_uploader(
    "Upload `rubric.json` **or** an Excel rubric (Sections/Criteria sheets)",
    type=["json", "xlsx"],
)

if rubric_file:
    try:
        if rubric_file.name.endswith(".json"):
            rubric = load_rubric_json(rubric_file)
        else:
            rubric = load_rubric_excel(rubric_file)

        st.success("Rubric loaded and validated ✅")
        st.session_state["rubric"] = rubric
        preview_rubric(rubric)
    except Exception as e:
        st.error(f"Failed to load rubric: {e}")

st.divider()
st.subheader("Next steps")
st.markdown(
    "- **Step 2:** Add notebook execution and Q1/Q2 section splitting.\n"
    "- **Step 3:** Deterministic checks + optional LLM feedback.\n"
    "- **Step 4:** Gradescope-style review UI and student report export."
)