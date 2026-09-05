from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)

from google_oauth.oauth_login import get_calendar_service, get_tasks_service

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
from streamlit_js_eval import streamlit_js_eval
import geoip2.database

# 1. Timezone Handling: Use pytz and tzlocal to manage timezones and create times.

def get_user_timezone() -> str:
    """
    Get the user's local timezone.
    
    Returns:
    The user's local timezone as a string (e.g., "America/New_York")"""
    try:
        with geoip2.database.Reader('GeoLite2-City.mmdb') as reader:
            response = reader.city(streamlit_js_eval("return window.location.hostname;"))
            timezone = response.location.time_zone
            if timezone:
                return str(timezone)
    except Exception as e:
        print(f"Error occurred while fetching user timezone: {e}")
        return "UTC"


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

    parsed_datetime = parsed_datetime.astimezone(user_timezone) #change
    start_datetime = parsed_datetime.isoformat().replace('+00:00', 'Z')

    if duration:
        duration_minutes = parse_duration(duration)
        end_datetime = (parsed_datetime + datetime.timedelta(minutes=duration_minutes)).isoformat().replace('+00:00', 'Z')
    else:
        end_datetime = (parsed_datetime + datetime.timedelta(hours=1)).isoformat().replace('+00:00', 'Z')

    return start_datetime, end_datetime, time_window

def parse_duration(duration: str) -> int:
    """
    Parse a duration string into minutes.
    
    Args:
        duration: Duration string (e.g., "30 minutes", "for 1 hour").
    
    Returns:
        Duration in minutes.
    
    Raises:
        ValueError: If duration cannot be parsed.
    """
    duration_match = re.match(r'(?:for\s+)?(\d+)\s*(hour|hours|minute|minutes)', duration, re.IGNORECASE)
    if duration_match:
        value, unit = duration_match.groups()
        value = int(value)
        return value * 60 if unit.lower().startswith('hour') else value
    raise ValueError(f"Could not parse duration: {duration}")



# Event Creation and Management: Functions to create, update, and delete events

def create_event(
    summary: str,
    start_datetime: str,
    end_datetime: str,
    location: str = "",
    description: str = "",
    recurrence: Optional[str] = None,
    attendees: Optional[List[Dict[str, str]]] = None
):
    user_timezone = get_user_timezone()
    service = get_calendar_service()
    event = {
        "summary": summary,
        "start": {"dateTime": start_datetime, "timeZone": user_timezone},
        "end": {"dateTime": end_datetime, "timeZone": user_timezone},
    }

    if location and location.strip() != "":
        event["location"] = location
    if description and description.strip() != "":
        event["description"] = description
    if recurrence:
        event["recurrence"] = [recurrence]
    if attendees:
        event["attendees"] = attendees

    try:
        created = service.events().insert(calendarId="primary", body=event).execute()
        return f"Event created: {created.get('htmlLink')}"
    except HttpError as error:
        raise ValueError(f"Failed to create event: {str(error)}")

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

def update_event(
    event_id: str,
    summary: Optional[str] = None,
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    location: Optional[str] = None,
    description: Optional[str] = None,
    recurrence: Optional[str] = None,
    attendees: Optional[List[Dict[str, str]]] = None,
    calendar_id: str = "primary",
    send_updates: str = "none" # "all", "externalOnly", or "none"
) -> str:
    service = get_calendar_service()
    update_body = {}

    # Conditionally add fields to update_body only if they are provided
    if summary is not None:
        update_body["summary"] = summary
    if start_datetime is not None:
        update_body["start"] = {"dateTime": start_datetime, "timeZone": get_user_timezone()}
    if end_datetime is not None:
        update_body["end"] = {"dateTime": end_datetime, "timeZone": get_user_timezone()}
    if location is not None:
        update_body["location"] = location
    if description is not None:
        update_body["description"] = description
    if recurrence is not None:
        update_body["recurrence"] = [recurrence]
    if attendees is not None:
        update_body["attendees"] = attendees

    if not update_body:
        raise ValueError("No fields provided to update.")

    try:
        updated = service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=update_body,
            sendUpdates=send_updates # Control notifications to attendees
        ).execute()
        return f"Event updated: {updated.get('htmlLink')}"
    except HttpError as error:
        raise ValueError(f"Failed to update event: {str(error)}")

def delete_event(event_id: str, calendar_id: str = "primary", send_updates: str = "none") -> str:
    service = get_calendar_service()
    try:
        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates=send_updates
        ).execute()
        return "Event deleted successfully."
    except HttpError as error:
        raise ValueError(f"Failed to delete event: {str(error)}")

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

def suggest_meeting_times(
    date_string: str,
    duration: Optional[str] = "1 hour",
    time_preference: Optional[str] = None,
    calendar_id: str = "primary",
    max_suggestions: int = 3
) -> List[str]:
    """
    Suggest available meeting times based on calendar free/busy status.
    
    Args:
        date_string: Target date (e.g., "next Tuesday").
        duration: Meeting duration (e.g., "1 hour", "30 minutes").
        time_preference: Optional time window (e.g., "morning", "9 AM to 2 PM").
        calendar_id: Calendar ID (default: "primary").
        max_suggestions: Maximum number of suggested slots.
    
    Returns:
        List of formatted time slots in local time zone (e.g., "2025-09-23 10:00 AM IST").
    """
    service = get_calendar_service()
    user_timezone = get_user_timezone()
    user_tz = pytz.timezone(user_timezone)

    # Parse date and duration
    start_datetime, end_datetime, time_window = parse_natural_language_datetime(date_string, duration, time_preference)
    parsed_date = datetime.datetime.fromisoformat(start_datetime.replace('Z', '+00:00')).astimezone(user_tz)
    day_start = parsed_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + datetime.timedelta(days=1)

    # Parse duration
    duration_minutes = parse_duration(duration)

    # Query free/busy status
    body = {
        "timeMin": day_start.astimezone(pytz.UTC).isoformat(),
        "timeMax": day_end.astimezone(pytz.UTC).isoformat(),
        "items": [{"id": calendar_id}]
    }
    try:
        freebusy = service.freebusy().query(body=body).execute()
        busy_periods = freebusy.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    except HttpError as error:
        raise ValueError(f"Failed to query free/busy status: {str(error)}")

    # Convert busy periods to user TZ
    busy_slots = []
    for period in busy_periods:
        start = datetime.datetime.fromisoformat(period["start"].replace('Z', '+00:00')).astimezone(user_tz)
        end = datetime.datetime.fromisoformat(period["end"].replace('Z', '+00:00')).astimezone(user_tz)
        busy_slots.append((start, end))

    # Find free slots
    free_slots = []
    current_time = day_start
    while current_time + datetime.timedelta(minutes=duration_minutes) <= day_end:
        slot_end = current_time + datetime.timedelta(minutes=duration_minutes)
        is_free = True
        for busy_start, busy_end in busy_slots:
            if not (slot_end <= busy_start or current_time >= busy_end):
                is_free = False
                break
        if is_free and (not time_window or (time_window[0] <= current_time.time() <= time_window[1])):
            free_slots.append(current_time)
        current_time += datetime.timedelta(minutes=30)  # Check every 30 minutes

    # Format suggestions
    if not free_slots:
        return [f"No available slots found for a {duration} meeting on {day_start.strftime('%Y-%m-%d')}. Would you like suggestions for another day or a shorter duration?"]
    
    formatted_slots = []
    for slot in free_slots[:max_suggestions]:
        slot_end = slot + datetime.timedelta(minutes=duration_minutes)
        formatted_slots.append(f"{slot.strftime('%Y-%m-%d %I:%M %p %Z')} - {slot_end.strftime('%I:%M %p %Z')}")
    return formatted_slots

