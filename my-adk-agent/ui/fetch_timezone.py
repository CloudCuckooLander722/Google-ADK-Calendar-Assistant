from streamlit_js_eval import streamlit_js_eval
import streamlit as st

def fetch_timezone():
    """
    Fetches the user's timezone using JavaScript and stores it in Streamlit's session state.
    """
    if "timezone" not in st.session_state:
        # Use streamlit_js_eval to execute JavaScript and get the timezone
        timezone = streamlit_js_eval(
            js_expressions="Intl.DateTimeFormat().resolvedOptions().timeZone",
            key="i_love_gilfoyle",
        )
        if timezone:
            st.session_state.timezone = timezone

        return timezone