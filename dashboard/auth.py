"""Login for the dashboard's database mode.

Uses the existing Admin/Operator authentication in database/database_service.py.
Mock and api modes keep the demo role selector and never import this module's database side.
"""
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)


def authenticate(username, password):
    """The user's role ("Admin" or "Operator"), or None if the login is wrong or the user is inactive."""
    if not username or not password:
        return None
    from database import database_service  # imported here so mock mode needs no database

    user = database_service.authenticate_user(username, password)
    return user["role"] if user else None


def sidebar_login(st):
    """Sign-in form / status for the sidebar. Stores st.session_state["role"] and ["user"] on success."""
    ss = st.session_state
    if ss.get("role"):
        st.caption(f"Signed in as **{ss.get('user', '?')}** ({ss['role']})")
        if st.button("Sign out", key="sign_out"):
            ss.pop("role", None)
            ss.pop("user", None)
            st.rerun()
        return
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            try:
                role = authenticate(username, password)
            except Exception as exc:  # noqa: BLE001 - e.g. database missing: show it, do not crash the page
                st.error(f"Sign-in is unavailable: {exc}")
                return
            if role:
                ss["role"], ss["user"] = role, username
                st.rerun()
            else:
                st.error("Incorrect username or password.")
