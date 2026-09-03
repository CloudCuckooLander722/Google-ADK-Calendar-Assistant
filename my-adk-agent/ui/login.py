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
from google_oauth.oauth_login import OAuthLogin

def login():
    authenticator = OAuthLogin()
    """
    Logs in the user and authenticates their credentials.
    """

    authenticator.login()          # may call st.stop() here if no ?code= yet
    creds = authenticator.get_creds()
    if creds is not None:
        st.session_state.logged_in = True



   