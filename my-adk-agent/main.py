from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)

import streamlit as st

from ui.home import show_home_page
from ui.login import login_page
from ui.privacy import show_privacy_page


def main():
    """
    Main function that runs the Streamlit application.

    The home page is the landing page at "/". Its sign-in links go to /login_page,
    where login_page shows either the sign-in card or the chat workspace.
    """
    home_page = st.Page(show_home_page, title="Home", icon="🏠", default=True)
    workspace_page = st.Page(login_page, title="WorkFlow", icon="💬", url_path="login_page")
    # Public at /privacy whether or not the visitor is signed in: Google's OAuth
    # verification requires a privacy policy reachable without signing in.
    privacy_page = st.Page(show_privacy_page, title="Privacy Policy", icon="🔒", url_path="privacy")

    # Each page draws its own navigation (the home page header, the login
    # card's links), so Streamlit's page menu is hidden.
    pg = st.navigation([home_page, workspace_page, privacy_page], position="hidden")

    # Google sends the user back to OAUTH_REDIRECT_URI, the site root (the home
    # page), with ?code=...&state=...: hand those to the login page so the
    # sign-in completes there.
    if pg.url_path == home_page.url_path and "code" in st.query_params:
        st.switch_page(workspace_page, query_params=st.query_params.to_dict())

    pg.run()


if __name__ == "__main__":
    main()
