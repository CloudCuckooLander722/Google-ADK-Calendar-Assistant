"""
Cookies meant to store data like pcke_verifier and oauth_state for the OAuth flow and credentials retrieval. This service is responsible for managing cookies, including setting, getting, and deleting them as needed during the authentication process.
"""
from streamlit_cookies_controller import CookieController

controller = CookieController()
