import os
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)

import streamlit as st
from authentication_login.oauth_login import OAuthLogin
from services.cookie_service import controller
from services.api_services import check_creds
from ui.chat_interface import run_chat_interface, restore_signed_in_user

# Query parameters Google adds when it sends the user back after sign-in.
GOOGLE_REDIRECT_PARAMS = ("code", "state", "scope", "iss", "authuser", "prompt", "hd")
# How many runs login() waits for the browser's cookies after Google's redirect.
OAUTH_COOKIE_WAITS_KEY = "oauth_cookie_waits"
MAX_OAUTH_COOKIE_WAITS = 2
# The user_id whose stored credentials this session already found working.
CREDS_CHECKED_KEY = "creds_checked_for"


def login(authenticator: OAuthLogin):
    """
    Finishes a sign-in Google redirected back here (download_creds), or shows
    the login link.
    Args: authenticator
    Returns: None
    """

    st.set_page_config(
        page_title="Login Panel",
        page_icon="🔒",
        layout="centered"
    )

    if "code" in st.query_params:
        # Google's redirect is a fresh page load: the cookie component sends
        # the browser's cookies (with the oauth_state and code_verifier to
        # check the code against) only after its first run, then reruns.
        waits = st.session_state.get(OAUTH_COOKIE_WAITS_KEY, 0)
        if not controller().getAll() and waits < MAX_OAUTH_COOKIE_WAITS:
            st.session_state[OAUTH_COOKIE_WAITS_KEY] = waits + 1
            st.stop()

        # Before login_link: it saves a new oauth_state and code_verifier.
        creds = authenticator.download_creds()

        # The code is single use: a refresh mustn't try to exchange it again.
        for param in GOOGLE_REDIRECT_PARAMS:
            st.query_params.pop(param, None)
        if creds is not None:
            st.query_params["logged_in"] = "True"
            # Shown in this run rather than after st.rerun(): the user_id
            # cookie is written by a component that a rerun could drop
            # before the browser runs it.
            run_chat_interface()
            return

    st.title("WorkFlow Assistant")
    st.write("Sign in with Google to plan your day with your calendar and tasks.")
    authenticator.login_link()
    st.caption("[Privacy Policy](/privacy) · [Terms of Service](/terms_of_service)")


def _has_credentials(user_id: str | None) -> bool:
    """
    Whether user_id has Google credentials that still work. A user_id cookie
    alone isn't enough: it can outlive the stored credentials (deleted, revoked,
    or saved by an older version of the app), which would show an empty chat
    and calendar with no way to sign in. Checked once per session.
    """
    if not user_id:
        return False
    if st.session_state.get(CREDS_CHECKED_KEY) == user_id:
        return True
    if check_creds() is None:
        # Forget the stale id, so nothing else this run uses it.
        st.session_state.pop("user_id", None)
        return False
    st.session_state[CREDS_CHECKED_KEY] = user_id
    return True


def login_page(authenticator: OAuthLogin | None = None):
    """
    Checks st.query_params to see whether st.query_params["logged_in"] is true or false, before logging in.
    Shows the chat interface to a signed-in user (also after a refresh, from
    their user_id cookie), and login() to everyone else.
    """
    # Who is signed in comes from the session or the cookie, never the URL.
    st.query_params.pop("user_id", None)
    authenticator = authenticator or OAuthLogin()

    if (
        "code" not in st.query_params
        and st.query_params.get("logged_in") != "False"
        and _has_credentials(restore_signed_in_user())
    ):
        st.query_params["logged_in"] = "True"
        run_chat_interface()
    else:
        if "code" not in st.query_params:
            st.query_params["logged_in"] = "False"
        login(authenticator)
