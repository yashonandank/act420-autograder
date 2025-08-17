import time
import streamlit as st
import bcrypt

SESSION_KEY = "auth"
SESSION_TTL = 60 * 60  # 60 minutes

def verify_password(username: str, password: str) -> bool:
    users = st.secrets.get("users", {})
    if username not in users:
        return False
    stored_hash = users[username].encode()
    try:
        return bcrypt.checkpw(password.encode(), stored_hash)
    except Exception:
        return False

def require_login():
    auth = st.session_state.get(SESSION_KEY)
    now = time.time()
    if auth and now - auth["ts"] < SESSION_TTL:
        st.session_state[SESSION_KEY]["ts"] = now
        return True

    st.title("🔐 Login")
    with st.form("login"):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in")

    if ok and verify_password(u, p):
        st.session_state[SESSION_KEY] = {"user": u, "ts": time.time()}
        st.rerun()
    st.stop()