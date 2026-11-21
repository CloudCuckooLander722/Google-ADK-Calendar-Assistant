import os
import sys
from pathlib import Path
from typing import Literal, Optional, Union

from google.adk import Context, Event, Workflow
from google.adk.agents import LlmAgent
from google.adk.workflow import START
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:

    from google.genai import types
except Exception:
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)


    from google.genai import types

from tools.calendar_tools import (
    parse_natural_language_datetime,
    parse_recurrence,
    list_calendars,
    get_event,
    create_event,
    update_event,
    delete_event,
    search_events,
    list_events,
    suggest_meeting_times,
    create_task,
    patch_task,
    delete_task,
    search_tasks,
)
from tools.maps_tools import resolve_address
from agents.tool_errors import report_tool_error


MODEL = "gemini-2.5-flash"

# AGENTS.md 1.21: every incoming request is sorted into one of these categories.
# UPDATE_EVENT / UPDATE_TASK keep the edit/move support the previous calendar_agent had.
CATEGORIES = (
    "SCHEDULE_EVENT",
    "UPDATE_EVENT",
    "DELETE_EVENT",
    "LIST_EVENTS",
    "SCHEDULE_TASK",
    "UPDATE_TASK",
    "DELETE_TASK",
    "LIST_TASKS",
)

CalendarCategory = Literal[
    "SCHEDULE_EVENT",
    "UPDATE_EVENT",
    "DELETE_EVENT",
    "LIST_EVENTS",
    "SCHEDULE_TASK",
    "UPDATE_TASK",
    "DELETE_TASK",
    "LIST_TASKS",
    "UNKNOWN",
]


# categorizes the request from the context window into a calendar command
class CalendarCommand(BaseModel):
    category: CalendarCategory
    confidence: float = Field(
        description="Probability score between 0.0 and 1.0 indicating confidence in this category."
    )
    request: str = Field(
        description=(
            "The full request restated as one self-contained instruction, with every detail the user "
            "gave or the conversation already settled: title, date, start/end time or duration, "
            "location, attendees, recurrence, notes, and which existing event/task it refers to."
        )
    )
    missing_info: Optional[str] = Field(
        default=None,
        description="What essential detail is missing (e.g. 'no date given'), or null if nothing is missing.",
    )


# classifier agent... sorts requests the way router_agent does for root_agent
command_classifier = LlmAgent(
    name="command_classifier",
    model=MODEL,
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    instruction=(
        "You sort calendar requests for the calendar_agent. Read the request (and the conversation "
        "so far, to resolve words like 'it', 'that', or 'move it to 3') and classify it into exactly one "
        "category, with a probability confidence (0.0 to 1.0):\n"
        "- 'SCHEDULE_EVENT': create a calendar event that has a time or time range/duration "
        "(e.g., 'Schedule an appointment with Dr. Chong from 3:20 PM - 4:00 PM', "
        "'Schedule a barbecue for Walter White Saturday at noon', 'a 2-hour study block Friday at 3pm'). "
        "Requests to suggest or find a free time for a meeting also go here.\n"
        "- 'UPDATE_EVENT': change, move, rename, or reschedule an existing event.\n"
        "- 'DELETE_EVENT': cancel or remove an existing event.\n"
        "- 'LIST_EVENTS': show, list, or search events (e.g., 'what's on my calendar tomorrow?').\n"
        "- 'SCHEDULE_TASK': create a to-do that has only a due date/deadline and no time range "
        "(e.g., 'remind me to finish PIQ 3 by Oct 21', 'add a task to buy milk').\n"
        "- 'UPDATE_TASK': change an existing task, including marking it done.\n"
        "- 'DELETE_TASK': remove an existing task.\n"
        "- 'LIST_TASKS': show, list, or search tasks / to-dos.\n"
        "- 'UNKNOWN': anything that maps to none of the above.\n"
        "A 'block' means an event OR a task: use the event categories when it has a time range, the "
        "task categories when it only has a deadline. If a request lists several items of the same kind "
        "(e.g., a plan of work sessions), it is still one category; keep every item in `request`.\n"
        "Fill `request` with a self-contained restatement that includes every detail, and set "
        "`missing_info` only when something essential is genuinely absent (e.g., no date at all for a new event)."
    ),
    output_schema=CalendarCommand,
    output_key="calendar_command",
)

CONFIDENCE_THRESHOLD = 0.60


def command_router(node_input: Union[CalendarCommand, dict], ctx: Context) -> Event:
    """
    Evaluates the category and probability from command_classifier and emits
    an ADK Event(route=...) that sends the restated request to the executor
    for that category.
    """
    if not node_input:
        return Event(route="UNKNOWN", output="")

    command = (
        node_input
        if isinstance(node_input, CalendarCommand)
        else CalendarCommand.model_validate(node_input)
    )
    ctx.state["last_calendar_command"] = command.model_dump()

    if command.category in CATEGORIES and command.confidence >= CONFIDENCE_THRESHOLD:
        return Event(route=command.category, output=command.request)

    return Event(route="UNKNOWN", output=command.missing_info or command.request)


def ask_for_clarification(node_input: Optional[str], ctx: Context) -> str:
    """Terminal node for requests that don't map to a calendar command."""
    command = ctx.state.get("last_calendar_command") or {}
    missing = command.get("missing_info")
    if missing:
        return f"I need a bit more information before I can do that: {missing}. Could you add it?"
    return (
        "I'm sorry, I couldn't tell which calendar action you want. I can schedule, update, delete, "
        "or list events and tasks. Could you rephrase your request?"
    )


# --- Shared instructions for every executor ------------------------------------

COMMON_INSTRUCTIONS = """
You operate in the user's local time zone.

General:
- Always show times to the user in their local time zone, with the timezone abbreviation; convert to UTC only for API requests.
- Use `parse_natural_language_datetime` to turn dates, times, and durations into ISO datetimes. For "next [day]", use the next occurrence.
- If `parse_natural_language_datetime` raises "Could not parse date/time", the text wasn't a date at all: tell the user you couldn't understand the date/time and ask them to rephrase. Do not guess a date.
- Keep responses short and user-friendly; no raw JSON.

Tool errors:
- A tool result with `"status": "error"` means that one call failed; its `tool`, `arguments`, and `error` fields say which and why.
- When handling several items, keep going with the rest after a failure, and don't retry the same failing arguments more than once.
- At the end, say which items succeeded and which failed: for each failure, name the item (title and date), the step that failed, and the error in plain words.
- If the error says the user is not signed in, tell them to sign in again instead of retrying.
"""

CALENDAR_SELECTION = """
Calendar selection:
- When the user hasn't named a calendar, call `list_calendars`, compare the request's topic with each calendar's `summary`, and use the `id` of the best semantic match; if no name is a clear match, use the calendar with `primary: True`.
- Do this silently; only ask if two calendars are equally strong matches.
"""


def _executor(name: str, description: str, instruction: str, tools: list) -> LlmAgent:
    return LlmAgent(
        name=name,
        model=MODEL,
        generate_content_config=types.GenerateContentConfig(temperature=0.0),
        on_tool_error_callback=report_tool_error,
        description=description,
        instruction=COMMON_INSTRUCTIONS + instruction,
        tools=tools,
    )


# --- SCHEDULE_EVENT -------------------------------------------------------------

schedule_event_agent = _executor(
    name="schedule_event_agent",
    description="Creates Google Calendar events, and suggests free meeting times.",
    instruction=CALENDAR_SELECTION + """
Scheduling events:
- Collect the title, start time, and end time or duration. If only a start is given, assume 1 hour.
- Parse times with `parse_natural_language_datetime` (pass the duration when one is given).
- If the user names a place or landmark (e.g., "the Cheesecake Factory", "Dr. Chong's office"), resolve it with `resolve_address` and use that address as `location`. Only set a location if the user gave one.
- Always pass a `description`: use the user's if they gave one; otherwise write a short one from the title and context (who, what, why, anything to bring or prepare). If the title is focused work or 'DEEP WORK', write a ~350-word description on how to achieve that specific goal. If the request includes plan steps, put those steps in the description.
- For recurring events, convert the recurrence (e.g., "every Tuesday for 5 weeks") with `parse_recurrence` and pass the RRULE.
- For attendees, pass emails as [{"email": "bob@example.com"}, ...].
- If the request contains several events (e.g., a plan of work sessions), create each one.
- If the user asks for a free time instead of a fixed one, use `suggest_meeting_times`, offer 2-3 slots, and ask which to book; don't create the event until they choose.
- After creating, confirm the title, start and end in local time, and the event link, then one short Cal Newport-style line: what was created, why it matters, and what attention it protects.
""",
    tools=[
        parse_natural_language_datetime,
        parse_recurrence,
        list_calendars,
        resolve_address,
        suggest_meeting_times,
        create_event,
    ],
)

# --- UPDATE_EVENT ---------------------------------------------------------------

update_event_agent = _executor(
    name="update_event_agent",
    description="Finds an existing Google Calendar event and changes it.",
    instruction=CALENDAR_SELECTION + """
Updating events:
- Find the event with `search_events` (keywords plus a time window parsed with `parse_natural_language_datetime`), or `get_event` if the ID is known. Search the likely calendar(s) from `list_calendars` if it isn't on the primary one.
- If several events match and it's not clear which one is meant, list them (title, local time) and ask; don't guess.
- Parse new times with `parse_natural_language_datetime`, recurrence with `parse_recurrence`, and places with `resolve_address`.
- Call `update_event` with only the changed fields. Set `send_updates` to "all" if the event has attendees, otherwise "none".
- Confirm the updated details in local time.
""",
    tools=[
        parse_natural_language_datetime,
        parse_recurrence,
        list_calendars,
        resolve_address,
        search_events,
        get_event,
        update_event,
    ],
)

# --- DELETE_EVENT ---------------------------------------------------------------

delete_event_agent = _executor(
    name="delete_event_agent",
    description="Finds an existing Google Calendar event and deletes it.",
    instruction=CALENDAR_SELECTION + """
Deleting events:
- Find the event with `search_events` (keywords plus a time window parsed with `parse_natural_language_datetime`). Search the likely calendar(s) from `list_calendars` if it isn't on the primary one.
- Delete only when exactly one event clearly matches. If several match, or the match is uncertain, list the candidates (title, local time) and ask which one; never delete more than the user asked for.
- Call `delete_event` with the event ID and its calendar ID. Set `send_updates` to "all" if the event has attendees, otherwise "none".
- Confirm what was deleted (title and local time).
""",
    tools=[
        parse_natural_language_datetime,
        list_calendars,
        search_events,
        get_event,
        delete_event,
    ],
)

# --- LIST_EVENTS ----------------------------------------------------------------

list_events_agent = _executor(
    name="list_events_agent",
    description="Lists and searches Google Calendar events.",
    instruction="""
Listing events:
- For "upcoming events" with no window, use `list_events`.
- For a specific window (e.g., "tomorrow", "this week") or keywords, use `search_events` with `time_min`/`time_max` from `parse_natural_language_datetime` and `query` for keywords.
- To cover every calendar, call `list_calendars` and search each one; label each event with its calendar name when there is more than one.
- For "important" or "top priority" requests, fetch the window first, then rank the results yourself (deadlines, exams, interviews, submissions, events with attendees rank higher), and say the ranking is inferred.
- Show each event's local time and title. If nothing is found, say so plainly.
""",
    tools=[
        parse_natural_language_datetime,
        list_calendars,
        list_events,
        search_events,
    ],
)

# --- SCHEDULE_TASK --------------------------------------------------------------

schedule_task_agent = _executor(
    name="schedule_task_agent",
    description="Creates Google Tasks with an optional due date.",
    instruction="""
Scheduling tasks:
- Use the user's phrase as the task title and parse any due date/time with `parse_natural_language_datetime`.
- If a specific time is given (e.g., "feed the cat at 6 PM"), pass it as `due` (ISO datetime). If only a date is given, pass the date as YYYY-MM-DD, which makes it an all-day task.
- Always pass `notes`: the user's notes if they gave any, otherwise a short summary of what the task is for.
- If the request contains several tasks, create each one.
- Confirm the title and due date/time in local time (say when it's all-day), then one short Cal Newport-style line naming the next concrete commitment.
""",
    tools=[
        parse_natural_language_datetime,
        create_task,
    ],
)

# --- UPDATE_TASK ----------------------------------------------------------------

update_task_agent = _executor(
    name="update_task_agent",
    description="Finds an existing Google Task and changes it or marks it complete.",
    instruction="""
Updating tasks:
- Find the task with `search_tasks`. If several match and it's not clear which one is meant, list them and ask.
- Parse new due dates with `parse_natural_language_datetime` (YYYY-MM-DD for all-day).
- To mark a task done, set `status` to "completed"; to reopen it, "needsAction".
- Call `patch_task` with only the changed fields, then confirm the result.
""",
    tools=[
        parse_natural_language_datetime,
        search_tasks,
        patch_task,
    ],
)

# --- DELETE_TASK ----------------------------------------------------------------

delete_task_agent = _executor(
    name="delete_task_agent",
    description="Finds an existing Google Task and deletes it.",
    instruction="""
Deleting tasks:
- Find the task with `search_tasks`.
- Delete only when exactly one task clearly matches. If several match, or the match is uncertain, list them and ask which one; never delete more than the user asked for.
- Call `delete_task` with the task ID and confirm which task was deleted.
""",
    tools=[
        search_tasks,
        delete_task,
    ],
)

# --- LIST_TASKS -----------------------------------------------------------------

list_tasks_agent = _executor(
    name="list_tasks_agent",
    description="Lists and searches Google Tasks.",
    instruction="""
Listing tasks:
- Use `search_tasks` (with `query` for keywords). Hide completed tasks (`show_completed=False`) unless the user asks for them.
- For a date window (e.g., "due this week"), parse it with `parse_natural_language_datetime` and keep only tasks due in that window.
- For "important" or "top priority" requests, rank the results yourself (deadlines, exams, submissions rank higher) and say the ranking is inferred.
- Show each task's title and status. If nothing is found, say so plainly.
""",
    tools=[
        parse_natural_language_datetime,
        search_tasks,
    ],
)


calendar_agent = Workflow(
    name="calendar_agent",
    description="Schedules, updates, deletes, and lists events and tasks in Google Calendar and Google Tasks.",
    edges=[
        (START, command_classifier, command_router),

        (
            command_router,
            {
                "SCHEDULE_EVENT": schedule_event_agent,
                "UPDATE_EVENT": update_event_agent,
                "DELETE_EVENT": delete_event_agent,
                "LIST_EVENTS": list_events_agent,
                "SCHEDULE_TASK": schedule_task_agent,
                "UPDATE_TASK": update_task_agent,
                "DELETE_TASK": delete_task_agent,
                "LIST_TASKS": list_tasks_agent,
                "UNKNOWN": ask_for_clarification,
            },
            
        ),
    ]
)
