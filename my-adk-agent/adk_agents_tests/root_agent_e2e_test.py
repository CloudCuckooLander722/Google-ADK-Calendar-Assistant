"""
End-to-end runs of adk_agents/root_agent.py through a real ADK Runner, with no
Gemini and no Google account.

Every LlmAgent in the graph gets a ScriptedLlm in place of Gemini: it replies
with preset answers (route decisions, tool calls, plans), one per model call,
and records what each agent was sent. The Calendar / Tasks APIs are replaced by
FakeGoogle, which records every event and task created.

What runs for real is everything ADK does between those calls: the Workflow
graph and its routes, state and output_key, the nested calendar_agent Workflow,
tool calls, and pausing on RequestInput then resuming from the user's
function_response. Each AGENTS.md 1.12 test case is one test:

    Hangout       router -> calendar_agent -> create_event
    Chapter 10    router -> ask DEADLINE -> consultant -> ask REVIEW_DECISION
                  -> SCHEDULE -> calendar_agent -> one event per milestone
    Document A    router -> workspace -> consultant -> REVISE -> consultant
                  -> SCHEDULE -> calendar_agent
    Problem set   ... -> CREATE_DOCS -> workspace -> consultant -> SCHEDULE
    Cancel        ... -> CANCEL -> reply, nothing created
    Off topic     router -> reply

    cd my-adk-agent && ../.venv/bin/python -m pytest adk_agents_tests/root_agent_e2e_test.py -v
"""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("ENVIRONMENT", "development")
if not os.environ.get("GOOGLE_CREDS_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["GOOGLE_CREDS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from google.adk import Runner
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types

import adk_agents.root_agent as root
import adk_agents.calendar_agent as cal
import adk_agents.consultant_agent as consultant
import adk_agents.workspace_agent as workspace
import tools.calendar_tools as calendar_tools


# --- Scripted model -------------------------------------------------------------

class Call:
    """A tool call the scripted model makes."""

    def __init__(self, name: str, **args):
        self.name, self.args = name, args


class Reply:
    """Structured output (output_schema): sent via set_model_response when the agent has tools."""

    def __init__(self, data: dict):
        self.data = data


def _request_text(request: LlmRequest) -> str:
    return "\n".join(p.text for c in request.contents or [] for p in c.parts or [] if p.text)


class ScriptedLlm(BaseLlm):
    """Answers each model call with the next step of its script."""

    model: str = "scripted"
    agent: str = ""
    script: list[Any] = []
    requests: list[str] = []

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(_request_text(llm_request))
        if not self.script:
            raise AssertionError(f"{self.agent} was called more times than scripted; last request:\n{self.requests[-1]}")
        step = self.script.pop(0)
        if isinstance(step, Call):
            part = types.Part(function_call=types.FunctionCall(name=step.name, args=step.args))
        elif isinstance(step, Reply):
            if "set_model_response" in llm_request.tools_dict:
                part = types.Part(function_call=types.FunctionCall(name="set_model_response", args=step.data))
            else:
                part = types.Part(text=json.dumps(step.data))
        else:
            part = types.Part(text=str(step))
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


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


# --- Wiring the scripts in ---------------------------------------------------------

def _llm_nodes(workflow) -> dict:
    """Every LlmAgent the workflow will actually run, nested Workflows included.
    (Workflow keeps its own copies of the agents, so patching the module-level
    agents would not reach them.)"""
    found = {}
    for n in workflow.graph.nodes:
        if hasattr(n, "graph") and n.graph is not None:
            found.update(_llm_nodes(n))
        elif hasattr(n, "instruction") and hasattr(n, "model"):
            found[n.name] = n
    return found


@pytest.fixture
def script(monkeypatch):
    """script(router_agent=[...], ...) gives each agent its scripted answers; returns the models.
    An agent with no script fails the test if it is called."""
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)  # nothing in this file may reach Gemini
    models = {}

    def install(**scripts):
        nodes = _llm_nodes(root.root_agent)
        unknown = set(scripts) - set(nodes)
        assert not unknown, f"no such agents in root_agent: {unknown}"
        for name, agent in nodes.items():
            model = ScriptedLlm(agent=name, script=list(scripts.get(name, [])))
            monkeypatch.setattr(agent, "model", model)
            models[name] = model
        return models

    return install


class Session:
    """One conversation with root_agent through a real Runner."""

    def __init__(self):
        self.app, self.user, self.id = "root_agent_e2e", "tester", uuid.uuid4().hex
        self.service = InMemorySessionService()
        asyncio.run(self.service.create_session(app_name=self.app, user_id=self.user, session_id=self.id))
        self.runner = Runner(agent=root.root_agent, app_name=self.app, session_service=self.service)
        self.events = []

    def _run(self, message: types.Content):
        async def go():
            out = []
            async for event in self.runner.run_async(user_id=self.user, session_id=self.id, new_message=message):
                out.append(event)
            return out
        new = asyncio.run(go())
        self.events += new
        return new

    def say(self, text: str):
        return self._run(types.Content(role="user", parts=[types.Part(text=text)]))

    def answer(self, interrupt_id: str, response: dict):
        """Reply to a RequestInput the way the UI must: a function_response with the interrupt's id."""
        part = types.Part(function_response=types.FunctionResponse(
            id=interrupt_id, name="adk_request_input", response=response))
        return self._run(types.Content(role="user", parts=[part]))

    @property
    def state(self) -> dict:
        session = asyncio.run(self.service.get_session(app_name=self.app, user_id=self.user, session_id=self.id))
        return session.state


def pending_question(events) -> tuple[str, dict] | None:
    """(interrupt id, args) of the RequestInput the run paused on, if any."""
    for event in reversed(events):
        for call in event.get_function_calls():
            if call.name == "adk_request_input":
                return call.id, call.args or {}
    return None


def final_text(events) -> str:
    for event in reversed(events):
        if event.content and event.content.parts:
            text = "".join(p.text for p in event.content.parts if p.text and not p.thought)
            if text.strip():
                return text
        if isinstance(getattr(event, "output", None), str) and event.output.strip():
            return event.output
    return ""


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


def test_document_a_revise_sends_the_note_back_then_schedules(script, google):
    revised = dict(PLAN, summary="Revised: evenings only.")
    models = script(
        router_agent=[Reply({"category": "WORKSPACE", "confidence": 0.9})],
        workspace_agent=["Document A lists 3 plans: Ch 10.1-10.2, Ch 10.3-10.4, a practice test."],
        consultant_agent=[Reply(PLAN), Reply(revised)],
        **_schedule_plan_script(revised),
    )
    chat = Session()

    question = pending_question(chat.say(DOCUMENT_A))
    assert question and question[0] == "REVIEW_DECISION"
    assert "Document A lists 3 plans" in models["consultant_agent"].requests[0], "workspace output reaches the consultant"

    # Revise: back to the consultant with the note, then a second review (same interrupt id).
    question = pending_question(chat.answer("REVIEW_DECISION", {"action": "REVISE", "note": "evenings only"}))
    assert question and question[0] == "REVIEW_DECISION", "the revised plan is reviewed again"
    assert "evenings only" in models["consultant_agent"].requests[-1]
    assert "Revised: evenings only." in json.dumps(question[1]), "the second review shows the revised plan"
    assert google.created_events == []

    chat.answer("REVIEW_DECISION", {"action": "SCHEDULE", "note": ""})
    assert len(google.created_events) == 3
    assert "Revised: evenings only." in models["command_classifier"].requests[0], "the revised plan is the one scheduled"


def test_problem_set_can_create_documents_before_scheduling(script, google):
    models = script(
        router_agent=[Reply({"category": "WORKSPACE", "confidence": 0.9})],
        workspace_agent=["Drafted a 15-question derivatives problem set.",
                         "Created 'Ch 10 study guide' (not saved to Drive yet)."],
        consultant_agent=[Reply(PLAN), Reply(PLAN)],
        **_schedule_plan_script(),
    )
    chat = Session()
    chat.say(PROBLEM_SET)

    question = pending_question(chat.answer("REVIEW_DECISION", {"action": "CREATE_DOCS", "note": ""}))
    assert len(models["workspace_agent"].requests) == 2, "CREATE_DOCS runs workspace_agent again"
    assert "Create the documents" in models["workspace_agent"].requests[-1]
    assert question and question[0] == "REVIEW_DECISION", "after the documents, the plan is reviewed again"

    chat.answer("REVIEW_DECISION", {"action": "SCHEDULE", "note": ""})
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
