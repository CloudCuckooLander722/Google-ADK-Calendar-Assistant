"""
The Streamlit app's bridge to root_agent (services/adk_service.py), run end to end
with scripted models (see adk_scripted.py): no Gemini, no Google account.

root_agent pauses on questions (exam date, plan review, where to save a document,
deleting a folder). The chat only sends text, so run_adk_async must:
  - show the question as the reply when a run pauses,
  - turn the next message into the answer (answer_from_reply, one structured
    Gemini call, stubbed here) and resume the paused step with it,
  - run the message as a new request when it doesn't answer the question.

    cd my-adk-agent && ../.venv/bin/python -m pytest adk_agents_tests/adk_service_test.py -v
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("ENVIRONMENT", "development")
if not os.environ.get("GOOGLE_CREDS_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["GOOGLE_CREDS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from google.adk import Runner
from google.adk.sessions import InMemorySessionService

from adk_scripted import Call, Reply, install_scripts
from root_agent_e2e_test import CHAPTER_10, HANGOUT, PLAN, FakeGoogle, _schedule_plan_script

import adk_agents.root_agent as root
import services.adk_service as service
import tools.calendar_tools as calendar_tools
from config.settings import APP_NAME_FOR_ADK


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(calendar_tools, "_calendar_service", lambda: fake)
    monkeypatch.setattr(calendar_tools, "_tasks_service", lambda: fake)
    monkeypatch.setattr(calendar_tools, "get_user_timezone", lambda: "America/Los_Angeles")
    return fake


@pytest.fixture
def script(monkeypatch):
    return lambda **scripts: install_scripts(monkeypatch, root.root_agent, **scripts)


@pytest.fixture
def answers(monkeypatch):
    """Stubs answer_from_reply: answers.queue holds what it returns, answers.seen what it was asked."""
    class Answers:
        queue, seen = [], []
    stub = Answers()
    stub.queue, stub.seen = [], []

    def fake(question, reply):
        stub.seen.append((question, reply))
        return stub.queue.pop(0)

    monkeypatch.setattr(service, "answer_from_reply", fake)
    return stub


class Chat:
    """The app's view: one ADK session, messages through run_adk_sync like the chat UI."""

    def __init__(self):
        self.runner = Runner(agent=root.root_agent, app_name=APP_NAME_FOR_ADK, session_service=InMemorySessionService())
        self.user, self.session_id = "tester", uuid.uuid4().hex
        asyncio.run(self.runner.session_service.create_session(
            app_name=APP_NAME_FOR_ADK, user_id=self.user, session_id=self.session_id))

    def send(self, text: str) -> str:
        return service.run_adk_sync(self.runner, self.user, self.session_id, text)

    def session(self):
        return asyncio.run(self.runner.session_service.get_session(
            app_name=APP_NAME_FOR_ADK, user_id=self.user, session_id=self.session_id))


def test_the_app_runs_the_new_root_agent():
    assert service.root_agent is root.root_agent


def test_a_direct_request_replies_with_the_agents_answer(script, google, answers):
    script(
        router_agent=[Reply({"category": "SCHEDULING", "confidence": 0.95})],
        command_classifier=[Reply({"category": "SCHEDULE_EVENT", "confidence": 0.95, "request": HANGOUT})],
        schedule_event_agent=[
            Call("create_event", summary="Hangout with Kaoruko Waguri",
                 start_datetime="2026-11-21T14:00:00", end_datetime="2026-11-21T16:00:00"),
            "Created: Hangout with Kaoruko Waguri, Sat 2-4 PM.",
        ],
    )
    assert Chat().send(HANGOUT) == "Created: Hangout with Kaoruko Waguri, Sat 2-4 PM."
    assert answers.seen == [], "no question was pending, so nothing is read as an answer"


def test_questions_are_shown_and_text_replies_resume_the_workflow(script, google, answers):
    script(router_agent=[Reply({"category": "CONSULTING", "confidence": 0.9})],
           consultant_agent=[Reply(PLAN)], **_schedule_plan_script())
    chat = Chat()

    reply = chat.send(CHAPTER_10)
    assert "exam or due date" in reply, "the deadline question is shown as the reply"
    assert service.pending_question(chat.session())[0] == "DEADLINE"

    answers.queue.append({"deadline": "Nov 24, 2026"})
    reply = chat.send("it's on the 24th")
    question, text = answers.seen[-1]
    assert text == "it's on the 24th" and question["response_schema"], "the answer is read against the question's schema"
    assert PLAN["summary"] in reply and "schedule" in reply.lower(), "then the plan review is shown"
    assert service.pending_question(chat.session())[0] == "REVIEW_DECISION"

    answers.queue.append({"action": "SCHEDULE"})
    reply = chat.send("yes, put it on my calendar")
    assert len(google.created_events) == 3
    assert reply == "Scheduled 3 study sessions."
    assert service.pending_question(chat.session()) is None


def test_cancelling_shows_the_function_steps_reply(script, google, answers):
    script(router_agent=[Reply({"category": "CONSULTING", "confidence": 0.9})], consultant_agent=[Reply(PLAN)])
    chat = Chat()
    chat.send(CHAPTER_10)
    answers.queue += [{"deadline": "Nov 24"}, {"action": "CANCEL"}]
    chat.send("Nov 24")
    assert chat.send("never mind").startswith("I understand")
    assert google.created_events == []


def test_a_message_that_isnt_an_answer_runs_as_a_new_request(script, google, answers):
    script(router_agent=[Reply({"category": "CONSULTING", "confidence": 0.9}),
                         Reply({"category": "UNKNOWN", "confidence": 0.95})])
    chat = Chat()
    chat.send(CHAPTER_10)
    answers.queue.append(None)  # answer_from_reply: not an answer
    reply = chat.send("what's the capital of France?")
    assert "couldn't determine the category" in reply


def test_pending_question_counts_repeated_ids():
    from google.adk.events import Event
    from google.genai import types

    def call():
        return Event(author="root_agent", content=types.Content(role="model", parts=[types.Part(
            function_call=types.FunctionCall(id="REVIEW_DECISION", name="adk_request_input", args={"message": "ok?"}))]))

    def reply():
        return Event(author="user", content=types.Content(role="user", parts=[types.Part(
            function_response=types.FunctionResponse(id="REVIEW_DECISION", name="adk_request_input", response={}))]))

    class S:
        events = [call(), reply()]
    assert service.pending_question(S) is None
    S.events.append(call())
    assert service.pending_question(S) == ("REVIEW_DECISION", {"message": "ok?"})


def test_answer_from_reply_reads_the_answer_with_the_questions_schema(monkeypatch):
    sent = {}

    class FakeModels:
        def generate_content(self, model, contents, config):
            sent.update(model=model, contents=contents, schema=config.response_json_schema)
            return type("R", (), {"parsed": {"answers_the_question": True, "answer": {"deadline": "Nov 24"}}, "text": ""})()

    class FakeClient:
        models = FakeModels()

    import google.genai
    monkeypatch.setattr(google.genai, "Client", lambda: FakeClient())
    schema = {"type": "object", "properties": {"deadline": {"type": "string"}}}
    answer = service.answer_from_reply({"message": "When is the exam?", "response_schema": schema}, "the 24th")
    assert answer == {"deadline": "Nov 24"}
    assert sent["schema"]["properties"]["answer"] == schema
    assert "When is the exam?" in sent["contents"] and "the 24th" in sent["contents"]

    FakeModels.generate_content = lambda self, model, contents, config: type(
        "R", (), {"parsed": {"answers_the_question": False, "answer": {}}, "text": ""})()
    assert service.answer_from_reply({"message": "When is the exam?", "response_schema": schema}, "hi") is None


def _depleted_credits():
    from google.genai import errors
    return errors.ClientError(402, {"error": {"code": 402, "status": "RESOURCE_EXHAUSTED",
                                              "message": "Your prepayment credits are depleted."}})


def _fail_with(monkeypatch, chat, exc):
    async def run_async(**kwargs):
        raise exc
        yield  # makes this an async generator, like Runner.run_async
    monkeypatch.setattr(chat.runner, "run_async", run_async)


def test_out_of_gemini_credits_says_so_instead_of_a_generic_error(monkeypatch):
    """The live app's 402 arrives wrapped: DynamicNodeFailError(error=ClientError(402))."""
    from google.adk.workflow._errors import DynamicNodeFailError
    chat = Chat()
    _fail_with(monkeypatch, chat, DynamicNodeFailError(
        message="Dynamic node root_agent failed", error=_depleted_credits(), error_node_path="root_agent"))
    assert chat.send("schedule a reminder") == service.QUOTA_ERROR_REPLY


def test_a_rate_limit_found_through_the_exception_chain_says_so(monkeypatch):
    from google.genai import errors
    chat = Chat()
    try:
        try:
            raise errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}})
        except errors.ClientError as cause:
            raise RuntimeError("model call failed") from cause
    except RuntimeError as wrapped:
        _fail_with(monkeypatch, chat, wrapped)
    assert chat.send("hi") == service.QUOTA_ERROR_REPLY


def test_other_errors_keep_the_generic_reply(monkeypatch):
    from google.genai import errors
    chat = Chat()
    _fail_with(monkeypatch, chat, errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}}))
    assert chat.send("hi") == service.GENERIC_ERROR_REPLY
