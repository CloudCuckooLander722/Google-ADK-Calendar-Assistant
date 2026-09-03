from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)

import streamlit as st
import extra_streamlit_components as stx
from ui.chat_interface import run_chat_interface
from ui.login import login
from ui.home import show_home_page

def main():
    """
    Main function to run the Streamlit application.
    """

    if "logged_in" not in st.session_state:
            st.session_state.logged_in = False
    
    login_page = st.Page(login, title="Login", icon="🔑")
    home_page = st.Page(show_home_page, title="Home", icon="🏠")
    chat_interface_page = st.Page(run_chat_interface, title="Chat Interface", icon="💬")

    if st.session_state.logged_in:
        pg = st.navigation({
             "Chat Interface": [chat_interface_page]
        }
        )
    else:
         pg = st.navigation({
              "Login": [login_page],
              "Home": [home_page]
         })

    pg.run()

if __name__ == "__main__":
     main()

    