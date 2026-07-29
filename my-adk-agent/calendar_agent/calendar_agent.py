import datetime
import os.path
import re
from dateutil import parser as dateutil_parser
import dateparser
import pytz
from tzlocal import get_localzone
from typing import Optional, List, Dict 

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import google.auth
from google.adk.agents import LlmAgent
from google.genai import types

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks"
]

credentials_path = "/workspaces/Google-ADK-Calendar-Assistant/my-adk-agent/calendar_agent/credentials.json"

# Prefer credentials/token located alongside this module, but fall back to cwd.
MODULE_DIR = os.path.dirname(__file__)
DEFAULT_CREDENTIALS_PATHS = [
    os.path.join(MODULE_DIR, 'credentials.json'),
    os.path.join(MODULE_DIR, '..', 'credentials.json'),
    credentials_path
]
DEFAULT_TOKEN_PATHS = [
        os.path.join(MODULE_DIR, 'token.json'),
        os.path.join(MODULE_DIR, '..', 'token.json'),
        'token.json'
]

def _find_first_existing(paths: list[str]) -> str | None:
        for p in paths:
                p_abs = os.path.abspath(p)
                if os.path.exists(p_abs):
                        return p_abs
        return None

ROOT_INSTRUCTIONS = """
You are a helpful and precise calendar assistant that operates in the user's local time zone (e.g., IST for Asia/Kolkata).

Event Creation Instructions:
When the user wants to create an event:
- Collect essential details: title, start time, end time/duration.
- Use `parse_natural_language_datetime` to parse dates/times/durations into ISO 8601 UTC.
- Location and description are optional; only include if provided.
- For recurring events, parse recurrence (e.g., "every Tuesday for 5 weeks") using `parse_recurrence` and pass as RRULE string.
- For attendees, parse emails (e.g., "invite bob@example.com and alice@example.com") as list of dicts [{email: "bob@example.com"}, {email: "alice@example.com"}].
- Call `create_event` with parsed values, including recurrence and attendees if provided.
- Respond with confirmation, title/time in local TZ, and link.

Event Updating/Editing Instructions:
When the user wants to update or edit an event:
- Identify the event: Use `search_events` or `get_event` if ID is known.
- Ask for clarification if multiple matches or ambiguous.
- Use `parse_natural_language_datetime` if updating times/durations.
- For updating recurrence or attendees, parse and pass as in creation.
- Call `update_event` with the event ID and only changed fields (pass None for unchanged), including recurrence or attendees.
- Set `send_updates` to "all" if attendees might be affected, else "none".
- Respond with confirmation and updated details in local TZ.

Event Deletion Instructions:
When the user wants to delete an event:
- Identify the event: Use `search_events` to find the event ID.
- Confirm with the user if needed (e.g., show details via `get_event`).
- Call `delete_event` with the event ID.
- Set `send_updates` to "all" if notifying others, else "none".
- Respond with confirmation.

Event Search and Querying Instructions:
When the user asks to search or query events:
- Use `search_events` with query (keywords), time_min/max (parsed via `parse_natural_language_datetime`).
- Display results in local TZ, including event ID for reference.
- If no results, say so politely.
- For upcoming events, use `list_events`.

Meeting Time Suggestions Instructions:
When the user asks to suggest meeting times (e.g., "Suggest a time for a meeting next Tuesday"):
- Use `suggest_meeting_times` with the target date, duration, and optional time preference (e.g., "morning", "9 AM to 2 PM").
- Parse inputs using `parse_natural_language_datetime` to get the date and duration.
- Return 2-3 free time slots in local TZ (e.g., IST).
- If no slots are available, suggest alternative days or durations.
- Offer to create an event with the chosen slot (e.g., "Shall I schedule the meeting at 2 PM?").
- Example: "Suggest a 1-hour meeting next Tuesday morning" returns slots like "2025-09-23 10:00 AM IST - 11:00 AM IST".

General Instructions:
- Always use local time zone (e.g., IST) for inputs/outputs; convert to UTC for API.
- For "next [day]" (e.g., "next Friday"), interpret as next occurrence.
- If event ID unknown for update/delete, search first.
- Handle ambiguities by asking questions.
- Keep responses short, user-friendly; no raw JSON.
- Prioritize clarity and correctness.
"""

#Events Functions (Marked)
def get_calendar_service():
    """Authenticate and return a Google Calendar service object."""
    creds = None
    token_path = _find_first_existing(DEFAULT_TOKEN_PATHS)
    if token_path and os.path.exists(token_path): #there's no token.json file here
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except (UnicodeDecodeError, ValueError):
            print("Warning: token file is invalid or has an encoding issue. Attempting to re-authorize.")
            try:
                os.remove(token_path)
            except Exception:
                pass
    #possible error in get_tasks_service -> possible cause...
    if not creds or not creds.valid: #checks for invalid creds
        if creds and creds.expired and creds.refresh_token:
            try: #checks for expired creds
                print("Attempting to refresh expired credentials...")
                creds.refresh(Request())
            except RefreshError as e:
                print(f"Error refreshing credentials: {e}. Attempting to re-authorize.")
                creds = None

        if not creds: #there is no credentials found from the token.json
            cred_path = _find_first_existing(DEFAULT_CREDENTIALS_PATHS)
            if cred_path and os.path.exists(cred_path):
                flow = InstalledAppFlow.from_client_secrets_file(cred_path, SCOPES) #the error should be here
                # Check if we are running inside GitHub Codespaces
                if 'CODESPACES' in os.environ:
                    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
                    auth_url, _ = flow.authorization_url(prompt='select_account')
                    print(f"\n[Codespaces Detected] Open this URL:\n{auth_url}\n")
                    code = input("Enter the authorization code: ").strip()
                    flow.fetch_token(code=code)
                    creds = flow.credentials
                else:
                    # Fallback to local server if running natively on your laptop
                    creds = flow.run_local_server(port=0)
            else:
                try:
                    creds, _ = google.auth.default(scopes=SCOPES)
                except Exception:
                    raise FileNotFoundError(
                        f"{credentials_path} not found and Application Default Credentials unavailable. "
                        f"Create OAuth client credentials ({credentials_path}) or set up ADC (gcloud auth application-default login or provide a service account)."
                    )
        if creds:
            write_token_path = token_path or os.path.join(MODULE_DIR, 'token.json')
            try:
                with open(write_token_path, "w", encoding="utf-8") as token:
                    token.write(creds.to_json())
            except Exception:
                # best-effort: try writing to cwd
                try:
                    with open('token.json', 'w', encoding='utf-8') as token:
                        token.write(creds.to_json())
                except Exception:
                    pass

    service = build("calendar", "v3", credentials=creds)
    return service

def get_user_timezone() -> str:
    """
    Detect the user's local time zone. Falls back to 'Asia/Kolkata' if detection fails.
    """
    try:
        local_tz = get_localzone()
        # Prefer canonical zone name when available (tzlocal may return different types).
        for attr in ("zone", "key"):
            name = getattr(local_tz, attr, None)
            if isinstance(name, str) and name:
                return name
        return str(local_tz)
    except Exception as e:
        print(f"Warning: Could not detect local time zone ({str(e)}). Falling back to 'Asia/Kolkata'.")
        return "Asia/Kolkata"

def create_event(
        summary: str,
        start_datetime: str,
        end_datetime: str,
        location: str = "",
        description: str = "",
        recurrence: str | None = None,
        attendees: list[dict[str, str]] | None = None
    ):
    service = get_calendar_service()
    user_timezone = get_user_timezone()
    event = {
        "summary": summary,
    }

    if start_datetime and end_datetime:
        event["start"] = {"dateTime": start_datetime , "timeZone": user_timezone}
        event["end"] = {"dateTime": end_datetime, "timeZone": user_timezone}

    if location and location.strip():
        event["location"] = location

    if description and description.strip():
        event["description"] = description
    
    if recurrence:
        event["recurrence"] = [recurrence]
    if attendees:
        event["attendees"] = attendees

    try:
        created = service.events().insert(calendarId="primary", body=event).execute()
        return f"Event created: {created.get('htmlLink')}"
    except HttpError as error:
        resp = getattr(error, 'resp', None)
        status = getattr(resp, 'status', None)
        if status == 403:
            raise PermissionError(
                "Google Calendar API returned 403 Forbidden (insufficient permissions). "
                "Ensure the credentials have the scopes: calendar and tasks. "
                "If using OAuth, place credentials.json in the project and complete the consent flow; "
                "if using ADC, run: `gcloud auth application-default login --scopes=https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/tasks`."
            )
        raise ValueError(f'ValueError: {error}')

def delete_event(event_id: str, calendar_id: str = "primary", send_updates: str = "none") -> str:
    try:
        service = get_calendar_service()
        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates=send_updates
        ).execute()
        return "Event deleted successfully."
    except HttpError as error:
        raise ValueError(f"Failed to delete event: {str(error)}")

# ---- Time helpers ----
def to_rfc3339_utc(dt: Optional[datetime.datetime], tz_name: str) -> Optional[str]:
    """Convert a naive or tz-aware datetime to an RFC3339 UTC string for Tasks API.

    Returns `None` if `dt` is None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        local_tz = pytz.timezone(tz_name)
        dt = local_tz.localize(dt)
    utc = dt.astimezone(pytz.UTC)
    return utc.isoformat().replace('+00:00', 'Z')

def get_event(event_id: str, calendar_id: str = "primary") -> Dict:
    service = get_calendar_service()
    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        return event
    except HttpError as error:
        raise ValueError(f"Failed to get event: {str(error)}")

def search_events(
    query: Optional[str] = None,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_results: int = 10,
    calendar_id: str = "primary"
) -> List[str]:
    service = get_calendar_service()
    params = {
        "calendarId": calendar_id,
        "maxResults": max_results,
        "singleEvents": True, # Expand recurring events into individual instances
        "orderBy": "startTime"
    }
    if query:
        params["q"] = query # Keyword search
    if time_min:
        params["timeMin"] = time_min # ISO 8601 UTC start time
    if time_max:
        params["timeMax"] = time_max # ISO 8601 UTC end time

    try:
        events_result = service.events().list(**params).execute()
        events = events_result.get("items", [])

        if not events:
            return ["No events found."]

        user_tz = pytz.timezone(get_user_timezone())
        formatted_events = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            if 'dateTime' in event['start']: # If it's a timed event
                utc_time = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                local_time = utc_time.astimezone(user_tz) # Convert to local TZ
                formatted_time = local_time.strftime("%Y-%m-%d %I:%M %p %Z")
            else: # All-day event
                formatted_time = start
            formatted_events.append(f"{formatted_time} - {event['summary']} - ID: {event['id']}")
        return formatted_events
    except HttpError as error:
        raise ValueError(f"Failed to search events: {str(error)}")

def list_events(max_results: int = 10):
    now = datetime.datetime.now(tz=pytz.UTC).isoformat()
    return search_events(time_min=now, max_results=max_results)

def update_event(
        event_id: str,
        summary: Optional[str] = None,
        start_datetime: Optional[str] = None,
        end_datetime: Optional[str] = None,
        location: Optional[str] = None,
        description: Optional[str] = None,
        recurrence: Optional[str] = None,
        attendees: Optional[List[Dict[str, str]]] = None,
        send_updates: str = "none",
        calendar_id: str = "primary"
        
    ):
    service = get_calendar_service()
    patch_body = {}
    if summary is not None:
        patch_body["summary"] = summary
    if start_datetime is not None and end_datetime is not None:
        patch_body["start"] = {"dateTime": start_datetime, "timeZone": get_user_timezone()}
        patch_body["end"] = {"dateTime": end_datetime, "timeZone": get_user_timezone()}
    if location is not None:
        patch_body["location"] = location
    if description is not None:
        patch_body["description"] = description
    if recurrence is not None:
        patch_body["recurrence"] = [recurrence]
    if attendees is not None:
        patch_body["attendees"] = attendees
    if not patch_body:
        return "No updates provided. Event remains unchanged."

    try:
        updated_event = service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=patch_body,
            sendUpdates=send_updates
        ).execute()
        return f"Event updated: {updated_event.get('htmlLink')}"
    except HttpError as error:
        raise ValueError(f"Failed to update event: {str(error)}")

def parse_recurrence(recurrence_string: str) -> str:
    """
    Parse natural language recurrence into RRULE format.

    Args:
        recurrence_string: Natural language (e.g., "every Tuesday for 5 weeks").

    Returns:
        RRULE string (e.g., "RRULE:FREQ=WEEKLY;WKST=TU;COUNT=5").

    Raises:
        ValueError: If recurrence cannot be parsed.
    """
    # Basic parsing for common patterns like "every tuesday for 5 weeks" or "every month for 3 months"
    match = re.match(r'every\s+(\w+)\s*(for\s+(\d+)\s*(week|month|year)s?)?', recurrence_string, re.IGNORECASE)
    if match:
        freq_map = {
            'daily': 'DAILY', 'weekly': 'WEEKLY', 'monthly': 'MONTHLY', 'yearly': 'YEARLY',
            'monday': 'WEEKLY;BYDAY=MO', 'tuesday': 'WEEKLY;BYDAY=TU', 'wednesday': 'WEEKLY;BYDAY=WE',
            'thursday': 'WEEKLY;BYDAY=TH', 'friday': 'WEEKLY;BYDAY=FR', 'saturday': 'WEEKLY;BYDAY=SA', 'sunday': 'WEEKLY;BYDAY=SU'
        }
        day_or_freq = match.group(1).lower()

        base_rrule = freq_map.get(day_or_freq)
        if not base_rrule:
            # Default to weekly if we can't map the token
            base_rrule = 'WEEKLY'

        # If the mapped value already contains a semicolon (e.g., 'WEEKLY;BYDAY=TU'),
        # split into FREQ and extras so we can append COUNT cleanly.
        parts = base_rrule.split(';')
        freq_part = parts[0]
        extras = parts[1:] if len(parts) > 1 else []

        rrule = f"RRULE:FREQ={freq_part}"
        if extras:
            rrule += ';' + ';'.join(extras)

        if match.group(2):
            count = int(match.group(3))
            unit = match.group(4).lower()
            # If user specified weeks, use count directly. For months/years, behave as follows:
            if unit.startswith('week'):
                rrule += f";COUNT={count}"
            elif unit.startswith('month'):
                # If the base frequency is MONTHLY or YEARLY, COUNT can be used directly.
                if freq_part in ('MONTHLY', 'YEARLY'):
                    rrule += f";COUNT={count}"
                else:
                    # If they specified a weekday-based recurrence but asked "for X months",
                    # approximate by converting months -> weeks (approx. 4 weeks/month).
                    rrule += f";COUNT={count * 4}"
            elif unit.startswith('year'):
                if freq_part == 'YEARLY':
                    rrule += f";COUNT={count}"
                else:
                    rrule += f";COUNT={count * 52}"
        return rrule
    raise ValueError(f"Could not parse recurrence: {recurrence_string}")


def parse_duration(duration_str: str) -> int:
    """Parse a simple duration string and return minutes.

    Examples: "1 hour", "30 minutes", "2 hrs", "90m".
    """
    if not duration_str:
        return 0
    s = duration_str.strip().lower()
    # common patterns
    m = re.match(r"^(\d+)\s*(?:hours?|hrs?|h)$", s)
    if m:
        return int(m.group(1)) * 60
    m = re.match(r"^(\d+)\s*(?:minutes?|mins?|m)$", s)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d+)\s*(?:days?|d)$", s)
    if m:
        return int(m.group(1)) * 60 * 24
    # combined like '1h30m' or '90m'
    total = 0
    parts = re.findall(r"(\d+)\s*(h|hr|hrs|m|min|mins|d)", s)
    if parts:
        for value, unit in parts:
            v = int(value)
            if unit.startswith('h'):
                total += v * 60
            elif unit.startswith('d'):
                total += v * 60 * 24
            else:
                total += v
        return total
    # fallback: try to parse as plain minutes
    try:
        return int(re.findall(r"\d+", s)[0])
    except Exception:
        raise ValueError(f"Could not parse duration: {duration_str}")

#Task Helpers

def get_tasks_service():
    """Create and return an authenticated Google Tasks API service (v1).

    Uses the same `token.json`/`credentials.json` flow as `get_calendar_service`.
    """
    creds = None
    token_path = _find_first_existing(DEFAULT_TOKEN_PATHS)
    if token_path and os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except (UnicodeDecodeError, ValueError):
            print("Warning: token file is invalid or has an encoding issue. Attempting to re-authorize.")
            try:
                os.remove(token_path)
            except Exception:
                pass

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                print('Attempting to refresh credentials...')
                creds.refresh(Request())
            except RefreshError as e:
                print(f"Error refreshing credentials: {e}. Attempting to re-authorize.")
                creds = None

        if not creds:
            cred_path = _find_first_existing(DEFAULT_CREDENTIALS_PATHS)
            if cred_path and os.path.exists(cred_path):
                flow = InstalledAppFlow.from_client_secrets_file(cred_path, SCOPES)
                if 'CODESPACES' in os.environ:
                    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
                    auth_url, _ = flow.authorization_url(prompt='select_account')
                    print(f"\n[Codespaces Detected] Open this URL:\n{auth_url}\n")
                    code = input("Enter the authorization code: ").strip()
                    flow.fetch_token(code=code)
                    creds = flow.credentials
                else:
                                    # Fallback to local server if running natively on your laptop
                    creds = flow.run_local_server(port=0)
            else:
                try:
                    creds, _ = google.auth.default(scopes=SCOPES)
                except Exception:
                    raise FileNotFoundError(
                        "credentials.json not found and Application Default Credentials unavailable. "
                        "Create OAuth client credentials (credentials.json) or set up ADC (gcloud auth application-default login or provide a service account)."
                    )

        write_token_path = token_path or os.path.join(MODULE_DIR, 'token.json')
        try:
            with open(write_token_path, "w", encoding="utf-8") as token:
                token.write(creds.to_json())
        except Exception:
            try:
                with open('token.json', 'w', encoding='utf-8') as token:
                    token.write(creds.to_json())
            except Exception:
                pass
    return build("tasks", "v1", credentials=creds)


def create_task(tasklist: str = "@default", title: str = None, notes: str | None = None, due: str | None = None, status: str | None = None, parent: str | None = None):
    """Create a task in the specified tasklist.

    - `due` should be an RFC3339 timestamp (use `to_rfc3339_utc` to convert).
    - Returns the created task resource.
    """
    service = get_tasks_service()
    body = {}
    if title:
        body["title"] = title
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = due
    if status is not None:
        body["status"] = status
    if parent is not None:
        body["parent"] = parent

    try:
        created = service.tasks().insert(tasklist=tasklist, body=body).execute()
        return created
    except HttpError as error:
        raise ValueError(f"Failed to create task: {str(error)}")


def get_task(tasklist: str, task_id: str):
    service = get_tasks_service()
    try:
        task = service.tasks().get(tasklist=tasklist, task=task_id).execute()
        return task
    except HttpError as error:
        raise ValueError(f"Failed to get task: {str(error)}")


def list_tasks(tasklist: str = "@default", show_completed: bool = False, max_results: int = 100):
    service = get_tasks_service()
    try:
        resp = service.tasks().list(tasklist=tasklist, showCompleted=show_completed, maxResults=max_results).execute()
        return resp.get("items", [])
    except HttpError as error:
        raise ValueError(f"Failed to list tasks: {str(error)}")


def patch_task(tasklist: str, 
               task_id: str, 
               patch_fields: dict,
               recurrence: Optional[str] = None,
               due: Optional[str] = None,
               notes: str | None = None,):
    """Partially update a task using Tasks API `patch`.

    `patch_fields` is a dict with any Task fields to change, e.g. {"notes": "updated"}.
    """
    service = get_tasks_service()
    if notes is not None:
        patch_fields["notes"] = notes
    if due is not None:
        patch_fields["due"] = due
    if recurrence is not None:
        patch_fields["recurrence"] = [recurrence]

    try:
        updated = service.tasks().patch(tasklist=tasklist, task=task_id, body=patch_fields).execute()
        return updated
    except HttpError as error:
        raise ValueError(f"Failed to patch task: {str(error)}")


def delete_task(tasklist: str, task_id: str):
    service = get_tasks_service()
    try:
        service.tasks().delete(tasklist=tasklist, task=task_id).execute()
        return "Task deleted successfully."
    except HttpError as error:
        raise ValueError(f"Failed to delete task: {str(error)}")

def parse_natural_language_datetime(datetime_string: str, duration: Optional[str] = None, time_preference: Optional[str] = None) -> tuple[str, str, Optional[tuple[datetime.time, datetime.time]]]:
    """
    Parses a natural language date/time string in the user's local time zone
    and returns start and end times in ISO 8601 UTC format, plus optional time window.
    
    Args:
        datetime_string: Natural language input (e.g., "next Friday at 11 AM").
        duration: Optional duration (e.g., "1 hour", "for 30 minutes").
        time_preference: Optional preference (e.g., "morning", "9 AM to 2 PM").
    
    Returns:
        Tuple of (start_datetime, end_datetime, time_window) in ISO 8601 UTC and optional (start_time, end_time).
    """
    user_timezone = get_user_timezone()
    settings = {
        'TIMEZONE': user_timezone,
        'TO_TIMEZONE': 'UTC',
        'RETURN_AS_TIMEZONE_AWARE': True,
        'PREFER_DATES_FROM': 'future',
        'DATE_ORDER': 'DMY',
        'STRICT_PARSING': False
    }

    time_window = None
    if time_preference:
        if time_preference.lower() in ["morning", "afternoon", "evening"]:
            time_ranges = {
                "morning": (datetime.time(9, 0), datetime.time(12, 0)),
                "afternoon": (datetime.time(12, 0), datetime.time(17, 0)),
                "evening": (datetime.time(17, 0), datetime.time(21, 0))
            }
            time_window = time_ranges.get(time_preference.lower())
        else:
            try:
                match = re.match(r'(\d+\s*(?:AM|PM|am|pm))\s*to\s*(\d+\s*(?:AM|PM|am|pm))', time_preference, re.IGNORECASE)
                if match:
                    start_str, end_str = match.groups()
                    start_time = dateutil_parser.parse(start_str).time()
                    end_time = dateutil_parser.parse(end_str).time()
                    time_window = (start_time, end_time)
            except ValueError:
                print(f"Could not parse time preference: {time_preference}")

    # Try parsing with dateparser first
    parsed_datetime = dateparser.parse(
        datetime_string,
        languages=['en'],
        settings=settings
    )

    if not parsed_datetime:
        # Handle "next [day]" patterns with optional time part
        match = re.match(r'next\s+([a-zA-Z]+)(?:\s+at\s+(.+?))?(?:\s+(morning|afternoon|evening))?$', datetime_string, re.IGNORECASE)
        if match:
            day_name, time_part, period = match.groups()
            print(f"Manual parsing: day_name={day_name}, time_part={time_part}, period={period}")

            day_map = {
                'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
                'friday': 4, 'saturday': 5, 'sunday': 6
            }
            if day_name.lower() not in day_map:
                raise ValueError(f"Invalid day name: {day_name}")

            target_weekday = day_map[day_name.lower()]
            current_date = datetime.datetime.now(pytz.timezone(user_timezone))
            current_weekday = current_date.weekday()
            days_ahead = (target_weekday - current_weekday + 7) % 7 or 7
            target_date = current_date + datetime.timedelta(days=days_ahead)

            # Default to 9 AM if no time part or period is provided
            default_hour = 9
            if period:
                period_map = {
                    'morning': 9,
                    'afternoon': 13,
                    'evening': 18
                }
                default_hour = period_map.get(period.lower(), 9)
                time_part = time_part or f"{default_hour}:00"

            if time_part:
                try:
                    time_parsed = dateutil_parser.parse(time_part, fuzzy=True)
                    parsed_datetime = target_date.replace(
                        hour=time_parsed.hour,
                        minute=time_parsed.minute,
                        second=0,
                        microsecond=0
                    )
                except ValueError:
                    raise ValueError(f"Could not parse time part: {time_part}")
            else:
                parsed_datetime = target_date.replace(
                    hour=default_hour,
                    minute=0,
                    second=0,
                    microsecond=0
                )

    if not parsed_datetime:
        try:
            # Fallback to dateutil for general parsing
            parsed_datetime = dateutil_parser.parse(datetime_string, fuzzy=True)
            parsed_datetime = pytz.timezone(user_timezone).localize(parsed_datetime)
        except ValueError:
            raise ValueError(f"Could not parse date/time: {datetime_string}")

    parsed_datetime = parsed_datetime.astimezone(pytz.UTC)
    start_datetime = parsed_datetime.isoformat().replace('+00:00', 'Z')

    if duration:
        duration_minutes = parse_duration(duration)
        end_datetime = (parsed_datetime + datetime.timedelta(minutes=duration_minutes)).isoformat().replace('+00:00', 'Z')
    else:
        end_datetime = (parsed_datetime + datetime.timedelta(hours=1)).isoformat().replace('+00:00', 'Z')

    return start_datetime, end_datetime, time_window


calendar_agent = LlmAgent(
    name="calendar_agent",
    description="An agent that can manage your Google Calendar events and tasks including updating, deleting, and searching for events.",
    instruction=ROOT_INSTRUCTIONS,
    tools=[create_event,
            delete_event,
            parse_recurrence,
            parse_natural_language_datetime,
            get_event,
            search_events,
            list_events,
            update_event,
            create_task,
            get_task,
            list_tasks,
            patch_task,
            delete_task,]
)

