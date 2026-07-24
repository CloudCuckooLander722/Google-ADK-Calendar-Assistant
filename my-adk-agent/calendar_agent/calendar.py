import datetime
import os.path
import re
from dateutil import parser as dateutil_parser
import dateparser
import pytz
from tzlocal import get_localzone
from typing import Optional, List, Dict 

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.adk.agents import Agent
from google.genai import types


SCOPES = ["https://www.googleapis.com/auth/calendar"]

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

calendar_agent = Agent(
    name="calendar_agent",
    model="gemini-2.0-flash",
    description="schedules events and creates tasks",
    instruction=ROOT_INSTRUCTIONS,
    tools=[get_user_timezone, 
           create_event, 
           delete_event,
           parse_recurrence,
           get_event,
           search_events,
           list_events,
           update_events,
           ]
)

def get_calendar_service(): #takes the credentials and uses build() to access google calendar
    creds = None
    if os.path.exists('token.json'):
        try:
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        except (UnicodeDecodeError, ValueError):
            print("Warning: 'token.json' is invalid or has an encoding issue. Attempting to re-authorize.")
            os.remove("token.json")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w", encoding="utf-8") as token:
            token.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)

def get_user_timezone() -> str:
    """
    Detect the user's local time zone. Falls back to 'Asia/Kolkata' if detection fails.
    """
    try:
        return str(get_localzone())
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

    if location and location.strip() == "":
        event["location"] = location
    
    if recurrence:
        event["recurrence"] = [recurrence]
    if attendees:
        event["attendees"] = attendees

    try:
        created = service.events().insert(calendarId="primary", body=event).execute()
        return f"Event created: {created.get('htmlLink')}"
    except HttpError as error:
        raise ValueError(f'ValueError: {error}')

def delete_event(event_id: str, calendar_id: str = "primary", send_updates: str = "none") -> str:
    try:
        service = get_calendar_service()
        delete = service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates=send_updates
        ).executes()
        return "Event deleted successfully."
    except HttpError as error:
        raise ValueError(f"Failed to delete event: {str(error)}")

def create_tasks():
    pass

def delete_tasks():
    pass

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
    # Basic parsing for common patterns
    match = re.match(r'every\s+(\w+)\s*(for\s+(\d+)\s*(week|month|year)s?)?', recurrence_string, re.IGNORECASE)
    if match:
        freq_map = {
            'daily': 'DAILY', 'weekly': 'WEEKLY', 'monthly': 'MONTHLY', 'yearly': 'YEARLY',
            'monday': 'WEEKLY;BYDAY=MO', 'tuesday': 'WEEKLY;BYDAY=TU', 'wednesday': 'WEEKLY;BYDAY=WE',
            'thursday': 'WEEKLY;BYDAY=TH', 'friday': 'WEEKLY;BYDAY=FR', 'saturday': 'WEEKLY;BYDAY=SA', 'sunday': 'WEEKLY;BYDAY=SU'
        }
        day_or_freq = match.group(1).lower()
        rrule = f"RRULE:FREQ={freq_map.get(day_or_freq, 'WEEKLY')}" # Default to WEEKLY if not specific day/freq

        if match.group(2): # Check if 'for X weeks/months/years' part exists
            count = match.group(3)
            unit = match.group(4).upper()
            if unit.startswith('WEEK'):
                rrule += f";COUNT={count}"
            elif unit.startswith('MONTH'):
                rrule += f";COUNT={int(count)*4}"  # Approximate weeks in a month
            elif unit.startswith('YEAR'):
                rrule += f";COUNT={int(count)*52}" # Approximate weeks in a year
        return rrule
    raise ValueError(f"Could not parse recurrence: {recurrence_string}")

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

def update_events(
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
    except HttpError as error:
        raise ValueError(f"Failed to update event: {str(error)}")
    


    
