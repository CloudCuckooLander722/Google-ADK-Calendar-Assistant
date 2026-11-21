"""
End-to-end runs of adk_agents/root_agent.py through a real ADK Runner, with no
Gemini and no Google account.

Every LlmAgent in the graph gets a ScriptedLlm in place of Gemini: it replies
with preset answers (route decisions, tool calls, plans), one per model call,
and records what each agent was sent. The Calendar / Tasks APIs are replaced by
FakeGoogle and Drive / Slides by FakeDrive, which record everything created.
The shared helpers live in adk_scripted.py.

What runs for real is everything ADK does between those calls: the Workflow
graph and its routes, state and output_key, the nested calendar_agent and
workspace_agent Workflows, tool calls, and pausing on RequestInput (including
inside a nested workflow) then resuming from the user's function_response.
The AGENTS.md 1.12 test cases:

    Hangout       router -> calendar_agent -> create_event
    Chapter 10    router -> ask DEADLINE -> consultant -> ask REVIEW_DECISION
                  -> SCHEDULE -> calendar_agent -> one event per milestone
    Revise        ... -> REVISE -> consultant -> review again -> SCHEDULE
    Problem set   router -> workspace_agent (draft -> ask CONFIRM_FOLDER -> save)
                  -> consultant -> REVIEW_DECISION -> SCHEDULE
    Create docs   ... -> CREATE_DOCS -> workspace_agent -> review again -> SCHEDULE
    Document A    expected failure: workspace_agent can't read documents yet
    Cancel        ... -> CANCEL -> reply, nothing created
    Off topic     router -> reply

    cd my-adk-agent && ../.venv/bin/python -m pytest adk_agents_tests/root_agent_e2e_test.py -v
"""
import json
import os
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("ENVIRONMENT", "development")
if not os.environ.get("GOOGLE_CREDS_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["GOOGLE_CREDS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from adk_scripted import Call, FakeDrive, Reply, final_text, install_scripts, pending_question
from adk_scripted import Session as ScriptedSession

import adk_agents.root_agent as root
import adk_agents.calendar_agent as cal
import adk_agents.consultant_agent as consultant
import adk_agents.workspace_agent as workspace
import tools.calendar_tools as calendar_tools
import tools.workspace_tools as workspace_tools


# --- Fake Google APIs -------------------------------------------------------------

class _Collection:
    """service.events() / service.tasks() / service.calendarList(): records inserts."""

    def __init__(self, store: list, kind: str):
        self.store, self.kind, self._result = store, kind, None

    def insert(self, body, calendarId=None, tasklist=None):
        self.store.append(body)
        n = len(self.store)
        self._result = {"id": f"{self.kind}{n}", "title": body.get("title"),
                        "htmlLink": f"https://calendar.example/{self.kind}{n}"}
        return self

    def list(self, **params):
        self._result = {"items": [{"id": "primary", "summary": "Main", "primary": True}]}
        return self

    def execute(self):
        return self._result


class FakeGoogle:
    """Just enough of the Calendar and Tasks services for the executors' tools."""

    def __init__(self):
        self.created_events, self.created_tasks = [], []

    def events(self):
        return _Collection(self.created_events, "event")

    def tasks(self):
        return _Collection(self.created_tasks, "task")

    def calendarList(self):
        return _Collection([], "calendar")


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(calendar_tools, "_calendar_service", lambda: fake)
    monkeypatch.setattr(calendar_tools, "_tasks_service", lambda: fake)
    monkeypatch.setattr(calendar_tools, "get_user_timezone", lambda: "America/Los_Angeles")
    return fake


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(workspace_tools, "get_drive_service", lambda: fake)
    monkeypatch.setattr(workspace_tools, "get_slides_service", lambda: fake)
    return fake


# --- Wiring the scripts in ---------------------------------------------------------

@pytest.fixture
def script(monkeypatch):
    """script(router_agent=[...], ...) gives each agent its scripted answers; returns the models."""
    return lambda **scripts: install_scripts(monkeypatch, root.root_agent, **scripts)


def Session():
    """One conversation with root_agent through a real Runner."""
    return ScriptedSession(root.root_agent)


# --- Test cases --------------------------------------------------------------------

HANGOUT = "Schedule a hangout with Kaoruko Waguri at the Cheesecake Factory on Saturday from 2 PM to 4 PM."


def test_hangout_goes_straight_to_the_calendar(script, google):
    models = script(
        router_agent=[Reply({"category": "SCHEDULING", "confidence": 0.95})],
        command_classifier=[Reply({"category": "SCHEDULE_EVENT", "confidence": 0.95, "request": HANGOUT})],
        schedule_event_agent=[
            Call("create_event", summary="Hangout with Kaoruko Waguri",
                 start_datetime="2026-11-21T14:00:00", end_datetime="2026-11-21T16:00:00",
                 location="The Cheesecake Factory", description="Catch up with Kaoruko."),
            "Created: Hangout with Kaoruko Waguri, Sat Nov 21, 2:00-4:00 PM PST.",
        ],
    )
    chat = Session()
    events = chat.say(HANGOUT)

    assert pending_question(events) is None, "a ready-to-book event should not stop to ask anything"
    assert [e["summary"] for e in google.created_events] == ["Hangout with Kaoruko Waguri"]
    assert chat.state.get("user_prompt") == HANGOUT
    assert HANGOUT in models["command_classifier"].requests[0], "calendar_agent must receive the user's request"
    assert "Hangout with Kaoruko Waguri" in final_text(events)


CHAPTER_10 = "I want to schedule a plan to study Chapter 10 test for Acc Geo / Alg 2 Trig"
PLAN = {
    "summary": "Three sessions covering 10.1-10.6 before the test.",
    "intentions": "Schedule 3 study sessions before the Chapter 10 test on Nov 24.",
    "milestones": [
        {"title": "Ch 10.1-10.2 review", "date": "2026-11-17 4:00-5:00 PM", "kind": "event", "description": "p.512 #1-20"},
        {"title": "Ch 10.3-10.4 review", "date": "2026-11-19 4:00-5:00 PM", "kind": "event", "description": "p.530 #1-18"},
        {"title": "Ch 10 practice test", "date": "2026-11-22 4:00-5:30 PM", "kind": "event", "description": "Timed"},
    ],
    "citations": [],
    "links": [],
    "open_questions": None,
}


def _schedule_plan_script(plan=PLAN):
    """calendar_agent's side of scheduling an approved plan: classify, one create_event per milestone, reply."""
    times = {"2026-11-17": ("16:00", "17:00"), "2026-11-19": ("16:00", "17:00"), "2026-11-22": ("16:00", "17:30")}
    calls = []
    for m in plan["milestones"]:
        day = m["date"][:10]
        start, end = times[day]
        calls.append(Call("create_event", summary=m["title"], start_datetime=f"{day}T{start}:00",
                          end_datetime=f"{day}T{end}:00", description=m["description"]))
    return dict(
        command_classifier=[Reply({"category": "SCHEDULE_EVENT", "confidence": 0.9,
                                   "request": "Schedule the approved Chapter 10 plan"})],
        schedule_event_agent=calls + [f"Scheduled {len(calls)} study sessions."],
    )


def test_chapter_10_asks_for_the_date_then_reviews_then_schedules(script, google):
    plan_steps = _schedule_plan_script()
    models = script(
        router_agent=[Reply({"category": "CONSULTING", "confidence": 0.9})],
        consultant_agent=[Reply(PLAN)],
        **plan_steps,
    )
    chat = Session()

    # 1. The router sends it to ask_for_deadline, which pauses.
    question = pending_question(chat.say(CHAPTER_10))
    assert question and question[0] == "DEADLINE", "should stop and ask for the exam date"
    assert models["consultant_agent"].requests == [], "the consultant must wait for the date"

    # 2. The user gives the date: the consultant plans, and the review pauses.
    question = pending_question(chat.answer("DEADLINE", {"deadline": "Nov 24, 2026"}))
    assert question and question[0] == "REVIEW_DECISION", "the plan must be reviewed before scheduling"
    assert "Nov 24, 2026" in models["consultant_agent"].requests[0], "the consultant gets the exam date"
    assert CHAPTER_10 in models["consultant_agent"].requests[0], "the consultant gets the request"
    assert chat.state.get("consultant_plan", {}).get("milestones"), "output_key saves the plan"
    assert google.created_events == [], "nothing is scheduled before the user approves"

    # 3. The user approves: calendar_agent creates one event per milestone.
    events = chat.answer("REVIEW_DECISION", {"action": "SCHEDULE", "note": ""})
    assert pending_question(events) is None
    assert [e["summary"] for e in google.created_events] == [m["title"] for m in PLAN["milestones"]]
    assert "Ch 10.1-10.2 review" in models["command_classifier"].requests[0], "calendar_agent gets the approved plan"


DOCUMENT_A = ("I have plan from Document A. Scan it, list all of the plans, and then "
              "schedule events with steps in calendar description.")
PROBLEM_SET = "Make me a problem set for Calc I and schedule events"


def test_review_shows_the_plan_summary_and_intentions(script, google):
    script(router_agent=[Reply({"category": "CONSULTING", "confidence": 0.9})], consultant_agent=[Reply(PLAN)])
    chat = Session()
    chat.say(CHAPTER_10)
    _, args = pending_question(chat.answer("DEADLINE", {"deadline": "Nov 24"}))
    shown = json.dumps(args)
    assert PLAN["summary"] in shown and PLAN["intentions"] in shown and "Ch 10 practice test" in shown


def test_revise_sends_the_note_back_then_schedules(script, google):
    revised = dict(PLAN, summary="Revised: evenings only.")
    models = script(
        router_agent=[Reply({"category": "CONSULTING", "confidence": 0.9})],
        consultant_agent=[Reply(PLAN), Reply(revised)],
        **_schedule_plan_script(revised),
    )
    chat = Session()
    chat.say(CHAPTER_10)
    question = pending_question(chat.answer("DEADLINE", {"deadline": "Nov 24"}))
    assert question and question[0] == "REVIEW_DECISION"

    # Revise: back to the consultant with the note, then a second review (same interrupt id).
    question = pending_question(chat.answer("REVIEW_DECISION", {"action": "REVISE", "note": "evenings only"}))
    assert question and question[0] == "REVIEW_DECISION", "the revised plan is reviewed again"
    assert "evenings only" in models["consultant_agent"].requests[-1]
    assert "Revised: evenings only." in json.dumps(question[1]), "the second review shows the revised plan"
    assert google.created_events == []

    chat.answer("REVIEW_DECISION", {"action": "SCHEDULE", "note": ""})
    assert len(google.created_events) == 3
    assert "Revised: evenings only." in models["command_classifier"].requests[0], "the revised plan is the one scheduled"


@pytest.mark.xfail(strict=True, reason=(
    "workspace_agent has no category for reading a document (AGENTS.md 1.3 lists creation, folders "
    "and moving), so 'scan Document A' gets its clarification reply instead of Document A's plans"))
def test_document_a_plans_reach_the_consultant(script, google, drive):
    models = script(
        router_agent=[Reply({"category": "WORKSPACE", "confidence": 0.9})],
        workspace_router_agent=[Reply({"category": "UNKNOWN", "confidence": 0.8, "request": DOCUMENT_A})],
        consultant_agent=[Reply(PLAN)],
    )
    chat = Session()
    question = pending_question(chat.say(DOCUMENT_A))
    assert question and question[0] == "REVIEW_DECISION"
    assert "couldn't tell" not in models["consultant_agent"].requests[0], \
        "the consultant should get Document A's plans, not workspace_agent's clarification message"


PROBLEM_SET_DRAFT = {
    "title": "Derivatives Problem Set", "content": "## Problems\n1. d/dx x^3\n2. d/dx sin x",
    "works_cited": None, "file_type": "DOC", "folder_topic": "Calculus I", "answer_key": "1. 3x^2\n2. cos x",
}
STUDY_GUIDE_DRAFT = dict(PROBLEM_SET_DRAFT, title="Ch 10 Study Guide", answer_key=None, content="## Key ideas\n- chain rule")


def test_problem_set_is_saved_then_planned_then_scheduled(script, google, drive):
    """AGENTS.md: workspace_agent makes the problem set and files it, then the consultant plans, then the calendar."""
    models = script(
        router_agent=[Reply({"category": "WORKSPACE", "confidence": 0.9})],
        workspace_router_agent=[Reply({"category": "DOCUMENT_CREATION", "confidence": 0.9, "request": PROBLEM_SET})],
        content_agent=[Reply(PROBLEM_SET_DRAFT)],
        consultant_agent=[Reply(PLAN)],
        **_schedule_plan_script(),
    )
    chat = Session()

    # The nested workspace_agent pauses root_agent to ask where to save the problem set.
    question = pending_question(chat.say(PROBLEM_SET))
    assert question and question[0] == "CONFIRM_FOLDER", "workspace_agent asks before writing to Drive"
    assert drive.created() == [] and models["consultant_agent"].requests == []

    question = pending_question(chat.answer("CONFIRM_FOLDER", {"action": "USE_SUGGESTED", "include_answer_key": True}))
    [doc] = drive.created()
    assert doc["parents"] == [drive.folder_named("Calculus I")["id"]]
    assert question and question[0] == "REVIEW_DECISION", "then the consultant's plan is reviewed"
    assert "Derivatives Problem Set" in models["consultant_agent"].requests[0], "the consultant hears what was created"

    chat.answer("REVIEW_DECISION", {"action": "SCHEDULE"})
    assert len(google.created_events) == 3


def test_create_docs_runs_workspace_agent_then_reviews_again(script, google, drive):
    models = script(
        router_agent=[Reply({"category": "CONSULTING", "confidence": 0.9})],
        workspace_router_agent=[Reply({"category": "DOCUMENT_CREATION", "confidence": 0.9,
                                       "request": "Create a Ch 10 study guide"})],
        content_agent=[Reply(STUDY_GUIDE_DRAFT)],
        consultant_agent=[Reply(PLAN), Reply(PLAN)],
        **_schedule_plan_script(),
    )
    chat = Session()
    chat.say(CHAPTER_10)
    chat.answer("DEADLINE", {"deadline": "Nov 24"})

    question = pending_question(chat.answer("REVIEW_DECISION", {"action": "CREATE_DOCS"}))
    assert question and question[0] == "CONFIRM_FOLDER"
    assert "Create the documents" in models["workspace_router_agent"].requests[0]

    question = pending_question(chat.answer("CONFIRM_FOLDER", {"action": "CREATE_NEW", "folder_name": "Acc Geo Ch 10"}))
    assert [f["name"] for f in drive.created("folder")] == ["Acc Geo Ch 10"]
    assert question and question[0] == "REVIEW_DECISION", "after the documents, the plan is reviewed again"

    chat.answer("REVIEW_DECISION", {"action": "SCHEDULE"})
    assert len(google.created_events) == 3


def test_cancel_schedules_nothing(script, google):
    script(router_agent=[Reply({"category": "CONSULTING", "confidence": 0.9})], consultant_agent=[Reply(PLAN)])
    chat = Session()
    chat.say(CHAPTER_10)
    chat.answer("DEADLINE", {"deadline": "Nov 24"})
    events = chat.answer("REVIEW_DECISION", {"action": "CANCEL", "note": "not now"})
    assert pending_question(events) is None
    assert google.created_events == [] and google.created_tasks == []
    assert "I understand" in final_text(events)


def test_unsure_scheduling_goes_to_the_consultant_first(script, google):
    script(router_agent=[Reply({"category": "SCHEDULING", "confidence": 0.4})])
    question = pending_question(Session().say(HANGOUT))
    assert question and question[0] == "DEADLINE", "low confidence should plan first, not book"
    assert google.created_events == []


def test_off_topic_gets_a_reply_and_nothing_else(script, google):
    events = Session().say("What's the capital of France?") if script(
        router_agent=[Reply({"category": "UNKNOWN", "confidence": 0.99})]) else None
    assert pending_question(events) is None
    assert "couldn't determine the category" in final_text(events)
    assert google.created_events == []
