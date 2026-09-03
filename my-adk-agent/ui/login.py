import streamlit as st
from google_oauth.oauth_login import OAuthLogin, fetch_creds
def login():
    authenticator = OAuthLogin()
    """
    Logs in the user and authenticates their credentials.
    """
    st.set_page_config(page_title="WorkFlow", layout="wide") # Configures the browser tab title and page layout.
    st.title("Automated Scheduling Assistant(Powered by ADK & Gemini)") # Main title of the app.
    st.markdown("This application uses the Google Agent Development Kit (ADK) to automate your scheduling, implement + critique planning.") # Descriptive text.
    st.divider()

    authenticator.login()
    authenticator.get_creds()
    