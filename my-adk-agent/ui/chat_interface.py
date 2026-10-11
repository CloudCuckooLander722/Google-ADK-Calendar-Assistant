import streamlit as st
from services.adk_service import (
    initialize_adk, run_adk_sync, content, find_conversation, get_conversation,
    SCREENSHOT_MIME_TYPES, DOCUMENT_MIME_TYPES, DRIVE_MIME_TYPES,
)
from config.settings import get_api_key
from streamlit_js_eval import streamlit_js_eval
from streamlit_calendar import calendar as st_calendar
from streamlit_google_picker import google_picker

from pathlib import Path
import os
import sys
import datetime
import logging

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)

from ui.fetch_timezone import fetch_timezone
from google_oauth.oauth_login import (
    OAuthLogin, get_calendar_service, get_tasks_service, get_drive_access_token,
    get_picker_app_id, restore_signed_in_user,
)


# One height for both panels so the chat box and the calendar always match.
PANEL_HEIGHT = 620
# Space the panel's own padding, border, input bar and gap take, so the
# scrolling message log plus the input fit inside PANEL_HEIGHT without a
# second scrollbar on the panel itself.
CHAT_LOG_HEIGHT = PANEL_HEIGHT - 108

# How long the calendar panel reuses what it fetched from Google. The agent's
# own changes bypass this (see _calendar_blocks).
CALENDAR_CACHE_SECONDS = 120
# Before the signed-in user is known (the first run after a refresh, while the
# cookie loads), the chat is keyed to this browser session instead.
GUEST_KEY = "guest_chat_key"

# Bumped after each send so the attachment widgets get new keys and start empty.
ATTACH_NONCE_KEY = "attach_nonce"
# Extensions (without the dot, as st.file_uploader wants them) for each upload mode.
SCREENSHOT_TYPES = [ext.lstrip(".") for ext in SCREENSHOT_MIME_TYPES]
DOCUMENT_TYPES = [ext.lstrip(".") for ext in DOCUMENT_MIME_TYPES]
# The Google Picker runs in the browser, so it needs its own API key: one
# restricted to the Google Picker API and to this app's web addresses.
GOOGLE_PICKER_API_KEY = os.environ.get("GOOGLE_PICKER_API_KEY")

# The calendar renders in its own iframe, so it can't inherit the page fonts.
CALENDAR_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&display=swap');
.fc { font-family: 'Lato', Helvetica, sans-serif; }
.fc .fc-toolbar-title { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; }
"""

DATEPARSER_LANGUAGES = [
    'en', 'af', 'agq', 'ak', 'am', 'ar', 'as', 'asa', 'ast', 'az', 'az-Cyrl', 'az-Latn', 'bas',
    'be', 'bem', 'bez', 'bg', 'bm', 'bn', 'bo', 'br', 'brx', 'bs', 'bs-Cyrl', 'bs-Latn', 'ca',
    'ce', 'cgg', 'chr', 'ckb', 'cs', 'cy', 'da', 'dav', 'de', 'dje', 'dsb', 'dua', 'dyo', 'dz',
    'ebu', 'ee', 'el', 'eo', 'es', 'et', 'eu', 'ewo', 'fa', 'ff', 'fi', 'fil', 'fo', 'fr',
    'fur', 'fy', 'ga', 'gd', 'gl', 'gsw', 'gu', 'guz', 'gv', 'ha', 'haw', 'he', 'hi', 'hr',
    'hsb', 'hu', 'hy', 'id', 'ig', 'ii', 'is', 'it', 'ja', 'jgo', 'jmc', 'ka', 'kab', 'kam',
    'kde', 'kea', 'khq', 'ki', 'kk', 'kl', 'kln', 'km', 'kn', 'ko', 'kok', 'ks', 'ksb', 'ksf',
    'ksh', 'kw', 'ky', 'lag', 'lb', 'lg', 'lkt', 'ln', 'lo', 'lrc', 'lt', 'lu', 'luo', 'luy',
    'lv', 'mas', 'mer', 'mfe', 'mg', 'mgh', 'mgo', 'mk', 'ml', 'mn', 'mr', 'ms', 'mt', 'mua',
    'my', 'mzn', 'naq', 'nb', 'nd', 'ne', 'nl', 'nmg', 'nn', 'nnh', 'nus', 'nyn', 'om', 'or',
    'os', 'pa', 'pa-Arab', 'pa-Guru', 'pl', 'ps', 'pt', 'qu', 'rm', 'rn', 'ro', 'rof', 'ru',
    'rw', 'rwk', 'sah', 'saq', 'sbp', 'se', 'seh', 'ses', 'sg', 'shi', 'shi-Latn', 'shi-Tfng',
    'si', 'sk', 'sl', 'smn', 'sn', 'so', 'sq', 'sr', 'sr-Cyrl', 'sr-Latn', 'sv', 'sw', 'ta',
    'te', 'teo', 'th', 'ti', 'tl', 'to', 'tr', 'twq', 'tzm', 'ug', 'uk', 'ur', 'uz', 'uz-Arab',
    'uz-Cyrl', 'uz-Latn', 'vi', 'vun', 'wae', 'xog', 'yav', 'yi', 'yo', 'yue', 'zgh', 'zh',
    'zh-Hans', 'zh-Hant', 'zu',
]

# Color for tasks on the calendar, so they stand apart from events.
TASK_COLOR = "#f4a261"


def _calendar_window() -> tuple[datetime.datetime, datetime.datetime]:
    """From the start of last month to the end of next month (UTC), so the
    calendar's prev/next buttons still show blocks one month either side."""
    now = datetime.datetime.now(datetime.timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    window_start = (month_start - datetime.timedelta(days=1)).replace(day=1)
    window_end = (month_start + datetime.timedelta(days=62)).replace(day=1)
    return window_start, window_end


def _google_drive_files(key: str) -> list:
    """
    Renders the Google Drive picker and returns the files picked with it, or
    explains why it can't be shown. The files are downloaded only when sent.
    """
    app_id = get_picker_app_id()
    if not GOOGLE_PICKER_API_KEY or not app_id:
        st.caption("Google Drive isn't set up on this server yet.")
        return []

    token = get_drive_access_token()
    if not token:
        # Signed in before WorkFlow asked for Drive, or unticked it at Google's
        # consent screen: signing in again asks for it.
        st.caption("WorkFlow needs your permission to open files you pick from Google Drive.")
        st.link_button("Connect Google Drive", OAuthLogin().init_google_auth())
        return []

    return list(google_picker(
        label="Pick files from Google Drive",
        token=token,
        apiKey=GOOGLE_PICKER_API_KEY,
        appId=app_id,
        accept_multiple_files=True,
        type=DRIVE_MIME_TYPES,
        key=key,
    ))

#how to use st.cache_resource?
def _google_calendar_events(window_start, window_end, max_results: int = 250) -> list[dict]:
    """Events from every calendar shown in the user's Google Calendar, colored like Google does."""
    service = get_calendar_service()
    if service is None:
        return []
    try:
        calendars = service.calendarList().list().execute().get("items", [])
    except Exception:
        logging.exception("Failed to list calendars for the calendar panel")
        calendars = [{"id": "primary", "primary": True}]

    calendar_events = []
    for cal in calendars:
        if not (cal.get("selected") or cal.get("primary")):
            continue
        try:
            raw_events = service.events().list(
                calendarId=cal["id"],
                timeMin=window_start.isoformat(),
                timeMax=window_end.isoformat(),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            ).execute().get("items", [])
        except Exception:
            # One unreachable calendar shouldn't hide the others.
            logging.exception("Failed to load events for calendar %s", cal.get("id"))
            continue
        for event in raw_events:
            start = event.get("start", {})
            end = event.get("end", {})
            calendar_event = {
                "title": event.get("summary") or "Untitled event",
                "start": start.get("dateTime") or start.get("date"),
                "end": end.get("dateTime") or end.get("date"),
                "allDay": "date" in start,
            }
            if cal.get("backgroundColor"):
                calendar_event["color"] = cal["backgroundColor"]
            calendar_events.append(calendar_event)
    return calendar_events


def _google_tasks(window_start, window_end, max_results: int = 100) -> list[dict]:
    """Tasks with a due date in the window, from every task list, as all-day entries."""
    service = get_tasks_service()
    if service is None:
        return []
    try:
        task_lists = service.tasklists().list(maxResults=100).execute().get("items", [])
    except Exception:
        logging.exception("Failed to list task lists for the calendar panel")
        return []

    calendar_tasks = []
    for task_list in task_lists:
        try:
            tasks = service.tasks().list(
                tasklist=task_list["id"],
                dueMin=window_start.isoformat(),
                dueMax=window_end.isoformat(),
                showCompleted=True,
                maxResults=max_results,
            ).execute().get("items", [])
        except Exception:
            logging.exception("Failed to load tasks for task list %s", task_list.get("id"))
            continue
        for task in tasks:
            if not task.get("due"):
                continue
            done = task.get("status") == "completed"
            calendar_tasks.append({
                "title": ("✓ " if done else "☐ ") + (task.get("title") or "Untitled task"),
                # The Tasks API only keeps the due *date*; the time part is always midnight.
                "start": task["due"][:10],
                "allDay": True,
                "color": TASK_COLOR,
                "textColor": "#ffffff",
            })
    return calendar_tasks


@st.cache_data(ttl=CALENDAR_CACHE_SECONDS, show_spinner=False)
def _calendar_blocks(user_id: str, calendar_version: int, window_start, window_end) -> list[dict]:
    """
    Every block (event or task) for the calendar panel, shaped for the
    streamlit-calendar (FullCalendar) widget.

    Cached per user, so reruns and refreshes don't refetch every calendar from
    Google. The arguments are the cache key: user_id keeps one user's calendar
    from ever being shown to another, and calendar_version is bumped after each
    agent turn, so the agent's changes show up right away. Changes made in
    Google Calendar itself show up within CALENDAR_CACHE_SECONDS.
    """
    return _google_calendar_events(window_start, window_end) + _google_tasks(window_start, window_end)


def _attachment_name(attachment) -> str:
    return attachment.name if hasattr(attachment, "name") else str(attachment)


def _attachment_menu(nonce: int) -> dict:
    """
    Renders the add_col menu where the user picks what to attach: a screenshot,
    a document, a recording, or Google Drive files. Returns the picks as keyword
    arguments for services.adk_service.content(): screenshots, documents,
    recording, google_drive.

    Tabs (rather than a mode switch) keep every widget rendered, so picks
    made in one tab survive switching to another.
    """
    keys = {name: f"attach_{name}_{nonce}" for name in ("screenshots", "documents", "recording", "google_drive")}
    # Widget values from the previous run are already in session state, so the
    # button can show how many things are attached while the menu is closed.
    count = (
        len(st.session_state.get(keys["screenshots"]) or [])
        + len(st.session_state.get(keys["documents"]) or [])
        + (1 if st.session_state.get(keys["recording"]) is not None else 0)
        + len(st.session_state.get(keys["google_drive"]) or [])
    )
    label = f"📎 {count}" if count else "➕"

    with st.popover(label, help="Attach a screenshot, document, recording, or Google Drive file to your next message"):
        screenshot_tab, document_tab, record_tab, google_drive_tab = st.tabs(["Screenshot", "Document", "Record", "Google Drive"])
        with screenshot_tab:
            screenshots = st.file_uploader(
                "Upload screenshots", type=SCREENSHOT_TYPES,
                accept_multiple_files=True, key=keys["screenshots"],
            )
        with document_tab:
            documents = st.file_uploader(
                "Upload documents", type=DOCUMENT_TYPES,
                accept_multiple_files=True, key=keys["documents"],
            )
        with record_tab:
            recording = st.audio_input("Record a voice message", key=keys["recording"])
        with google_drive_tab:
            google_drive = _google_drive_files(keys["google_drive"])
        st.caption("Attachments are sent with your next message.")

    return {
        "screenshots": list(screenshots or []),
        "documents": list(documents or []),
        "recording": recording,
        "google_drive": google_drive,
    }


def run_chat_interface():
    """
    Sets up and runs the Streamlit web application for the ADK chat assistant.
    """
    st.set_page_config(page_title="WorkFlow Assistant", layout="wide")

    api_key = get_api_key()
    if not api_key:
        st.error("⚠️ Action Required: Google API Key Not Found or Invalid! Please set GOOGLE_API_KEY in your .env file. ⚠️")
        st.stop()

    # Figma / Notion workspace styling.
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,300;0,400;0,700;0,900;1,300;1,400;1,700;1,900&family=Montserrat:wght@400;500;600;700&family=Playfair+Display:wght@600;700&display=swap');

        /* Helvetica isn't a Google Font, so it can't be loaded: it only renders
           where installed locally (mostly Apple devices) and falls back to Arial. */
        .stApp {
            background: #f6f7f9;
            font-family: 'Montserrat', 'Playfair Display', sans-serif;
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

    # After a browser refresh session_state is empty: bring the signed-in user
    # back from their cookie, so the calendar and the agent's tools can find
    # their Google credentials again.
    signed_in_user = restore_signed_in_user()

    # The runner and every user's conversation are kept by st.cache_resource,
    # so the chat and the agent's context survive a refresh. They're keyed by
    # the Google account, never shared between users.
    adk_runner = initialize_adk()
    user_key = signed_in_user or st.session_state.setdefault(GUEST_KEY, f"guest_{os.urandom(8).hex()}")
    conversation = find_conversation(user_key)
    fetch_timezone()

    # Left column: actual chat panel with messages and input in 40%
    chat_col, calendar_col = st.columns([4, 6])

    with chat_col:
        st.markdown("#### WorkFlow Assistant")
        st.session_state.setdefault(ATTACH_NONCE_KEY, 0)

        # Outer panel is the same height as the calendar. Inside it, the
        # message log scrolls and the add/prompt row stays pinned beneath it.
        chat_panel = st.container(height=PANEL_HEIGHT)
        with chat_panel:
            chat_log = st.container(height=CHAT_LOG_HEIGHT, border=False)
            with chat_log:
                # Display existing chat messages.
                for message in (conversation.messages if conversation else []):
                    with st.chat_message(message["role"]):
                        st.markdown(message["content"])

            # add_col: attachments menu, prompt_col: the text prompt.
            add_col, prompt_col = st.columns([1, 9], vertical_alignment="bottom")
            with add_col:
                attachments = _attachment_menu(st.session_state[ATTACH_NONCE_KEY])
            with prompt_col:
                prompt = st.chat_input("Ask WorkFlow Assistant to plan or schedule...")

        if prompt:
            # Show the prompt plus a label per attachment (never the raw bytes).
            labels = [
                f"📎 {_attachment_name(a)}"
                for a in attachments["screenshots"] + attachments["documents"] + attachments["google_drive"]
            ]
            if attachments["recording"] is not None:
                labels.append("🎤 Recording")
            shown = "\n\n".join([prompt, *labels])

            conversation = get_conversation(adk_runner, user_key)
            conversation.messages.append({"role": "user", "content": shown})
            with chat_log:
                with st.chat_message("user"):
                    st.markdown(shown)

                with st.chat_message("assistant"):
                    empty_response = st.empty()
                    with st.spinner("Assistant is thinking..."):
                        try:
                            message = content(prompt=prompt, **attachments)
                        except ValueError as e:
                            agent_response = f"I couldn't send that: {e}"
                        else:
                            agent_response = run_adk_sync(adk_runner, user_key, conversation.session_id, message)
                        empty_response.markdown(agent_response)

            conversation.messages.append({"role": "assistant", "content": agent_response})
            # The agent may have changed the calendar: fetch it fresh next run.
            conversation.calendar_version += 1
            # Attachments go with exactly one prompt: new widget keys empty them, and
            # the rerun redraws the menu (and its count) from the cleared widgets.
            st.session_state[ATTACH_NONCE_KEY] += 1
            st.rerun()

    with calendar_col:
        st.markdown("#### Google Calendar")
        calendar_blocks = []
        if signed_in_user:
            calendar_blocks = _calendar_blocks(
                signed_in_user, conversation.calendar_version if conversation else 0, *_calendar_window(),
            )
        st_calendar(
            events=calendar_blocks,
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
        if not signed_in_user:
            st.caption("Loading your calendar... if it stays empty, sign in again.")
        elif not calendar_blocks:
            st.caption("No events or tasks found around this month.")
        else:
            st.caption("Events use their calendar's color; ☐ tasks are orange.")

