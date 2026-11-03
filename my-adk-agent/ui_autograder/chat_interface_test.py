"""
Autograder for ui/chat_interface.py: the signed-in workspace (chat beside the
calendar), and chat_navigation, which switches between it and the agenda page
(ui/to_do.py load_agenda_page).

Each function is checked against its expected output:

    _calendar_window()          -> (first of last month, first of the month after next), UTC
    _google_calendar_events()   -> events from every shown calendar, in its color
    _google_tasks()             -> tasks with a due date, as orange all-day blocks
    _calendar_blocks(...)       -> events + tasks for the calendar panel
    _attachment_name(...)       -> a file's name
    _cookie_value(...)          -> a cookie's exact text value (never a rounded number)
    restore_signed_in_user()    -> the user_id from the session, else the cookie
    _browser_tab_id()           -> this tab's id from sessionStorage
    current_conversation(...)   -> the same chat after a refresh, a new one in a new tab
    run_chat_interface()        -> the chat and calendar, or a sign-in link
    chat_navigation()           -> a visible bar switching between chat and agenda; the
                                   page shown is kept in ?view=, so a refresh stays on it

and the workflow through ui/login.py login_page: a returning user lands on the
navigation, can chat, switch to the agenda and back, and refresh on either page.

Pages run as real Streamlit scripts with AppTest. Only the outside world is
faked: Google Calendar / Tasks, the browser (cookies, sessionStorage, its date
and timezone) and the agent's reply.

    cd my-adk-agent && ../.venv/bin/python -m pytest ui_autograder/chat_interface_test.py -v
"""
import builtins
import datetime
import importlib
import os
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from streamlit.testing.v1 import AppTest

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

# --- Simulated data -----------------------------------------------------------

USER_ID = "109876543210987654321"
TODAY = "2026-11-03"
TIMEZONE = "America/Los_Angeles"
TAB_ID = "tab-1"

CALENDARS = [
    {"id": "primary-cal", "primary": True, "backgroundColor": "#039be5"},
    {"id": "work-cal", "selected": True, "backgroundColor": "#d50000"},
    {"id": "hidden-cal", "selected": False, "backgroundColor": "#33b679"},
]
EVENTS = {
    "primary-cal": [
        {"id": "event-1", "summary": "Team standup",
         "start": {"dateTime": f"{TODAY}T09:00:00-08:00"}, "end": {"dateTime": f"{TODAY}T09:15:00-08:00"}},
        {"id": "event-2", "start": {"date": TODAY}, "end": {"date": "2026-11-04"}},
    ],
    "work-cal": [
        {"id": "event-3", "summary": "Design review",
         "start": {"dateTime": f"{TODAY}T14:00:00-08:00"}, "end": {"dateTime": f"{TODAY}T15:00:00-08:00"}},
    ],
    "hidden-cal": [
        {"id": "event-4", "summary": "Should not show",
         "start": {"date": TODAY}, "end": {"date": "2026-11-04"}},
    ],
}
TASK_LISTS = [{"id": "@default"}, {"id": "errands"}]
TASKS = {
    "@default": [
        {"id": "task-1", "title": "Send the budget to Amy", "status": "needsAction", "due": f"{TODAY}T00:00:00.000Z"},
        {"id": "task-2", "title": "Book the dentist", "status": "completed", "due": f"{TODAY}T00:00:00.000Z"},
        {"id": "task-3", "title": "Someday", "status": "needsAction"},
    ],
    "errands": [
        {"id": "task-4", "status": "needsAction", "due": "2026-11-05T00:00:00.000Z"},
    ],
}


# --- Fakes for the outside world ----------------------------------------------

class FakeRequest:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class FakeCalendarService:
    """Calendar API: calendarList().list() and events().list(calendarId=...)."""

    def __init__(self, failing_calendars=()):
        self.failing_calendars = set(failing_calendars)
        self.calls = []

    def calendarList(self):
        class _CalendarList:
            def list(self):
                return FakeRequest({"items": list(CALENDARS)})
        return _CalendarList()

    def events(self):
        service = self

        class _Events:
            def list(self, **kwargs):
                service.calls.append(kwargs)
                calendar_id = kwargs["calendarId"]
                if calendar_id in service.failing_calendars:
                    return FakeRequest(error=RuntimeError("calendar unavailable"))
                if calendar_id == "primary":
                    calendar_id = "primary-cal"
                return FakeRequest({"items": list(EVENTS.get(calendar_id, []))})
        return _Events()


class FakeTasksService:
    """Tasks API: tasklists().list() and tasks().list(tasklist=...)."""

    def __init__(self):
        self.calls = []

    def tasklists(self):
        class _TaskLists:
            def list(self, **kwargs):
                return FakeRequest({"items": list(TASK_LISTS)})
        return _TaskLists()

    def tasks(self):
        service = self

        class _Tasks:
            def list(self, **kwargs):
                service.calls.append(kwargs)
                return FakeRequest({"items": list(TASKS.get(kwargs["tasklist"], []))})
        return _Tasks()


class FakeBrowser:
    """streamlit_js_eval: the browser's answers, by component key."""

    def __init__(self):
        self.answers = {"browser_tab_id": TAB_ID, "get_browser_date": TODAY}
        self.calls = []

    def __call__(self, js_expressions=None, key=None, **kwargs):
        self.calls.append(key)
        return self.answers.get(key)


class FakeAgent:
    """run_adk_sync: replies without calling Gemini, and records what it was sent."""

    def __init__(self):
        self.turns = []

    def __call__(self, runner, user_key, session_id, message):
        text = message.parts[0].text if not isinstance(message, str) else message
        self.turns.append({"user_key": user_key, "session_id": session_id, "text": text})
        return f"Reply to: {text}"


# --- The app under test -----------------------------------------------------------

@pytest.fixture
def browser():
    return FakeBrowser()


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
def services():
    return FakeCalendarService(), FakeTasksService()


@pytest.fixture
def app(monkeypatch, browser, agent, services):
    """ui.chat_interface, ui.to_do and ui.login, imported with the outside world faked."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    if not os.environ.get("GOOGLE_CREDS_ENCRYPTION_KEY"):
        monkeypatch.setenv("GOOGLE_CREDS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    try:
        chat = importlib.import_module("ui.chat_interface")
        to_do = importlib.import_module("ui.to_do")
        login = importlib.import_module("ui.login")
        adk = importlib.import_module("services.adk_service")
    except SyntaxError as error:
        pytest.fail(f"syntax error in {error.filename} at line {error.lineno}: {error.msg}")
    except Exception as error:
        pytest.fail(f"the chat interface does not import: {type(error).__name__}: {error}")

    calendar, tasks = services
    for module in (chat, to_do):
        monkeypatch.setattr(module, "streamlit_js_eval", browser)
        monkeypatch.setattr(module, "fetch_timezone", lambda: TIMEZONE)
        monkeypatch.setattr(module, "get_calendar_service", lambda: calendar)
        monkeypatch.setattr(module, "get_tasks_service", lambda: tasks)
    monkeypatch.setattr(chat, "get_api_key", lambda: "test-gemini-key")
    monkeypatch.setattr(chat, "run_adk_sync", agent)
    # Firestore isn't reachable from a test: the stored credentials "work".
    monkeypatch.setattr(login, "check_creds", lambda: object())

    # Every test starts with an empty server: no conversations, no cached calendar.
    adk._conversation_store.clear()
    chat._calendar_blocks.clear()
    return chat


# --- Pages of the app -----------------------------------------------------------

PAGE_SCRIPT = """
import builtins
builtins.CHAT_AUTOGRADER_PAGE()
"""


class Visit:
    """One browser tab loading the app: a fresh Streamlit session, like a page load
    or a refresh. Only the browser's cookies, tab id and URL carry over."""

    def __init__(self, tmp_path, body, cookies=None, query_params=None, session_state=None):
        self.outcome = {}

        def page():
            try:
                self.outcome["result"] = body()
            except Exception as error:
                self.outcome["error"] = error

        self.page = page
        script = tmp_path / "page.py"
        script.write_text(PAGE_SCRIPT)
        self.at = AppTest.from_file(str(script), default_timeout=60)
        if cookies is not None:
            self.at.session_state["cookies"] = dict(cookies)
        for key, value in (session_state or {}).items():
            self.at.session_state[key] = value
        for key, value in (query_params or {}).items():
            self.at.query_params[key] = value

    def run(self, action=None):
        builtins.CHAT_AUTOGRADER_PAGE = self.page
        try:
            (action or self.at.run)()
        finally:
            del builtins.CHAT_AUTOGRADER_PAGE
        return self

    @property
    def result(self):
        return self.outcome.get("result")

    @property
    def error(self):
        return self.outcome.get("error") or (self.at.exception[0].value if self.at.exception else None)

    def messages(self):
        return [m.markdown[0].value for m in self.at.chat_message]

    def markdown(self):
        return [m.value for m in self.at.markdown]

    def shows_agenda(self):
        return any('class="agenda-title"' in m for m in self.markdown())

    def shows_chat(self):
        return bool(self.at.chat_input)

    def send(self, text):
        return self.run(lambda: self.at.chat_input[0].set_value(text).run())

    def go_to(self, view):
        return self.run(lambda: self.at.radio(key="chat_view").set_value(view).run())


def _signed_in_visit(tmp_path, body, view=None, tab=TAB_ID):
    """A returning user's tab: the user_id cookie is set and sessionStorage has its tab id."""
    query = {"logged_in": "True", **({"view": view} if view else {})}
    return Visit(tmp_path, body, cookies={"user_id": USER_ID}, query_params=query,
                 session_state={"tab_id": tab})


def _run(tmp_path, body, **kwargs):
    visit = Visit(tmp_path, body, **kwargs).run()
    assert visit.error is None, f"raised {type(visit.error).__name__}: {visit.error}"
    return visit


# --- _calendar_window -------------------------------------------------------------

def test_calendar_window_spans_last_month_to_next_month(app):
    start, end = app._calendar_window()
    now = datetime.datetime.now(datetime.timezone.utc)
    this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    assert start.day == 1 and end.day == 1
    assert start.utcoffset() == end.utcoffset() == datetime.timedelta(0)
    assert start < this_month <= now < end
    assert (this_month - start).days in range(28, 32), "starts on the first of last month"
    assert (end - this_month).days in range(59, 63), "ends on the first of the month after next"


# --- _google_calendar_events ---------------------------------------------------------

def test_calendar_events_from_every_shown_calendar(app):
    blocks = app._google_calendar_events(*app._calendar_window())
    assert [b["title"] for b in blocks] == ["Team standup", "Untitled event", "Design review"]


def test_calendar_events_use_their_calendars_color(app):
    blocks = {b["title"]: b for b in app._google_calendar_events(*app._calendar_window())}
    assert blocks["Team standup"]["color"] == "#039be5"
    assert blocks["Design review"]["color"] == "#d50000"


def test_calendar_events_all_day_and_timed(app):
    blocks = {b["title"]: b for b in app._google_calendar_events(*app._calendar_window())}
    assert blocks["Untitled event"] == {**blocks["Untitled event"], "allDay": True, "start": TODAY, "end": "2026-11-04"}
    assert blocks["Team standup"]["allDay"] is False
    assert blocks["Team standup"]["start"] == f"{TODAY}T09:00:00-08:00"


def test_calendar_events_skip_a_calendar_that_fails(app, monkeypatch):
    monkeypatch.setattr(app, "get_calendar_service", lambda: FakeCalendarService(failing_calendars={"work-cal"}))
    titles = [b["title"] for b in app._google_calendar_events(*app._calendar_window())]
    assert titles == ["Team standup", "Untitled event"]


def test_calendar_events_when_signed_out(app, monkeypatch):
    monkeypatch.setattr(app, "get_calendar_service", lambda: None)
    assert app._google_calendar_events(*app._calendar_window()) == []


# --- _google_tasks --------------------------------------------------------------------

def test_tasks_with_a_due_date_from_every_list(app):
    blocks = app._google_tasks(*app._calendar_window())
    assert [b["title"] for b in blocks] == ["☐ Send the budget to Amy", "✓ Book the dentist", "☐ Untitled task"]


def test_tasks_are_orange_all_day_blocks_on_their_due_date(app):
    for block in app._google_tasks(*app._calendar_window()):
        assert block["allDay"] is True and block["color"] == app.TASK_COLOR
        datetime.date.fromisoformat(block["start"])


def test_tasks_when_signed_out(app, monkeypatch):
    monkeypatch.setattr(app, "get_tasks_service", lambda: None)
    assert app._google_tasks(*app._calendar_window()) == []


# --- _calendar_blocks -----------------------------------------------------------------

def test_calendar_blocks_are_events_then_tasks(app):
    window = app._calendar_window()
    blocks = app._calendar_blocks(USER_ID, 0, *window)
    assert blocks == app._google_calendar_events(*window) + app._google_tasks(*window)


# --- _attachment_name -----------------------------------------------------------------

def test_attachment_name(app):
    upload = type("UploadedFile", (), {"name": "photo.jpg"})()
    assert app._attachment_name(upload) == "photo.jpg"
    assert app._attachment_name("notes.txt") == "notes.txt"


# --- _cookie_value / restore_signed_in_user ---------------------------------------------

def test_cookie_value_reads_the_cookie(app, tmp_path):
    visit = _run(tmp_path, lambda: app._cookie_value(app.controller(), "user_id"), cookies={"user_id": USER_ID})
    assert visit.result == USER_ID


def test_cookie_value_never_returns_a_rounded_number(app, tmp_path):
    """The cookie component JSON-parses an all-digit id into a float: that isn't the id."""
    visit = _run(tmp_path, lambda: app._cookie_value(app.controller(), "user_id"),
                 cookies={"user_id": float(USER_ID)})
    assert visit.result is None or visit.result == USER_ID


def test_restore_signed_in_user_from_the_cookie(app, tmp_path):
    visit = _run(tmp_path, app.restore_signed_in_user, cookies={"user_id": USER_ID})
    assert visit.result == USER_ID
    assert visit.at.session_state["user_id"] == USER_ID


def test_restore_signed_in_user_prefers_the_session(app, tmp_path):
    visit = _run(tmp_path, app.restore_signed_in_user, cookies={"user_id": "someone-else"},
                 session_state={"user_id": USER_ID})
    assert visit.result == USER_ID


def test_restore_signed_in_user_when_signed_out(app, tmp_path):
    assert _run(tmp_path, app.restore_signed_in_user, cookies={}).result is None


# --- _browser_tab_id / current_conversation -----------------------------------------------

def test_browser_tab_id_from_session_storage(app, tmp_path):
    visit = _run(tmp_path, app._browser_tab_id)
    assert visit.result == TAB_ID
    assert visit.at.session_state["tab_id"] == TAB_ID


def test_browser_tab_id_without_javascript_falls_back(app, tmp_path, browser):
    """If the browser never answers, the page still works: the chat just won't survive a refresh."""
    browser.answers.pop("browser_tab_id")
    visit = _run(tmp_path, app._browser_tab_id, session_state={"tab_id_waits": app.MAX_TAB_ID_WAITS})
    assert visit.result.startswith("session_")


def test_current_conversation_same_tab_same_chat(app, tmp_path):
    runner = importlib.import_module("services.adk_service").initialize_adk()
    first = _run(tmp_path, lambda: app.current_conversation(runner, USER_ID), session_state={"tab_id": TAB_ID})
    again = _run(tmp_path, lambda: app.current_conversation(runner, USER_ID), session_state={"tab_id": TAB_ID})
    other = _run(tmp_path, lambda: app.current_conversation(runner, USER_ID), session_state={"tab_id": "tab-2"})
    assert again.result is first.result
    assert other.result is not first.result and other.result.messages == []


# --- run_chat_interface -------------------------------------------------------------------

def test_chat_interface_when_signed_out_offers_sign_in(app, tmp_path):
    visit = _run(tmp_path, app.run_chat_interface, cookies={})
    assert any("signed out" in w.value.lower() for w in visit.at.warning)
    assert not visit.shows_chat()


def test_chat_interface_shows_chat_and_calendar(app, tmp_path):
    visit = _signed_in_visit(tmp_path, app.run_chat_interface).run()
    assert visit.error is None, visit.error
    assert visit.shows_chat()
    assert "#### WorkFlow Assistant" in visit.markdown() and "#### Google Calendar" in visit.markdown()
    assert visit.messages() == [], "a new visit starts with a clean chat"


def test_chat_interface_sends_a_message_and_shows_the_reply(app, tmp_path, agent):
    visit = _signed_in_visit(tmp_path, app.run_chat_interface).run().send("Plan my Tuesday")
    assert visit.error is None, visit.error
    assert visit.messages() == ["Plan my Tuesday", "Reply to: Plan my Tuesday"]
    assert agent.turns[0]["user_key"] == USER_ID


def test_chat_interface_keeps_the_chat_after_a_refresh(app, tmp_path):
    _signed_in_visit(tmp_path, app.run_chat_interface).run().send("Plan my Tuesday")
    refreshed = _signed_in_visit(tmp_path, app.run_chat_interface).run()
    assert refreshed.messages() == ["Plan my Tuesday", "Reply to: Plan my Tuesday"]


def test_chat_interface_new_tab_starts_clean(app, tmp_path):
    _signed_in_visit(tmp_path, app.run_chat_interface).run().send("Plan my Tuesday")
    assert _signed_in_visit(tmp_path, app.run_chat_interface, tab="tab-2").run().messages() == []


# --- chat_navigation ----------------------------------------------------------------------

def test_navigation_is_visible_with_chat_and_agenda(app, tmp_path):
    visit = _signed_in_visit(tmp_path, app.chat_navigation).run()
    assert visit.error is None, visit.error
    nav = visit.at.radio(key="chat_view")
    assert list(nav.options) == ["💬 Chat", "📅 Agenda"]


def test_navigation_opens_on_the_chat(app, tmp_path):
    visit = _signed_in_visit(tmp_path, app.chat_navigation).run()
    assert visit.at.radio(key="chat_view").value == "chat"
    assert visit.shows_chat() and not visit.shows_agenda()
    assert visit.at.query_params["view"] == ["chat"]


def test_navigation_to_the_agenda(app, tmp_path):
    visit = _signed_in_visit(tmp_path, app.chat_navigation).run().go_to("agenda")
    assert visit.error is None, visit.error
    assert visit.shows_agenda() and not visit.shows_chat()
    assert {c.key for c in visit.at.checkbox} >= {"task_task-1", "event_event-1"}
    assert visit.at.query_params["view"] == ["agenda"]


def test_navigation_refresh_stays_on_the_agenda(app, tmp_path):
    visit = _signed_in_visit(tmp_path, app.chat_navigation, view="agenda").run()
    assert visit.error is None, visit.error
    assert visit.at.radio(key="chat_view").value == "agenda"
    assert visit.shows_agenda()


def test_navigation_back_to_the_chat_keeps_the_conversation(app, tmp_path):
    visit = _signed_in_visit(tmp_path, app.chat_navigation).run().send("Plan my Tuesday")
    visit.go_to("agenda").go_to("chat")
    assert visit.error is None, visit.error
    assert visit.messages() == ["Plan my Tuesday", "Reply to: Plan my Tuesday"]


def test_navigation_with_an_unknown_view_opens_the_chat(app, tmp_path):
    visit = _signed_in_visit(tmp_path, app.chat_navigation, view="nonsense").run()
    assert visit.shows_chat() and visit.at.query_params["view"] == ["chat"]


# --- The workflow from login_page ------------------------------------------------------------

def test_returning_user_lands_on_the_navigation(app, tmp_path):
    login_page = importlib.import_module("ui.login").login_page
    visit = _signed_in_visit(tmp_path, login_page).run()
    assert visit.error is None, visit.error
    assert visit.at.radio(key="chat_view").value == "chat" and visit.shows_chat()


def test_workflow_chat_agenda_refresh_and_back(app, tmp_path):
    login_page = importlib.import_module("ui.login").login_page
    visit = _signed_in_visit(tmp_path, login_page).run().send("Plan my Tuesday").go_to("agenda")
    assert visit.error is None, visit.error
    assert visit.shows_agenda()

    # Refresh on the agenda: the URL still says ?view=agenda.
    refreshed = _signed_in_visit(tmp_path, login_page, view="agenda").run()
    assert refreshed.error is None, refreshed.error
    assert refreshed.shows_agenda()

    # Back to the chat: the conversation is still there.
    refreshed.go_to("chat")
    assert refreshed.messages() == ["Plan my Tuesday", "Reply to: Plan my Tuesday"]


def test_signed_out_user_gets_the_login_screen_not_the_navigation(app, tmp_path, monkeypatch):
    login = importlib.import_module("ui.login")
    visit = _run(tmp_path, login.login_page, cookies={})
    assert not visit.at.radio, "no navigation without signing in"
    assert visit.at.query_params["logged_in"] == ["False"]
