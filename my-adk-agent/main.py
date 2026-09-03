from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)


import streamlit as st
from ui.chat_interface import run_chat_interface
from ui.login import login

"""
Executes main streamlit pages, the login page running login() and the chat interface running run_chat_interface().
"""
if __name__ == "__main__":
    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False
        login()
        st.session_state.logged_in = True
        run_chat_interface()
    else:
        if st.session_state.logged_in == False:
            login()
        st.session_state.logged_in = True
        run_chat_interface()

    

    