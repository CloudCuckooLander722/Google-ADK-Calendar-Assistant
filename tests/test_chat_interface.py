"""run_chat_interface across a soft refresh: the calendar panel shows the user's
events again, and the agent's calendar tools can still change their calendar.

Only Google itself is faked (the Calendar/Tasks APIs, Firestore and the browser
cookie). The real restore_signed_in_user, fetch_creds, calendar panel fetch and
the agent's create_event tool all run."""
import builtins
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_ROOT = Path(__file__).resolve().parent.parent / "my-adk-agent"
sys.path.insert(0, str(APP_ROOT))

CHAT_APP = '''
import builtins
import sys
from unittest import mock
import streamlit as st
sys.path.insert(0, {app_root!r})
import ui.chat_interface as ci
import google_oauth.oauth_login as ol
from tools import calendar_tools

google = builtins.FAKE_GOOGLE


class Cookies:
    """The browser only hands its cookies over on the cookie component's
    second run, as the real one does after a refresh."""
    cookies = {{}}
    def get_all(self, key=None):
        st.session_state["cookie_runs"] = st.session_state.get("cookie_runs", 0) + 1
        self.cookies = {{}} if st.session_state["cookie_runs"] < 2 else dict(google["cookies"])
        return self.cookies
    def set(self, *a, **k): pass
    def delete(self, *a, **k): pass


class Request:
    def __init__(self, result):
        self.result = result
    def execute(self):
        if google.get("api_error"):
            raise RuntimeError("Google API unavailable")
        return self.result() if callable(self.result) else self.result


class Events:
    def __init__(self, user):
        self.user = user
    def list(self, calendarId, **kwargs):
        google["event_lists"] += 1
        return Request(lambda: {{"items": list(google["events"].get(self.user, []))}})
    def insert(self, calendarId, body):
        def insert():
            google["events"].setdefault(self.user, []).append(body)
            return {{"htmlLink": "https://calendar.google.com/event?eid=1"}}
        return Request(insert)


class Service:
    def __init__(self, user):
        self.user = user
    def calendarList(self):
        return mock.Mock(list=lambda: Request({{"items": [{{"id": "primary", "primary": True}}]}}))
    def events(self):
        return Events(self.user)


def fake_credentials(user_id):
    google["credential_lookups"].append(user_id)
    return mock.Mock(user=user_id) if user_id in google["stored_users"] else None


def fake_agent(runner, user_key, session_id, message):
    """Stands in for Gemini: it calls the agent's real create_event tool."""
    if google.get("agent_tool") != "create_event":
        return "Done."
    try:
        return calendar_tools.create_event(
            "Dentist", "2026-10-20T09:00:00", "2026-10-20T10:00:00",
        )
    except Exception as error:  # ADK hands tool errors back to the model
        return f"Tool error: {{error}}"


def fake_calendar(events, **kwargs):
    st.session_state["shown_events"] = events


class EmptyLocalStorage:
    """AppTest has no browser to run the local-storage component in."""
    def __init__(self, key=None): pass
    def getItem(self, key): return None
    def getAll(self): return {{}}
    def setItem(self, *a, **k): pass


with mock.patch.object(ol, "LocalStorage", EmptyLocalStorage), \\
     mock.patch.object(ol, "get_cookie_manager", lambda: st.session_state.setdefault("cookie_manager", Cookies())), \\
     mock.patch.object(ol, "get_valid_credentials", fake_credentials), \\
     mock.patch.object(ol, "build", lambda name, version, credentials: Service(credentials.user)), \\
     mock.patch.object(ci, "get_tasks_service", lambda: None), \\
     mock.patch.object(ci, "get_api_key", lambda: google.get("api_key", "key")), \\
     mock.patch.object(ci, "fetch_timezone", lambda: st.session_state.setdefault("timezone", "America/Los_Angeles")), \\
     mock.patch.object(ci, "_attachment_menu", lambda n: google.get("attachments") or {{"screenshots": [], "documents": [], "recording": None, "google_drive": []}}), \\
     mock.patch.object(ci, "run_adk_sync", fake_agent), \\
     mock.patch.object(ci, "st_calendar", fake_calendar):
    ci.run_chat_interface()
'''


@pytest.fixture
def google(tmp_path):
    import ui.chat_interface as ci
    from services import adk_service
    ci._calendar_blocks.clear()
    adk_service._conversation_store.clear()
    builtins.FAKE_GOOGLE = {
        "cookies": {"user_id": "alice"},
        "stored_users": {"alice", "bob"},
        "events": {"alice": [{"summary": "Standup", "start": {"date": "2026-10-19"}, "end": {"date": "2026-10-20"}}]},
        "event_lists": 0,
        "credential_lookups": [],
    }
    app = tmp_path / "chat_app.py"
    app.write_text(CHAT_APP.format(app_root=str(APP_ROOT)))
    builtins.FAKE_GOOGLE["app"] = str(app)
    yield builtins.FAKE_GOOGLE
    ci._calendar_blocks.clear()
    adk_service._conversation_store.clear()
    del builtins.FAKE_GOOGLE


def _open_page(google):
    """A browser loading (or soft-refreshing) the page: a new session, then the
    automatic rerun the cookie component triggers once the cookies arrive."""
    at = AppTest.from_file(google["app"], default_timeout=15)
    at.run()
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    return at


def _titles(at):
    return [e["title"] for e in at.session_state["shown_events"]]


def _send(at, prompt):
    at.chat_input[0].set_value(prompt).run()
    assert not at.exception, [e.message for e in at.exception]
    return at


def test_calendar_waits_for_cookie_on_first_run_after_refresh(google):
    at = AppTest.from_file(google["app"], default_timeout=15).run()
    assert not at.exception
    assert at.session_state["shown_events"] == []
    assert any("Loading your calendar" in c.value for c in at.caption)
    at.run()
    assert _titles(at) == ["Standup"]


def test_soft_refresh_still_shows_calendar_events(google):
    _open_page(google)
    refreshed = _open_page(google)
    assert refreshed.session_state["user_id"] == "alice"
    assert _titles(refreshed) == ["Standup"]


def test_agent_can_update_calendar_after_soft_refresh(google):
    _open_page(google)
    refreshed = _open_page(google)

    google["agent_tool"] = "create_event"
    _send(refreshed, "Book the dentist Tuesday at 9")

    # The agent's tool used the restored user's own credentials...
    assert google["events"]["alice"][-1]["summary"] == "Dentist"
    assert google["events"]["alice"][-1]["start"] == {"dateTime": "2026-10-20T09:00:00", "timeZone": "America/Los_Angeles"}
    assert "Event created" in refreshed.chat_message[-1].markdown[0].value
    # ...and the calendar panel shows the new event right away, not after the cache expires.
    assert _titles(refreshed) == ["Standup", "Dentist"]


def test_agent_change_and_chat_survive_another_refresh(google):
    at = _open_page(google)
    google["agent_tool"] = "create_event"
    _send(at, "Book the dentist")

    refreshed = _open_page(google)
    assert _titles(refreshed) == ["Standup", "Dentist"]
    assert [m.markdown[0].value for m in refreshed.chat_message][0] == "Book the dentist"


def test_refresh_reuses_cached_calendar_instead_of_calling_google(google):
    _open_page(google)
    lists_before = google["event_lists"]
    _open_page(google)
    assert google["event_lists"] == lists_before


def test_each_user_sees_only_their_own_calendar(google):
    _open_page(google)
    google["cookies"] = {"user_id": "bob"}
    bob = _open_page(google)
    assert bob.session_state["user_id"] == "bob"
    assert bob.session_state["shown_events"] == []


def test_no_cookie_shows_empty_calendar_and_asks_to_sign_in(google):
    google["cookies"] = {}
    at = _open_page(google)
    assert at.session_state["shown_events"] == []
    assert any("sign in again" in c.value for c in at.caption)
    assert google["credential_lookups"] == []


def test_cookie_for_user_without_stored_credentials_shows_empty_calendar(google):
    google["cookies"] = {"user_id": "deleted-user"}
    at = _open_page(google)
    assert at.session_state["shown_events"] == []
    assert "deleted-user" in google["credential_lookups"]


def test_agent_without_credentials_reports_error_instead_of_crashing(google):
    google["cookies"] = {}
    at = _open_page(google)
    google["agent_tool"] = "create_event"
    _send(at, "Book the dentist")
    assert "Not signed in" in at.chat_message[-1].markdown[0].value
    assert "alice" not in google["credential_lookups"]
    assert google["events"]["alice"][-1]["summary"] == "Standup"


def test_google_api_errors_show_empty_calendar_not_a_crash(google):
    google["api_error"] = True
    at = _open_page(google)
    assert at.session_state["shown_events"] == []


def test_missing_api_key_stops_with_error(google):
    google["api_key"] = None
    at = AppTest.from_file(google["app"], default_timeout=15).run()
    assert any("Google API Key Not Found" in e.value for e in at.error)
    assert "shown_events" not in at.session_state


def test_unsendable_attachment_shows_reason_in_chat(google):
    at = _open_page(google)
    bad = type("Upload", (), {"name": "virus.exe", "type": "application/x-msdownload", "getvalue": lambda self: b"MZ"})()
    google["attachments"] = {"screenshots": [], "documents": [bad], "recording": None, "google_drive": []}
    _send(at, "Read this")
    assert "couldn't send that" in at.chat_message[-1].markdown[0].value
