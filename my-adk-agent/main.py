import streamlit as st
from ui.chat_interface import run_chat_interface
from ui.login import login
from google_oauth.oauth_login import OAuthLogin
"""
Executes main streamlit pages, the login page running login() and the chat interface running run_chat_interface().
"""
if __name__ == "__main__":
    if "connected" not in st.session_state:
        st.session_state["connected"] = False
        login()
        st.session_state["connected"] = True
    else:
        run_chat_interface()
    