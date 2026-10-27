"""
Cookies meant to store data like pcke_verifier and oauth_state for the OAuth flow and credentials retrieval. This service is responsible for managing cookies, including setting, getting, and deleting them as needed during the authentication process.
"""
import streamlit as st
from streamlit.errors import StreamlitAPIException
from streamlit_cookies_controller import CookieController

# The session_state key CookieController keeps the browser's cookies under.
COOKIES_KEY = "cookies"


class _Cookies(CookieController):
    """
    CookieController that can be made more than once per run.

    The first one in a session asks the browser for its cookies (an empty dict
    until the browser answers and Streamlit reruns). Later ones reuse what is
    in st.session_state. CookieController writes that back to session_state so
    it outlives runs where the widget isn't drawn; in the run that drew the
    widget Streamlit forbids the write, and it isn't needed there.
    """

    def __init__(self):
        if COOKIES_KEY not in st.session_state:
            super().__init__(key=COOKIES_KEY)
        else:
            self._CookieController__key = COOKIES_KEY
            self._CookieController__cookies = st.session_state[COOKIES_KEY]
        # Anything but a dict (the component hasn't answered) means no cookies yet.
        if not isinstance(self._CookieController__cookies, dict):
            self._CookieController__cookies = {}
        try:
            st.session_state[COOKIES_KEY] = self._CookieController__cookies
        except StreamlitAPIException:
            pass


class _SessionCookies:
    """
    The browser cookies of whoever's Streamlit session is running right now.

    Modules are imported once per server process and shared by every browser,
    so a CookieController can't be made once at import: it would hand the
    first visitor's cookies to everyone. Instead, calling controller() or any
    method on it (controller.get, controller.set, ...) uses the current
    session's cookies.
    """

    def __call__(self) -> CookieController:
        return _Cookies()

    def __getattr__(self, name):
        return getattr(_Cookies(), name)


controller = _SessionCookies()
