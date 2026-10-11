"""Tests for REQUIREMENTS.md 8.11: home page sign-in links, login_page routing,
and keeping the user's chat, credentials and calendar across a refresh."""
import re
import sys
from pathlib import Path
from unittest import mock

import pytest
from streamlit.testing.v1 import AppTest

APP_ROOT = Path(__file__).resolve().parent.parent / "my-adk-agent"
sys.path.insert(0, str(APP_ROOT))

import ui.login as login_module  # noqa: E402
import ui.chat_interface as chat_module  # noqa: E402
from ui.home import PAGE  # noqa: E402


def _links(html: str) -> dict[str, str]:
    """{link text: href} for every <a> in the home page."""
    return {
        re.sub(r"<[^>]+>", "", text).strip(): href
        for href, text in re.findall(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
    }


def test_home_page_sign_in_links_go_to_login_page():
    links = _links(PAGE)
    assert links["Log in or sign up"] == "/login_page"
    assert links["Try WorkFlow Assistant free"] == "/login_page"
    assert links["Privacy Policy"] == "/privacy"
    assert links["Home"] == "/"


def _fake_pages():
    """Stand-ins for the real sign-in card and chat, which need Google."""
    calls = []
    login = lambda: (calls.append("login"), login_module.st.markdown("SIGN-IN CARD"))  # noqa: E731
    chat = lambda: (calls.append("chat"), login_module.st.markdown("CHAT WORKSPACE"))  # noqa: E731
    return calls, login, chat


def _run_main(query_params=None, session_state=None):
    calls, login, chat = _fake_pages()
    with mock.patch.object(login_module, "login", login), \
         mock.patch.object(login_module, "run_chat_interface", chat), \
         mock.patch("ui.home.st.html"):
        at = AppTest.from_file(str(APP_ROOT / "main.py"), default_timeout=60)
        for key, value in (query_params or {}).items():
            at.query_params[key] = value
        for key, value in (session_state or {}).items():
            at.session_state[key] = value
        at.switch_page("main.py")  # the default page, "/"
        at.run()
        return at, calls


LOGIN_PAGE_APP = """
import sys
sys.path.insert(0, {app_root!r})
from ui.login import login_page
login_page()
"""


def _run_login_page(tmp_path, login, chat, query_params=None, session_state=None):
    app = tmp_path / "login_page_app.py"
    app.write_text(LOGIN_PAGE_APP.format(app_root=str(APP_ROOT)))
    with mock.patch.object(login_module, "login", login), \
         mock.patch.object(login_module, "run_chat_interface", chat):
        at = AppTest.from_file(str(app), default_timeout=60)
        for key, value in (query_params or {}).items():
            at.query_params[key] = value
        for key, value in (session_state or {}).items():
            at.session_state[key] = value
        return at.run()


def test_home_is_the_landing_page():
    at, calls = _run_main()
    assert not at.exception
    assert calls == []  # neither the sign-in card nor the chat


def test_login_page_shows_sign_in_when_logged_out(tmp_path):
    calls, login, chat = _fake_pages()
    at = _run_login_page(tmp_path, login, chat)
    assert not at.exception
    assert calls == ["login"]
    assert at.query_params["logged_in"] == ["False"]


@pytest.mark.parametrize("query_param", ["False", None])
def test_login_page_shows_sign_in_when_query_param_is_not_true(tmp_path, query_param):
    calls, login, chat = _fake_pages()
    _run_login_page(tmp_path, login, chat, query_params={"logged_in": query_param} if query_param else None)
    assert calls == ["login"]


def test_login_page_shows_chat_when_logged_in(tmp_path):
    calls, login, chat = _fake_pages()
    _run_login_page(tmp_path, login, chat, session_state={"logged_in": True})
    assert calls == ["chat"]


def test_login_page_keeps_user_signed_in_after_refresh(tmp_path):
    """A refresh empties session_state, but ?logged_in=True stays in the URL."""
    calls, login, chat = _fake_pages()
    at = _run_login_page(tmp_path, login, chat, query_params={"logged_in": "True"})
    assert calls == ["chat"]
    assert at.session_state["logged_in"] is True


def test_login_page_swaps_to_chat_once_sign_in_completes(tmp_path):
    calls = []

    def login_that_succeeds():
        calls.append("login")
        login_module.st.session_state.logged_in = True
        login_module.st.query_params.logged_in = "True"

    at = _run_login_page(tmp_path, login_that_succeeds, lambda: calls.append("chat"))
    assert not at.exception
    assert calls == ["login", "chat"]


def test_oauth_redirect_to_site_root_is_handed_to_login_page():
    """Google redirects back to "/" (the home page) with ?code=: the code must
    reach the login page so the sign-in completes."""
    at, calls = _run_main(query_params={"code": "abc", "state": "xyz"})
    assert not at.exception
    assert calls == ["login"]
    assert at.query_params["code"] == ["abc"]
    assert at.query_params["state"] == ["xyz"]


# --- Refresh keeps the chat, credentials and calendar (run_chat_interface) ---

CHAT_APP = '''
import sys
from unittest import mock
import streamlit as st
sys.path.insert(0, {app_root!r})
import ui.chat_interface as ci

def fake_restore():
    # The real one reads the user_id cookie, which survives a refresh.
    st.session_state["user_id"] = "alice"
    return "alice"

def fake_events(window_start, window_end):
    import builtins
    builtins.TEST_EVENT_FETCHES = getattr(builtins, "TEST_EVENT_FETCHES", 0) + 1
    return [{{"title": "Dentist for " + st.session_state["user_id"], "start": "2026-10-12", "allDay": True}}]

with mock.patch.object(ci, "restore_signed_in_user", fake_restore), \\
     mock.patch.object(ci, "get_api_key", lambda: "key"), \\
     mock.patch.object(ci, "fetch_timezone", lambda: None), \\
     mock.patch.object(ci, "_google_calendar_events", fake_events), \\
     mock.patch.object(ci, "_google_tasks", lambda ws, we: []), \\
     mock.patch.object(ci, "_attachment_menu", lambda n: {{"screenshots": [], "documents": [], "recording": None, "google_drive": []}}), \\
     mock.patch.object(ci, "run_adk_sync", lambda runner, user, session, message: "Booked it."), \\
     mock.patch.object(ci, "st_calendar", lambda events, **kwargs: st.session_state.__setitem__("shown_events", events)):
    ci.run_chat_interface()
'''


def test_chat_keeps_conversation_credentials_and_calendar_after_refresh(tmp_path):
    import builtins
    builtins.TEST_EVENT_FETCHES = 0
    app = tmp_path / "chat_app.py"
    app.write_text(CHAT_APP.format(app_root=str(APP_ROOT)))

    first = AppTest.from_file(str(app), default_timeout=60).run()
    first.chat_input[0].set_value("Book the dentist").run()
    assert not first.exception
    fetches_before_refresh = builtins.TEST_EVENT_FETCHES

    refreshed = AppTest.from_file(str(app), default_timeout=60).run()  # a new browser session
    assert not refreshed.exception
    assert [m.markdown[0].value for m in refreshed.chat_message] == ["Book the dentist", "Booked it."]
    assert refreshed.session_state["user_id"] == "alice"
    assert refreshed.session_state["shown_events"][0]["title"] == "Dentist for alice"
    # The calendar came from the per-user cache, not a new round of Google calls.
    assert builtins.TEST_EVENT_FETCHES == fetches_before_refresh


def test_restore_signed_in_user_reads_user_id_cookie_after_refresh():
    from google_oauth import oauth_login

    cookies = mock.Mock()
    cookies.get_all.return_value = {"user_id": "alice"}
    with mock.patch.object(oauth_login, "st") as st_mock, \
         mock.patch.object(oauth_login, "get_cookie_manager", return_value=cookies):
        st_mock.session_state = {}
        assert oauth_login.restore_signed_in_user() == "alice"
        assert st_mock.session_state["user_id"] == "alice"


def test_sign_in_shows_chat_in_the_same_run_without_rerun(tmp_path):
    """st.rerun() right after sign-in cuts the run short before the browser
    saves the user_id cookie, so the next refresh forgets the user."""
    calls = []

    def login_that_succeeds():
        calls.append("login")
        login_module.st.session_state.logged_in = True
        login_module.st.query_params.logged_in = "True"

    with mock.patch("streamlit.rerun", side_effect=AssertionError("st.rerun() after sign-in")):
        at = _run_login_page(tmp_path, login_that_succeeds, lambda: calls.append("chat"))
    assert not at.exception
    assert calls == ["login", "chat"]


LOGIN_APP = """
import sys
sys.path.insert(0, {app_root!r})
from unittest import mock
import streamlit as st
import ui.login as login_module

class FakeAuth:
    def __init__(self):
        st.session_state.setdefault("exchanges", 0)
    def login(self):
        st.markdown("GOOGLE-BUTTON")
        st.stop()
    def get_creds(self):
        if "code" not in st.query_params:
            return None
        st.session_state.exchanges += 1
        return {creds}

with mock.patch.object(login_module, "OAuthLogin", FakeAuth):
    login_module.login()
"""


def _run_login(tmp_path, creds, query_params):
    app = tmp_path / "login_app.py"
    app.write_text(LOGIN_APP.format(app_root=str(APP_ROOT), creds=creds))
    at = AppTest.from_file(str(app), default_timeout=60)
    for key, value in query_params.items():
        at.query_params[key] = value
    return at.run()


def _shown(at) -> str:
    return "\n".join(m.value for m in at.markdown)


def test_returning_from_google_skips_the_sign_in_card(tmp_path):
    at = _run_login(tmp_path, "object()", {"code": "good", "state": "s"})
    assert not at.exception
    assert at.session_state["logged_in"] is True
    assert at.query_params["logged_in"] == ["True"]
    assert "Welcome back" not in _shown(at)
    assert "GOOGLE-BUTTON" not in _shown(at)


def test_failed_google_code_shows_the_sign_in_card_again(tmp_path):
    at = _run_login(tmp_path, "None", {"code": "expired", "state": "s", "scope": "x"})
    assert not at.exception
    assert at.session_state["exchanges"] == 1
    assert "code" not in at.query_params and "state" not in at.query_params
    assert "Welcome back" in _shown(at)
    assert "GOOGLE-BUTTON" in _shown(at)


GET_CREDS_APP = """
import sys
sys.path.insert(0, {app_root!r})
from unittest import mock
import streamlit as st
import google_oauth.oauth_login as ol

class FakeCookies:
    def get_all(self, key=None):
        return {cookies!r}
    def set(self, *a, **k): pass
    def delete(self, *a, **k): pass

st.session_state.setdefault("exchanged", [])
with mock.patch.object(ol, "get_cookie_manager", lambda: FakeCookies()), \\
     mock.patch.object(ol.OAuthLogin, "_exchange_code", lambda self, c: st.session_state.exchanged.append(dict(c))):
    ol.OAuthLogin().get_creds()
"""


def _run_get_creds(tmp_path, cookies):
    app = tmp_path / "get_creds_app.py"
    app.write_text(GET_CREDS_APP.format(app_root=str(APP_ROOT), cookies=cookies))
    at = AppTest.from_file(str(app), default_timeout=60)
    at.query_params["code"] = "c"
    at.query_params["state"] = "s"
    return at


def test_get_creds_waits_for_cookies_after_google_redirect(tmp_path):
    """Google's redirect starts a new session whose cookies (with oauth_state)
    arrive a rerun later: the code must not be judged until then."""
    at = _run_get_creds(tmp_path, {}).run()
    assert not at.exception
    assert at.session_state["exchanged"] == []
    assert any("Finishing sign-in" in i.value for i in at.info)


def test_get_creds_exchanges_code_once_cookies_arrive(tmp_path):
    at = _run_get_creds(tmp_path, {"oauth_state": "s", "oauth_code_verifier": "v"}).run()
    assert not at.exception
    assert at.session_state["exchanged"] == [{"oauth_state": "s", "oauth_code_verifier": "v"}]


def test_get_creds_stops_waiting_after_a_few_runs(tmp_path):
    at = _run_get_creds(tmp_path, {})
    for _ in range(4):
        at.run()
    assert at.session_state["exchanged"] == [{}]  # tried once, then gave up waiting
