import streamlit as st
from services.adk_service import initialize_adk, run_adk_sync
from config.settings import MESSAGE_HISTORY_KEY, get_api_key
from streamlit_js_eval import streamlit_js_eval
from streamlit_calendar import calendar as st_calendar

from pathlib import Path
import sys
import datetime

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)

from ui.fetch_timezone import fetch_timezone
from google_oauth.oauth_login import get_calendar_service

# One height for both panels so the chat box and the calendar always match.
PANEL_HEIGHT = 620
# Space the panel's own padding, border, input bar and gap take, so the
# scrolling message log plus the input fit inside PANEL_HEIGHT without a
# second scrollbar on the panel itself.
CHAT_LOG_HEIGHT = PANEL_HEIGHT - 108

# The calendar renders in its own iframe, so it can't inherit the page fonts.
CALENDAR_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&display=swap');
.fc { font-family: 'Lato', Arial, sans-serif; }
.fc .fc-toolbar-title { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; }
"""


def _google_calendar_month_events(max_results: int = 250):
    """
    Return this month's Google Calendar events as event dicts shaped for the
    streamlit-calendar (FullCalendar) widget: {"title", "start", "end", "allDay"}.
    Empty list if the user isn't signed in or the calendar can't be reached.
    """
    service = get_calendar_service()
    if service is None:
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    try:
        events_result = service.events().list(
            calendarId="primary",
            timeMin=month_start.isoformat(),
            timeMax=month_end.isoformat(),
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        raw_events = events_result.get("items", [])
    except Exception:
        return []

    calendar_events = []
    for event in raw_events:
        start = event.get("start", {})
        end = event.get("end", {})
        calendar_events.append({
            "title": event.get("summary") or "Untitled event",
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "allDay": "date" in start,
        })
    return calendar_events



def run_chat_interface():
    """
    Sets up and runs the Streamlit web application for the ADK chat assistant.
    """
    st.set_page_config(page_title="WorkFlow Workspace", layout="wide")

    api_key = get_api_key()
    if not api_key:
        st.error("⚠️ Action Required: Google API Key Not Found or Invalid! Please set GOOGLE_API_KEY in your .env file. ⚠️")
        st.stop()

    # Figma / Notion workspace styling.
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,300;0,400;0,700;0,900;1,300;1,400;1,700;1,900&display=swap');

        /* Helvetica isn't a Google Font, so it can't be loaded: it only renders
           where installed locally (mostly Apple devices) and falls back to Arial. */
        .stApp {
            background: #f6f7f9;
            font-family: 'Lato', Arial, sans-serif;
        }

        .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6 {
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
        }

        /* Streamlit's theme font (config.toml) is set directly on these text
           elements, so the .stApp value above isn't inherited by them. */
        .stApp p, .stApp li, .stApp td, .stApp th, .stApp blockquote, .stApp textarea {
            font-family: 'Lato', Arial, sans-serif;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Initialize ADK runner and session ID (cached to run only once).
    adk_runner, current_session_id = initialize_adk()
    fetch_timezone()

    # Left column: actual chat panel with messages and input in 40%
    chat_col, calendar_col = st.columns([4, 6])

    with chat_col:
        st.markdown("#### Assistant")
        # Initialize chat history.
        if MESSAGE_HISTORY_KEY not in st.session_state:
            st.session_state[MESSAGE_HISTORY_KEY] = []

        # Outer panel is the same height as the calendar. Inside it, the
        # message log scrolls and the input stays pinned beneath the log -
        # scoped to chat only, the calendar is untouched.
        chat_panel = st.container(height=PANEL_HEIGHT)
        with chat_panel:
            chat_log = st.container(height=CHAT_LOG_HEIGHT, border=False)

            with chat_log:
                # Display existing chat messages.
                for message in st.session_state[MESSAGE_HISTORY_KEY]:
                    with st.chat_message(message["role"]):
                        st.markdown(message["content"])

            # Handle new user input.
            prompt = st.chat_input("Ask WorkFlow Assistant to plan or schedule...")

        if prompt:
            # Append user message.
            st.session_state[MESSAGE_HISTORY_KEY].append({"role": "user", "content": prompt})
            with chat_log:
                with st.chat_message("user"):
                    st.write(prompt)

                with st.chat_message("assistant"):
                    empty_response = st.empty()
                    with st.spinner("Assistant is thinking..."):
                        agent_response = run_adk_sync(adk_runner, current_session_id, prompt)
                        empty_response.markdown(agent_response)

            st.session_state[MESSAGE_HISTORY_KEY].append({"role": "assistant", "content": agent_response})

    with calendar_col:
        st.markdown("#### Google Calendar")
        calendar_events = _google_calendar_month_events()
        st_calendar(
            events=calendar_events,
            options={
                "initialView": "dayGridMonth",
                "headerToolbar": {
                    "left": "prev,next today",
                    "center": "title",
                    "right": "dayGridMonth,timeGridWeek,listWeek",
                },
                "height": PANEL_HEIGHT,
            },
            custom_css=CALENDAR_CSS,
            key="google_calendar_view",
        )
        if not calendar_events:
            st.caption("No events found for this month, or you're not signed in to Google Calendar yet.")

