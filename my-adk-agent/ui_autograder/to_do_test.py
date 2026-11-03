"""
Autograder for ui/to_do.py, the agenda page: today's tasks and events as
checkboxes, with the title and subtitles in Montserrat and the tasks and
events in Open Sans.

Each function is checked against its expected output:

    fetch_date()        -> today's date in the user's own timezone, "YYYY-MM-DD"
    list_times(...)     -> (timeMin, timeMax): the user's local midnight to the
                           next one, as RFC 3339 timestamps with a UTC offset
    _return_tasks(...)  -> the list of tasks due today (completed ones too)
    _return_events(...) -> the events.list result for today
    display_checks(...) -> the page: title, "Tasks" and "Events" subtitles, one
                           checkbox per task and event
    load_agenda_page()  -> all of the above, wired to the signed-in user's services

Pages run as real Streamlit scripts with AppTest. Only the outside world is
faked: the browser (streamlit_js_eval) and the Google Calendar / Tasks services.

    cd my-adk-agent && ../.venv/bin/python -m pytest ui_autograder/to_do_test.py -v

Set TO_DO_MODULE to grade a different module (default ui.to_do).
"""
import ast
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
MODULE_UNDER_TEST = os.environ.get("TO_DO_MODULE", "ui.to_do")

# --- Simulated data -----------------------------------------------------------

TODAY = "2026-11-03"
TIMEZONE = "America/Los_Angeles"

TASKS = [
    {"id": "task-1", "title": "Send the budget to Amy", "status": "needsAction", "due": f"{TODAY}T00:00:00.000Z"},
    {"id": "task-2", "title": "Book the dentist", "status": "completed", "due": f"{TODAY}T00:00:00.000Z"},
]
EVENTS = {
    "items": [
        {"id": "event-1", "summary": "Team standup",
         "start": {"dateTime": f"{TODAY}T09:00:00-08:00"}, "end": {"dateTime": f"{TODAY}T09:15:00-08:00"}},
        {"id": "event-2", "summary": "Lunch with Sam",
         "start": {"dateTime": f"{TODAY}T12:30:00-08:00"}, "end": {"dateTime": f"{TODAY}T13:30:00-08:00"}},
    ]
}


# --- Fakes for the outside world ----------------------------------------------

class FakeRequest:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class FakeTasksService:
    """googleapiclient's Tasks service: tasks().list(**kwargs).execute()."""

    def __init__(self, result=None, error=None):
        self.result = {"items": list(TASKS)} if result is None else result
        self.error = error
        self.calls = []

    def tasks(self):
        service = self

        class _Tasks:
            def list(self, **kwargs):
                service.calls.append(kwargs)
                return FakeRequest(service.result, service.error)
        return _Tasks()


class FakeCalendarService:
    """googleapiclient's Calendar service: events().list(**kwargs).execute()."""

    def __init__(self, result=None, error=None):
        self.result = dict(EVENTS) if result is None else result
        self.error = error
        self.calls = []

    def events(self):
        service = self

        class _Events:
            def list(self, **kwargs):
                service.calls.append(kwargs)
                return FakeRequest(service.result, service.error)
        return _Events()


class FakeBrowser:
    """streamlit_js_eval: answers each JavaScript expression like a browser would."""

    def __init__(self, today=TODAY):
        self.today = today
        self.calls = []

    def __call__(self, js_expressions=None, key=None, **kwargs):
        self.calls.append({"js": js_expressions, "key": key})
        return self.today


def _api_error():
    from googleapiclient.errors import HttpError
    response = type("Response", (), {"status": 403, "reason": "Forbidden"})()
    return HttpError(response, b'{"error": {"message": "Forbidden"}}')


# --- The module under test ----------------------------------------------------

def _module_path() -> Path:
    return APP_ROOT / (MODULE_UNDER_TEST.replace(".", "/") + ".py")


def _undefined_names(path: Path) -> list[str]:
    """Names the module loads but never binds anywhere (imports, defs, assignments, params)."""
    tree = ast.parse(path.read_text())
    # Names Python gives every module.
    defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__package__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            defined.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
    return sorted({
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in defined
    })


@pytest.fixture
def browser():
    return FakeBrowser()


@pytest.fixture
def to_do(monkeypatch, browser):
    """The module under test, imported with the outside world faked."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    if not os.environ.get("GOOGLE_CREDS_ENCRYPTION_KEY"):
        monkeypatch.setenv("GOOGLE_CREDS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    try:
        module = importlib.import_module(MODULE_UNDER_TEST)
    except SyntaxError as error:
        pytest.fail(f"{MODULE_UNDER_TEST} has a syntax error at line {error.lineno}, so no test can run: {error.msg}")
    except Exception as error:
        pytest.fail(f"{MODULE_UNDER_TEST} does not import: {type(error).__name__}: {error}")

    monkeypatch.setattr(module, "streamlit_js_eval", browser)
    monkeypatch.setattr(module, "fetch_timezone", lambda: TIMEZONE, raising=False)
    return module


# --- Pages of the app -----------------------------------------------------------

PAGE_SCRIPT = """
import builtins
builtins.TO_DO_AUTOGRADER_PAGE()
"""


def _run_page(tmp_path, body, session_state=None):
    """Runs `body()` as a Streamlit page; returns (AppTest, result, error)."""
    outcome = {}

    def page():
        try:
            outcome["result"] = body()
        except Exception as error:
            outcome["error"] = error

    script = tmp_path / "page.py"
    script.write_text(PAGE_SCRIPT)
    builtins.TO_DO_AUTOGRADER_PAGE = page
    try:
        at = AppTest.from_file(str(script), default_timeout=30)
        for key, value in (session_state or {}).items():
            at.session_state[key] = value
        at.run()
    finally:
        del builtins.TO_DO_AUTOGRADER_PAGE
    return at, outcome.get("result"), outcome.get("error")


def _describe(error) -> str:
    return f"{type(error).__name__}: {error}"


def _markdown(at) -> list[str]:
    return [m.value for m in at.markdown]


def _checkboxes(at) -> dict[str, tuple[str, bool]]:
    """{widget key: (label, checked)}"""
    return {c.key: (c.label, c.value) for c in at.checkbox}


# --- Module ---------------------------------------------------------------------

def test_module_defines_every_name_it_uses():
    path = _module_path()
    try:
        ast.parse(path.read_text())
    except SyntaxError as error:
        pytest.fail(f"syntax error at line {error.lineno}: {error.msg}")
    missing = _undefined_names(path)
    assert not missing, f"{path.name} uses names it never defines or imports: {', '.join(missing)}"


# --- fetch_date -------------------------------------------------------------------

def test_fetch_date_returns_the_browsers_date(to_do, tmp_path):
    _, result, error = _run_page(tmp_path, to_do.fetch_date)
    assert error is None, f"fetch_date() raised {_describe(error)}"
    assert result == TODAY


def test_fetch_date_asks_for_the_local_date_not_utc(to_do, tmp_path, browser):
    """toISOString() is UTC: in California after 4 pm it's already tomorrow."""
    _run_page(tmp_path, to_do.fetch_date)
    assert browser.calls, "fetch_date() should ask the browser for the date"
    assert "toISOString" not in browser.calls[0]["js"], "toISOString() gives the UTC date, not the user's"


def test_fetch_date_before_the_browser_answers_is_still_a_date_string(to_do, tmp_path, browser):
    """The browser answers one run late; until then a date string (not a datetime) keeps the page working."""
    browser.today = None
    _, result, error = _run_page(tmp_path, to_do.fetch_date, {"timezone": TIMEZONE})
    assert error is None, f"fetch_date() raised {_describe(error)}"
    assert isinstance(result, str), f"expected a 'YYYY-MM-DD' string, got {type(result).__name__}"
    datetime.date.fromisoformat(result)


# --- list_times --------------------------------------------------------------------

def test_list_times_spans_the_users_day(to_do):
    assert to_do.list_times(TODAY, TIMEZONE) == ("2026-11-03T00:00:00-08:00", "2026-11-04T00:00:00-08:00")


def test_list_times_has_utc_offsets(to_do):
    """The Calendar API rejects timeMin/timeMax without an offset (400 Bad Request)."""
    for value in to_do.list_times(TODAY, TIMEZONE):
        assert datetime.datetime.fromisoformat(value).tzinfo is not None, f"{value} has no UTC offset"


def test_list_times_on_a_daylight_saving_change(to_do):
    """Clocks go back on 2026-11-01 in Los Angeles: the day starts at -07:00 and ends at -08:00."""
    assert to_do.list_times("2026-11-01", TIMEZONE) == ("2026-11-01T00:00:00-07:00", "2026-11-02T00:00:00-08:00")


def test_list_times_without_a_timezone_uses_utc(to_do):
    assert to_do.list_times(TODAY, None) == ("2026-11-03T00:00:00+00:00", "2026-11-04T00:00:00+00:00")


# --- _return_tasks ------------------------------------------------------------------

def test_return_tasks_returns_todays_tasks(to_do):
    assert to_do._return_tasks(FakeTasksService(), TODAY) == TASKS


def test_return_tasks_asks_for_tasks_due_today(to_do):
    service = FakeTasksService()
    to_do._return_tasks(service, TODAY)
    (call,) = service.calls
    assert call["tasklist"] == "@default"
    # Tasks only have a due date, stored as midnight UTC of that date.
    assert call["dueMin"] == f"{TODAY}T00:00:00Z"
    assert call["dueMax"] == f"{TODAY}T23:59:59Z"


def test_return_tasks_includes_completed_tasks(to_do):
    """Tasks completed in Google's own apps are hidden unless showHidden is set too."""
    service = FakeTasksService()
    to_do._return_tasks(service, TODAY)
    assert service.calls[0].get("showCompleted") is True
    assert service.calls[0].get("showHidden") is True


def test_return_tasks_with_no_tasks_due(to_do):
    assert to_do._return_tasks(FakeTasksService(result={}), TODAY) == []


def test_return_tasks_when_signed_out(to_do):
    assert to_do._return_tasks(None, TODAY) == []


def test_return_tasks_when_google_refuses(to_do):
    assert to_do._return_tasks(FakeTasksService(error=_api_error()), TODAY) == []


# --- _return_events ----------------------------------------------------------------

def test_return_events_returns_the_events_result(to_do):
    time_min, time_max = "2026-11-03T00:00:00-08:00", "2026-11-04T00:00:00-08:00"
    assert to_do._return_events(TIMEZONE, time_min, time_max, FakeCalendarService()) == EVENTS


def test_return_events_asks_for_todays_events_in_order(to_do):
    service = FakeCalendarService()
    time_min, time_max = "2026-11-03T00:00:00-08:00", "2026-11-04T00:00:00-08:00"
    to_do._return_events(TIMEZONE, time_min, time_max, service)
    (call,) = service.calls
    assert call["calendarId"] == "primary"
    assert call["timeMin"] == time_min and call["timeMax"] == time_max
    assert call["timeZone"] == TIMEZONE
    # Recurring events expanded into today's occurrence, earliest first.
    assert call["singleEvents"] is True and call["orderBy"] == "startTime"


def test_return_events_when_signed_out(to_do):
    assert to_do._return_events(TIMEZONE, "a", "b", None) == {"items": []}


def test_return_events_when_google_refuses(to_do):
    assert to_do._return_events(TIMEZONE, "a", "b", FakeCalendarService(error=_api_error())) == {"items": []}


# --- display_checks (the page) ------------------------------------------------------

def _display(to_do, tmp_path, tasks=TASKS, events=EVENTS, today=TODAY):
    at, _, error = _run_page(tmp_path, lambda: to_do.display_checks(tasks, events, today))
    assert error is None, f"display_checks() raised {_describe(error)}"
    assert not at.exception, f"the page raised {[e.value for e in at.exception]}"
    return at


def test_display_shows_the_title_with_todays_date(to_do, tmp_path):
    at = _display(to_do, tmp_path)
    assert any('class="agenda-title"' in m and f"Agenda for {TODAY}" in m for m in _markdown(at))


def test_display_shows_tasks_then_events_subtitles(to_do, tmp_path):
    subtitles = [m for m in _markdown(_display(to_do, tmp_path)) if 'class="agenda-subtitle"' in m]
    assert [("Tasks" in m, "Events" in m) for m in subtitles] == [(True, False), (False, True)]


def test_display_title_and_subtitles_are_montserrat(to_do, tmp_path):
    css = "\n".join(_markdown(_display(to_do, tmp_path)))
    for selector in (".agenda-title", ".agenda-subtitle"):
        rule = css[css.index(selector):]
        rule = rule[:rule.index("}")]
        assert "Montserrat" in rule, f"{selector} should use Montserrat"
    assert "family=Montserrat" in css, "Montserrat must be loaded from Google Fonts"


def test_display_tasks_and_events_are_open_sans(to_do, tmp_path):
    css = "\n".join(_markdown(_display(to_do, tmp_path)))
    rule = css[css.index('[data-testid="stCheckbox"]'):]
    rule = rule[:rule.index("}")]
    assert "Open Sans" in rule, "checkbox labels (tasks and events) should use Open Sans"
    assert "family=Open+Sans" in css, "Open Sans must be loaded from Google Fonts"


def test_display_one_checkbox_per_task_checked_if_done(to_do, tmp_path):
    boxes = _checkboxes(_display(to_do, tmp_path))
    assert boxes["task_task-1"] == ("Send the budget to Amy", False)
    assert boxes["task_task-2"] == ("Book the dentist", True)


def test_display_one_checkbox_per_event(to_do, tmp_path):
    boxes = _checkboxes(_display(to_do, tmp_path))
    assert boxes["event_event-1"][0] == "Team standup"
    assert boxes["event_event-2"][0] == "Lunch with Sam"


def test_display_untitled_tasks_and_events(to_do, tmp_path):
    boxes = _checkboxes(_display(to_do, tmp_path, tasks=[{"id": "t"}], events={"items": [{"id": "e"}]}))
    assert boxes["task_t"][0] == "Untitled task"
    assert boxes["event_e"][0] == "Untitled event"


def test_display_says_when_nothing_is_due(to_do, tmp_path):
    at = _display(to_do, tmp_path, tasks=[], events={"items": []})
    shown = " ".join(_markdown(at) + [c.value for c in at.caption])
    assert "No tasks due today" in shown and "No events today" in shown
    assert not at.checkbox


def test_display_escapes_the_date(to_do, tmp_path):
    at = _display(to_do, tmp_path, today="<script>x</script>")
    assert not any("<script>x</script>" in m for m in _markdown(at))


# --- load_agenda_page -------------------------------------------------------------

@pytest.fixture
def services(monkeypatch, to_do):
    calendar, tasks = FakeCalendarService(), FakeTasksService()
    monkeypatch.setattr(to_do, "get_calendar_service", lambda: calendar)
    monkeypatch.setattr(to_do, "get_tasks_service", lambda: tasks)
    return calendar, tasks


def test_load_agenda_page_shows_the_users_tasks_and_events(to_do, tmp_path, services):
    at, _, error = _run_page(tmp_path, to_do.load_agenda_page)
    assert error is None, f"load_agenda_page() raised {_describe(error)}"
    assert not at.exception, f"the page raised {[e.value for e in at.exception]}"
    assert set(_checkboxes(at)) == {"task_task-1", "task_task-2", "event_event-1", "event_event-2"}


def test_load_agenda_page_asks_for_the_users_day(to_do, tmp_path, services):
    calendar, tasks = services
    _run_page(tmp_path, to_do.load_agenda_page)
    assert calendar.calls[0]["timeMin"] == "2026-11-03T00:00:00-08:00"
    assert calendar.calls[0]["timeZone"] == TIMEZONE
    assert tasks.calls[0]["dueMin"] == f"{TODAY}T00:00:00Z"


def test_load_agenda_page_asks_the_browser_for_the_date_once(to_do, tmp_path, services, browser):
    """Each call renders a component with the same key; a second one in a run is a Streamlit error."""
    _run_page(tmp_path, to_do.load_agenda_page)
    assert len(browser.calls) == 1, f"the date was asked for {len(browser.calls)} times in one run"


def test_load_agenda_page_when_signed_out(to_do, tmp_path, monkeypatch):
    monkeypatch.setattr(to_do, "get_calendar_service", lambda: None)
    monkeypatch.setattr(to_do, "get_tasks_service", lambda: None)
    at, _, error = _run_page(tmp_path, to_do.load_agenda_page)
    assert error is None, f"load_agenda_page() raised {_describe(error)}"
    assert not at.checkbox
    assert any("sign in" in w.value.lower() for w in at.warning), "tell the user to sign in"
