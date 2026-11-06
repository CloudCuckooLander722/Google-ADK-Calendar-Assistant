import streamlit as st
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
from services.cookie_service import controller
from services.api_services import get_calendar_service, get_tasks_service, get_drive_access_token
import html
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Agenda fonts: Montserrat for the title and subtitles, Open Sans for the
# tasks and events (the checkbox labels).
AGENDA_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@500;600;700&family=Open+Sans:wght@400;500&display=swap');

.stApp h1.agenda-title {
    font-family: 'Montserrat', sans-serif;
    font-weight: 700;
    font-size: 2rem;
    margin-bottom: 0.5rem;
}

.stApp h3.agenda-subtitle {
    font-family: 'Montserrat', sans-serif;
    font-weight: 600;
    font-size: 1.25rem;
    margin: 1.25rem 0 0.25rem;
}

/* Streamlit sets its theme font on the <p> inside each checkbox label, so
   the label has to be targeted directly. */
.stApp div[data-testid="stCheckbox"] label p {
    font-family: 'Open Sans', sans-serif;
    font-size: 1rem;
}
</style>
"""

# The user's own date (not UTC's: toISOString() is already tomorrow in the
# evening in the Americas), as YYYY-MM-DD.
LOCAL_DATE_JS = """(() => {
    const d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
})()"""


def _zone(timeZone):
    """The user's timezone, or UTC if it isn't known (yet) or isn't valid."""
    try:
        return ZoneInfo(timeZone) if timeZone else datetime.timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.timezone.utc


def fetch_date():
    """
    Returns the date for today, in the user's timezone.
    Call it once per run: each call draws the same browser component.

    Returns: ISO date string (YYYY-MM-DD)
    """
    user_date = streamlit_js_eval(
        js_expressions=LOCAL_DATE_JS,
        key="get_browser_date"
    )

    if isinstance(user_date, str) and user_date:
        return user_date

    # The browser answers one run late: until then, today in the user's
    # timezone if it is known, else UTC.
    return datetime.datetime.now(_zone(st.session_state.get("timezone"))).date().isoformat()

def list_times(today_date, timeZone):
    """
    Lists the start and end time for the service object: the user's local
    midnight today and the next one, with their UTC offsets (the Calendar API
    rejects times without one).
    params today_date (YYYY-MM-DD), timeZone (IANA name, e.g. America/Los_Angeles)
    returns timeMin, timeMax
    """
    zone = _zone(timeZone)
    day = datetime.date.fromisoformat(today_date)

    # Midnight to the next midnight in the user's timezone: the whole day.
    timeMin = datetime.datetime.combine(day, time.min, tzinfo=zone).isoformat()
    timeMax = datetime.datetime.combine(day + datetime.timedelta(days=1), time.min, tzinfo=zone).isoformat()

    return timeMin, timeMax

def _return_tasks(tasks_service, today_date):
    """
    Returns tasks due today from every task list, completed ones too.
    Params: tasks_service (None when signed out), today_date (YYYY-MM-DD)
    Returns: list of tasks
    """
    if tasks_service is None:
        return []

    try:
        task_lists = tasks_service.tasklists().list(maxResults=100).execute().get('items', [])
    except Exception:
        logging.exception("Failed to list task lists")
        return []

    # A task's due is midnight UTC of its date (2026-11-05T00:00:00.000Z).
    # Google leaves out a task due exactly at dueMin, so ask for the day either
    # side and keep the tasks whose due date is today.
    day = datetime.date.fromisoformat(today_date)
    due_min = f"{day - datetime.timedelta(days=1)}T00:00:00Z"
    due_max = f"{day + datetime.timedelta(days=1)}T00:00:00Z"

    tasks = []
    for task_list in task_lists:
        try:
            tasks_result = tasks_service.tasks().list(
            tasklist=task_list['id'],
            dueMin=due_min,
            dueMax=due_max,
            showCompleted=True,
            # Tasks completed in Google's own apps are hidden unless asked for.
            showHidden=True,
            maxResults=100,
            ).execute()
        except Exception:
            # One unreachable list shouldn't hide the others.
            logging.exception("Failed to load today's tasks from task list %s", task_list.get('id'))
            continue
        tasks.extend(
            task for task in tasks_result.get('items', [])
            if (task.get('due') or '')[:10] == today_date
        )

    return tasks


def _event_start(event):
    """Sort key: all-day events (a bare date) come before timed ones on the same day."""
    start = event.get('start', {})
    return start.get('dateTime') or start.get('date') or ""


def _return_events(timeZone, timeMin, timeMax, calendar_service):
    """
    Returns events from every calendar shown in the user's Google Calendar
    (the ticked ones, plus the primary calendar), all-day first, then by start time.
    Params: timeZone, timeMin, timeMax, calendar_service (None when signed out)
    Returns: events_result ({"items": [...]})
    """
    if calendar_service is None:
        return {"items": []}

    try:
        calendars = calendar_service.calendarList().list().execute().get('items', [])
        calendar_ids = [cal['id'] for cal in calendars if cal.get('selected') or cal.get('primary')]
    except Exception:
        logging.exception("Failed to list calendars")
        calendar_ids = ['primary']

    events = []
    seen_ids = set()
    for calendar_id in calendar_ids:
        try:
            events_result = calendar_service.events().list(
            calendarId=calendar_id,
            timeMin=timeMin,                  
            timeMax=timeMax,                   
            timeZone=timeZone,         
            singleEvents=True,
            orderBy='startTime'
            ).execute()
        except Exception:
            # One unreachable calendar shouldn't hide the others.
            logging.exception("Failed to load today's events from calendar %s", calendar_id)
            continue
        for event in events_result.get('items', []):
            # An event the user was invited to can be in two of their calendars
            # with the same id: show it once (its id is the checkbox's key).
            if event.get('id') not in seen_ids:
                seen_ids.add(event.get('id'))
                events.append(event)

    # Every calendar is in timeZone, so the start times compare as text.
    return {"items": sorted(events, key=_event_start)}



def load_agenda_page():
    """
    Displays events_result and tasks and permits further editing.
    """
    cal_service = get_calendar_service()
    task_service = get_tasks_service()

    if cal_service is None and task_service is None:
        st.warning("Sign in with Google to see today's agenda.")
        return

    timeZone = fetch_timezone()
    today_date = fetch_date()

    timeMin, timeMax = list_times(today_date, timeZone)

    events_result = _return_events(timeZone, timeMin, timeMax, cal_service)

    tasks = _return_tasks(task_service, today_date)

    display_checks(tasks, events_result, today_date)

    #reminder tommorow to add in custom HTML and also option to create tasks / verify tasks

    
def display_checks(tasks, events_result, today_date=None):
    """
    Displays checkboxes for tasks and events. A completed task starts checked.
    Params: tasks, events_result, today_date (fetch_date() if not given)
    Returns: None
    """

    todayDate = today_date or fetch_date()
    st.markdown(AGENDA_CSS, unsafe_allow_html=True)
    st.markdown(
        f'<h1 class="agenda-title">Agenda for {html.escape(str(todayDate))}</h1>',
        unsafe_allow_html=True,
    )

    st.markdown('<h3 class="agenda-subtitle">Tasks</h3>', unsafe_allow_html=True)
    for task in tasks:
        st.checkbox(
            task.get('title') or "Untitled task",
            value=task.get('status') == "completed",
            key=f"task_{task['id']}",
        )
    if not tasks:
        st.caption("No tasks due today.")

    events = events_result.get('items', [])
    st.markdown('<h3 class="agenda-subtitle">Events</h3>', unsafe_allow_html=True)
    for event in events:
        st.checkbox(event.get('summary') or "Untitled event", key=f"event_{event['id']}")
    if not events:
        st.caption("No events today.")


    
