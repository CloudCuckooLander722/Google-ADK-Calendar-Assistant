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
from ui.chat_interface import run_chat_interface

# Query parameters Google adds when it sends the user back after sign-in.
GOOGLE_REDIRECT_PARAMS = ("code", "state", "scope", "iss", "authuser", "prompt", "hd")

def login():
    """
    Loads the login link and downloads the credentials for further usage.
    Args: None
    Returns: None
    """

    st.set_page_config(
        page_title="Login Panel", 
        page_icon="🔒", 
        layout="centered"
    )

    authenticator = OAuthLogin()

    authenticator.login_link()
    authenticator.download_creds()
    st.query_params["logged_in"] = "True"


def login_page():
    """
    Checks st.query_params to see whether st.query_params["logged_in"] is true or false, before logging in.
    """
    if st.query_params.get("logged_in") == "False":
        login()
    else:
        run_chat_interface()

    st.rerun()