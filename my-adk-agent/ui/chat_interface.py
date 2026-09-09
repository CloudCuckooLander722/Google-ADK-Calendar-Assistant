import streamlit as st
from services.adk_service import initialize_adk, run_adk_sync
from config.settings import MESSAGE_HISTORY_KEY, get_api_key
from streamlit_js_eval import streamlit_js_eval

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


def _safe_google_calendar_events(max_results: int = 5):
    """
    Return the upcoming live Google Calendar events for the signed-in user.
    If the user is not authenticated or no events are available, return []
    rather than crashing the page.
    """
    service = get_calendar_service()
    if service is None:
        return []

    try:
        time_min = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        events_result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return events_result.get("items", [])
    except Exception:
        return []


def _format_event_markdown(events):
    """
    Render the live Google Calendar events as plain markdown text blocks.
    This intentionally avoids custom HTML grids or widget-like visual output.
    """
    if not events:
        return "- No Google Calendar events found."

    lines = []
    for event in events:
        summary = event.get("summary") or "Untitled event"
        start = event.get("start", {})
        if start.get("dateTime"):
            try:
                value = start["dateTime"]
                dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
                time_label = dt.strftime("%a %H:%M")
            except Exception:
                time_label = start["dateTime"]
        elif start.get("date"):
            time_label = start["date"]
        else:
            time_label = "All day"

        lines.append(f"- {time_label} — {summary}")

    return "\n".join(lines)


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
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

        .stApp {
            background: #f6f7f9;
        }

        .workflow-workspace {
            background: #f6f7f9;
            min-height: 88vh;
            border-radius: 22px;
            border: 1px solid rgba(20,33,61,0.05);
            font-family: 'Inter', Arial, sans-serif;
            color: #172033;
            padding: 16px;
        }

        .workspace-topbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            padding: 6px 4px 16px;
            border-bottom: 1px solid rgba(31,45,70,0.08);
        }

        .workspace-brand {
            display: flex;
            align-items: center;
            gap: 10px;
            color: #14213d;
            font-weight: 900;
            font-size: 20px;
            letter-spacing: -0.035em;
        }

        .workspace-brand .mark {
            width: 34px;
            height: 34px;
            border-radius: 50%;
            background: #14213d;
            color: #eefcf8;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 16px;
        }

        .workspace-nav {
            display: flex;
            gap: 16px;
            align-items: center;
            color: #56616f;
            font-weight: 700;
            font-size: 12px;
        }

        .workspace-nav span {
            padding: 8px 10px;
            border-radius: 999px;
            background: #eef3f8;
        }

        .workspace-layout {
            display: grid;
            grid-template-columns: 40% 60%;
            gap: 14px;
            margin-top: 14px;
        }

        .chat-panel {
            background: #ffffff;
            border: 1px solid rgba(20,33,61,0.06);
            border-radius: 22px;
            box-shadow: 0 18px 50px rgba(31,45,70,0.06);
            padding: 16px;
            min-height: 620px;
            display: flex;
            flex-direction: column;
            background-image: radial-gradient(#eef3f8 1px, transparent 1px);
            background-size: 20px 20px;
        }

        .chat-panel-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding-bottom: 12px;
            border-bottom: 1px solid rgba(31,45,70,0.08);
        }

        .chat-panel-header .title {
            font-size: 17px;
            font-weight: 900;
            color: #14213d;
            letter-spacing: -0.02em;
        }

        .chat-panel-header .status {
            display: flex;
            align-items: center;
            gap: 8px;
            color: #005a4b;
            font-size: 11px;
            font-weight: 800;
        }

        .chat-panel-header .status:before {
            content: '';
            width: 7px;
            height: 7px;
            background: #005a4b;
            border-radius: 50%;
        }

        .chat-log {
            flex: 1;
            overflow: auto;
            padding: 12px 4px 14px;
        }

        .chat-log .stChatMessage {
            background: transparent;
        }

        .chat-input-wrap {
            border-top: 1px solid rgba(31,45,70,0.08);
            padding-top: 12px;
        }

        .calendar-panel {
            background: #ffffff;
            border-radius: 22px;
            border: 1px solid rgba(20,33,61,0.06);
            box-shadow: 0 18px 50px rgba(31,45,70,0.06);
            padding: 16px;
            min-height: 620px;
            display: flex;
            flex-direction: column;
        }

        .calendar-panel-head {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 2px 0 14px;
            border-bottom: 1px solid rgba(31,45,70,0.08);
        }

        .calendar-panel-head .label {
            color: #56616f;
            font-size: 11px;
            font-weight: 900;
            letter-spacing: 0.14em;
            text-transform: uppercase;
        }

        .calendar-panel-head .month {
            color: #14213d;
            font-size: 23px;
            font-weight: 900;
            letter-spacing: -0.035em;
        }

        .calendar-panel-head .controls {
            display: flex;
            gap: 8px;
        }

        .calendar-panel-head .controls button {
            border-radius: 50%;
            border: 1px solid rgba(20,33,61,0.14);
            background: #fff;
            color: #14213d;
            width: 34px;
            height: 34px;
            font-size: 16px;
            font-weight: 800;
        }

        .calendar-grid {
            margin-top: 16px;
            display: grid;
            grid-template-columns: repeat(7, minmax(40px, 1fr));
            gap: 8px;
            color: #56616f;
            font-size: 11px;
            font-weight: 800;
            text-align: center;
        }

        .calendar-grid .weekday {
            padding-bottom: 8px;
            color: #56616f;
            border-bottom: 1px solid rgba(31,45,70,0.08);
        }

        .calendar-grid .day {
            min-height: 44px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            border: 1px solid transparent;
            background: #f8f9fb;
            color: #14213d;
            font-weight: 800;
        }

        .calendar-grid .day.today {
            background: #14213d;
            color: #eefcf8;
            border-radius: 50%;
            width: 34px;
            height: 34px;
            margin: auto;
            min-height: unset;
            border: none;
        }

        .calendar-grid .day.event {
            background: #dcefed;
            color: #005a4b;
            border-color: rgba(0,90,75,0.20);
        }

        .calendar-events {
            margin-top: 16px;
            background: #eef3f8;
            border-radius: 18px;
            padding: 12px;
            border: 1px solid rgba(20,33,61,0.04);
        }

        .calendar-events .event-title {
            font-size: 12px;
            font-weight: 900;
            color: #14213d;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            margin-bottom: 10px;
        }

        .calendar-events .event-line {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 11px 0;
            border-top: 1px solid rgba(31,45,70,0.08);
            color: #56616f;
            font-size: 12px;
            font-weight: 700;
        }

        .calendar-events .event-line strong {
            color: #14213d;
        }

        .calendar-events .event-line .tag {
            padding: 5px 8px;
            background: #14213d;
            color: #eefcf8;
            border-radius: 999px;
            font-size: 10px;
            font-weight: 800;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Initialize ADK runner and session ID (cached to run only once).
    adk_runner, current_session_id = initialize_adk()
    fetch_timezone()

    st.markdown(
        """
        <div class="workflow-workspace">
            <div class="workspace-topbar">
                <div class="workspace-brand"><span class="mark">W</span>WorkFlow</div>
                <div class="workspace-nav">
                    <span>Plan</span>
                    <span>Calendar</span>
                    <span>Workspace</span>
                </div>
            </div>

            <div class="workspace-layout">
                <section class="chat-panel">
                    <div class="chat-panel-header">
                        <div class="title">Assistant</div>
                        <div class="status">online</div>
                    </div>
                    <div class="chat-log">
        """,
        unsafe_allow_html=True,
    )

    # Left column: actual chat panel with messages and input in 40%
    chat_col, calendar_col = st.columns([4, 6])

    with chat_col:
        st.markdown("<div class='chat-panel-header'>...</div>", unsafe_allow_html=True)
        # The visual chat panel is controlled by the HTML above; keep messages only in this actual column.
        # Initialize chat history.
        if MESSAGE_HISTORY_KEY not in st.session_state:
            st.session_state[MESSAGE_HISTORY_KEY] = []

        # Display existing chat messages.
        for message in st.session_state[MESSAGE_HISTORY_KEY]:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

        # Handle new user input.
        prompt = st.chat_input("Ask your assistant to plan or schedule...")
        if prompt:
            # Append user message.
            st.session_state[MESSAGE_HISTORY_KEY].append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant"):
                message_placeholder = st.empty()
                with st.spinner("Assistant is thinking..."):
                    agent_response = run_adk_sync(adk_runner, current_session_id, prompt)
                    message_placeholder.markdown(agent_response)

            st.session_state[MESSAGE_HISTORY_KEY].append({"role": "assistant", "content": agent_response})

    with calendar_col:
        events = _safe_google_calendar_events(max_results=5)
        month_label = datetime.datetime.utcnow().strftime("%B %Y")
        event_rows_md = _format_event_markdown(events)

        st.markdown("### Google Calendar")
        st.markdown(f"**{month_label}**")
        st.markdown(event_rows_md)

