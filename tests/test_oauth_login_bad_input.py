"""oauth_login.py with bad or missing input: tampered or incomplete Google
callbacks, failing Google and Firestore calls, missing or junk cookies, and
stored credentials that are gone, corrupted or revoked. Every one of these must
end as "not signed in" (None), never as a crash or a half-finished sign-in."""
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent / "my-adk-agent"
sys.path.insert(0, str(APP_ROOT))

from google.auth.exceptions import RefreshError  # noqa: E402

import google_oauth.oauth_login as ol  # noqa: E402
import google_oauth.credentials_store as cs  # noqa: E402
import google_oauth.creds_db as creds_db  # noqa: E402


class _State(dict):
    """Stands in for st.session_state / st.query_params: a dict that also
    allows attribute access, like the real ones."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None

    def __setattr__(self, key, value):
        self[key] = value


class _Stopped(Exception):
    """Raised by the fake st.stop()."""


class _Cookies:
    def __init__(self, cookies=None, raises=False):
        self.cookies = cookies if cookies is not None else {}
        self.raises = raises
        self.set_calls = []
        self.deleted = []

    def get_all(self, key=None):
        if self.raises:
            raise RuntimeError("cookie component failed")
        return self.cookies

    def set(self, cookie, val, key=None, **options):
        self.set_calls.append((cookie, val))
        self.options = options

    def delete(self, cookie, key=None):
        self.deleted.append(cookie)


@pytest.fixture
def st():
    fake = mock.MagicMock()
    fake.session_state = _State()
    fake.query_params = _State()
    fake.stop.side_effect = _Stopped
    empty_local_storage = mock.Mock(return_value=mock.Mock(getAll=lambda: {}))
    with mock.patch.object(ol, "st", fake), mock.patch.object(ol, "LocalStorage", empty_local_storage):
        yield fake


def _auth(cookies):
    with mock.patch.object(ol, "get_cookie_manager", lambda: cookies):
        return ol.OAuthLogin()


# --- _exchange_code: the Google callback ------------------------------------

def _callback(st, code="good-code", state="s1", saved_state="s1"):
    if code is not None:
        st.query_params["code"] = code
    if state is not None:
        st.query_params["state"] = state
    if saved_state is not None:
        st.session_state["oauth_state"] = saved_state
        st.session_state["oauth_code_verifier"] = "verifier"


def _google(id_info=None, fetch_error=None, verify_error=None):
    """Patches Google's token exchange and ID-token check."""
    creds = mock.Mock(id_token="id-token", client_id="client")
    flow = mock.Mock(credentials=creds)
    if fetch_error:
        flow.fetch_token.side_effect = fetch_error
    verify = mock.Mock(return_value=id_info or {"sub": "12345", "email": "a@example.com"})
    if verify_error:
        verify.side_effect = verify_error
    return creds, flow, verify


def _exchange(st, cookies, flow, verify, upsert=None):
    with mock.patch.object(ol.Flow, "from_client_secrets_file", return_value=flow), \
         mock.patch.object(ol.google_id_token, "verify_oauth2_token", verify), \
         mock.patch.object(ol.creds_db, "upsert_credentials", upsert or mock.Mock()) as up:
        return _auth(cookies)._exchange_code(cookies.cookies), up


def test_exchange_code_signs_in_and_saves_credentials(st):
    _callback(st)
    cookies = _Cookies()
    creds, flow, verify = _google()
    result, upsert = _exchange(st, cookies, flow, verify)
    assert result is creds
    upsert.assert_called_once_with("12345", creds, email="a@example.com")
    assert ("user_id", "12345") in cookies.set_calls
    assert st.session_state["user_id"] == "12345"
    assert "code" not in st.query_params


def test_sign_in_cookie_lasts_longer_than_a_day(st):
    import datetime
    _callback(st)
    cookies = _Cookies()
    _, flow, verify = _google()
    _exchange(st, cookies, flow, verify)
    assert cookies.options["expires_at"] > datetime.datetime.now() + datetime.timedelta(days=7)


def test_exchange_code_marks_user_signed_in_before_rendering_anything(st):
    """A component's value arriving from the browser makes Streamlit abort the
    run at the next element it draws. Sign-in must already be recorded then."""
    from streamlit.runtime.scriptrunner_utils.exceptions import RerunException
    _callback(st)
    cookies = _Cookies()
    seen = {}

    def interrupted(cookie, val, key=None, **options):
        seen.update(user_id=st.session_state.get("user_id"), logged_in=st.session_state.get("logged_in"),
                    code_in_url="code" in st.query_params)
        raise RerunException(None)

    cookies.set = interrupted
    _, flow, verify = _google()
    with pytest.raises(RerunException):
        _exchange(st, cookies, flow, verify)
    assert seen == {"user_id": "12345", "logged_in": True, "code_in_url": False}


def test_rerun_after_interrupted_sign_in_does_not_reuse_the_code(st):
    """Google rejects a code it already accepted (invalid_grant): a rerun with
    the same ?code must finish signing in from the saved credentials."""
    _callback(st)
    st.session_state["user_id"] = "12345"
    st.session_state[ol.OAUTH_USED_CODE_KEY] = "good-code"
    cookies = _Cookies()
    _, flow, verify = _google()
    saved = object()
    with mock.patch.object(ol, "get_valid_credentials", return_value=saved) as get:
        result, upsert = _exchange(st, cookies, flow, verify)
    assert result is saved
    get.assert_called_once_with("12345")
    flow.fetch_token.assert_not_called()
    upsert.assert_not_called()
    assert ("user_id", "12345") in cookies.set_calls
    assert st.session_state["logged_in"] is True
    assert "code" not in st.query_params


def test_a_new_code_is_still_exchanged_after_an_earlier_sign_in(st):
    _callback(st, code="new-code")
    st.session_state["user_id"] = "12345"
    st.session_state[ol.OAUTH_USED_CODE_KEY] = "old-code"
    cookies = _Cookies()
    creds, flow, verify = _google()
    result, _ = _exchange(st, cookies, flow, verify)
    assert result is creds
    flow.fetch_token.assert_called_once()


@pytest.mark.parametrize("state", [None, ""])
def test_exchange_code_rejects_callback_without_state(st, state):
    _callback(st, state=state)
    cookies = _Cookies()
    _, flow, verify = _google()
    result, upsert = _exchange(st, cookies, flow, verify)
    assert result is None
    flow.fetch_token.assert_not_called()
    upsert.assert_not_called()


def test_exchange_code_rejects_tampered_state(st):
    _callback(st, state="attacker", saved_state="s1")
    cookies = _Cookies()
    _, flow, verify = _google()
    result, upsert = _exchange(st, cookies, flow, verify)
    assert result is None
    flow.fetch_token.assert_not_called()
    assert cookies.set_calls == []


def test_exchange_code_rejects_when_no_state_was_saved(st):
    _callback(st, saved_state=None)
    cookies = _Cookies()
    _, flow, verify = _google()
    result, _ = _exchange(st, cookies, flow, verify)
    assert result is None
    flow.fetch_token.assert_not_called()


def test_exchange_code_uses_state_from_cookie_when_session_is_new(st):
    _callback(st, saved_state=None)
    cookies = _Cookies({"oauth_state": "s1", "oauth_code_verifier": "v"})
    creds, flow, verify = _google()
    result, _ = _exchange(st, cookies, flow, verify)
    assert result is creds
    assert flow.code_verifier == "v"


def test_exchange_code_handles_used_or_expired_code(st):
    from oauthlib.oauth2.rfc6749.errors import InvalidGrantError
    _callback(st)
    cookies = _Cookies()
    _, flow, verify = _google(fetch_error=InvalidGrantError())
    result, upsert = _exchange(st, cookies, flow, verify)
    assert result is None
    upsert.assert_not_called()
    assert cookies.set_calls == []


def test_exchange_code_handles_invalid_id_token(st):
    _callback(st)
    cookies = _Cookies()
    _, flow, verify = _google(verify_error=ValueError("Token has wrong audience"))
    result, upsert = _exchange(st, cookies, flow, verify)
    assert result is None
    upsert.assert_not_called()
    assert "user_id" not in st.session_state


def test_exchange_code_handles_id_token_without_sub(st):
    _callback(st)
    cookies = _Cookies()
    _, flow, verify = _google(id_info={"email": "a@example.com"})
    result, upsert = _exchange(st, cookies, flow, verify)
    assert result is None
    upsert.assert_not_called()


def test_exchange_code_does_not_sign_in_when_firestore_is_unreachable(st):
    """No Application Default Credentials (or Firestore down): the user must
    not get a user_id cookie that points at credentials that were never saved."""
    from google.auth.exceptions import DefaultCredentialsError
    _callback(st)
    cookies = _Cookies()
    _, flow, verify = _google()
    result, _ = _exchange(st, cookies, flow, verify, upsert=mock.Mock(side_effect=DefaultCredentialsError("no ADC")))
    assert result is None
    assert ("user_id", "12345") not in cookies.set_calls


# --- get_creds ----------------------------------------------------------------

def test_get_creds_waits_while_cookies_load(st):
    with pytest.raises(_Stopped):
        _auth(_Cookies({})).get_creds()
    st.info.assert_called_once()


def test_get_creds_survives_cookie_component_error(st):
    st.query_params["code"] = "c"
    st.query_params["state"] = "s"
    st.session_state[ol.OAUTH_COOKIE_WAITS_KEY] = ol.MAX_OAUTH_COOKIE_WAITS
    auth = _auth(_Cookies(raises=True))
    with mock.patch.object(ol.OAuthLogin, "_exchange_code", return_value=None):
        assert auth.get_creds() is None
    st.error.assert_called_once()


def test_get_creds_returns_none_for_unknown_user_id_cookie(st):
    with mock.patch.object(ol, "get_valid_credentials", return_value=None) as get:
        assert _auth(_Cookies({"user_id": "nobody"})).get_creds() is None
    get.assert_called_once_with("nobody")
    st.error.assert_not_called()


def test_get_creds_falls_back_to_user_id_cookie_when_code_fails(st):
    st.query_params["code"] = "stale"
    st.query_params["state"] = "s"
    stored = object()
    with mock.patch.object(ol.OAuthLogin, "_exchange_code", return_value=None), \
         mock.patch.object(ol, "get_valid_credentials", return_value=stored):
        assert _auth(_Cookies({"oauth_state": "s", "user_id": "alice"})).get_creds() is stored


def test_get_creds_reports_failure_for_bad_code_and_no_saved_user(st):
    st.query_params["code"] = "bad"
    st.query_params["state"] = "s"
    with mock.patch.object(ol.OAuthLogin, "_exchange_code", return_value=None):
        assert _auth(_Cookies({"oauth_state": "s"})).get_creds() is None
    st.error.assert_called_once()


# --- init_google_auth ---------------------------------------------------------

@pytest.mark.parametrize("error", [FileNotFoundError("missing"), ValueError("not a client secrets file")])
def test_init_google_auth_reports_misconfigured_client_secrets(st, error):
    with mock.patch.object(ol.Flow, "from_client_secrets_file", side_effect=error):
        with pytest.raises(_Stopped):
            _auth(_Cookies()).init_google_auth()
    st.error.assert_called_once()


# --- restore_signed_in_user ---------------------------------------------------

@pytest.mark.parametrize("cookies", [{}, None, "junk", ["user_id"], {"user_id": ""}, {"user_id": None}, {"other": "x"}])
def test_restore_signed_in_user_ignores_missing_or_junk_cookies(st, cookies):
    with mock.patch.object(ol, "get_cookie_manager", lambda: _Cookies(cookies)):
        assert ol.restore_signed_in_user() is None
    assert "user_id" not in st.session_state


def test_restore_signed_in_user_survives_cookie_component_error(st):
    with mock.patch.object(ol, "get_cookie_manager", lambda: _Cookies(raises=True)):
        assert ol.restore_signed_in_user() is None


def test_restore_signed_in_user_prefers_session_over_cookie(st):
    st.session_state["user_id"] = "alice"
    with mock.patch.object(ol, "get_cookie_manager", lambda: _Cookies({"user_id": "mallory"})):
        assert ol.restore_signed_in_user() == "alice"


def test_restore_signed_in_user_turns_numeric_cookie_into_string(st):
    with mock.patch.object(ol, "get_cookie_manager", lambda: _Cookies({"user_id": 12345})):
        assert ol.restore_signed_in_user() == "12345"


# --- fetch_creds and the Google API clients the agent's tools use ------------

def test_fetch_creds_without_any_user_returns_none(st):
    with mock.patch.object(ol, "get_valid_credentials") as get:
        assert ol.fetch_creds() is None
    get.assert_not_called()


@pytest.mark.parametrize("cookies", [None, "junk", {}, {"user_id": ""}])
def test_fetch_creds_ignores_missing_or_junk_cached_cookies(st, cookies):
    st.session_state["cookie_manager"] = mock.Mock(cookies=cookies)
    with mock.patch.object(ol, "get_valid_credentials") as get:
        assert ol.fetch_creds() is None
    get.assert_not_called()
    st.error.assert_not_called()


def test_fetch_creds_never_renders_a_second_cookie_component(st):
    """get_all() renders a component with a fixed key: calling it again in the
    same run (events, then tasks) raises StreamlitDuplicateElementKey."""
    cookies = _Cookies({"user_id": "alice"})
    cookies.get_all = mock.Mock(side_effect=AssertionError("get_all() called from fetch_creds"))
    st.session_state["cookie_manager"] = cookies
    with mock.patch.object(ol, "get_valid_credentials", return_value=None) as get:
        ol.fetch_creds()
        st.session_state.pop("user_id")
        ol.fetch_creds()
    assert get.call_args_list == [mock.call("alice"), mock.call("alice")]


@pytest.mark.parametrize("items", [None, "junk", {}])
def test_fetch_creds_treats_unloaded_local_storage_as_empty(st, items):
    """Right after a refresh the local-storage component holds None, not a dict."""
    with mock.patch.object(ol, "LocalStorage", mock.Mock(return_value=mock.Mock(getAll=lambda: items))), \
         mock.patch.object(ol, "get_valid_credentials") as get:
        assert ol.fetch_creds() is None
    get.assert_not_called()
    st.error.assert_not_called()


def test_fetch_creds_prefers_local_storage_over_cookie(st):
    st.session_state["cookie_manager"] = mock.Mock(cookies={"user_id": "from-cookie"})
    storage = mock.Mock(return_value=mock.Mock(getAll=lambda: {"user_id": "from-local-storage"}))
    with mock.patch.object(ol, "LocalStorage", storage), \
         mock.patch.object(ol, "get_valid_credentials", return_value=None) as get:
        ol.fetch_creds()
    get.assert_called_once_with("from-local-storage")


def test_fetch_creds_reads_user_id_cookie_and_keeps_it_in_session(st):
    st.session_state["cookie_manager"] = mock.Mock(cookies={"user_id": "alice"})
    creds = object()
    with mock.patch.object(ol, "get_valid_credentials", return_value=creds) as get:
        assert ol.fetch_creds() is creds
    get.assert_called_once_with("alice")
    assert st.session_state["user_id"] == "alice"


def test_fetch_creds_drops_user_id_from_url_but_keeps_logged_in(st):
    st.query_params["user_id"] = "alice"
    st.query_params["logged_in"] = "True"
    with mock.patch.object(ol, "get_valid_credentials", return_value=None):
        assert ol.fetch_creds() is None
    assert "user_id" not in st.query_params
    assert st.query_params["logged_in"] == "True"


@pytest.mark.parametrize("getter", [ol.get_calendar_service, ol.get_tasks_service])
def test_google_services_are_none_when_signed_out(st, getter):
    with mock.patch.object(ol, "fetch_creds", return_value=None), mock.patch.object(ol, "build") as build:
        assert getter() is None
    build.assert_not_called()


@pytest.mark.parametrize("getter", [ol.get_calendar_service, ol.get_tasks_service])
def test_google_services_are_none_when_client_build_fails(st, getter):
    with mock.patch.object(ol, "fetch_creds", return_value=object()), \
         mock.patch.object(ol, "build", side_effect=RuntimeError("discovery failed")):
        assert getter() is None


@pytest.mark.parametrize("scopes", [None, [], ["https://www.googleapis.com/auth/calendar"]])
def test_drive_token_needs_drive_scope(st, scopes):
    with mock.patch.object(ol, "fetch_creds", return_value=mock.Mock(scopes=scopes, token="t")):
        assert ol.get_drive_access_token() is None


def test_drive_token_returned_with_drive_scope(st):
    with mock.patch.object(ol, "fetch_creds", return_value=mock.Mock(scopes=[ol.DRIVE_SCOPE], token="t")):
        assert ol.get_drive_access_token() == "t"


# --- get_picker_app_id --------------------------------------------------------

@pytest.mark.parametrize("contents", [None, "not json", json.dumps({}), json.dumps({"web": {}})])
def test_picker_app_id_is_none_for_bad_client_secrets(tmp_path, contents):
    path = tmp_path / "credentials.json"
    if contents is not None:
        path.write_text(contents)
    ol.get_picker_app_id.cache_clear()
    try:
        with mock.patch.object(ol, "credentials_path", str(path)):
            assert ol.get_picker_app_id() is None
    finally:
        ol.get_picker_app_id.cache_clear()


def test_picker_app_id_is_the_project_number(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"web": {"client_id": "947143767112-abc.apps.googleusercontent.com"}}))
    ol.get_picker_app_id.cache_clear()
    try:
        with mock.patch.object(ol, "credentials_path", str(path)):
            assert ol.get_picker_app_id() == "947143767112"
    finally:
        ol.get_picker_app_id.cache_clear()


# --- delete_my_data -----------------------------------------------------------

def _delete(st, stored, revoke=None, cookies=None):
    cookies = cookies or _Cookies()
    with mock.patch.object(ol.creds_db, "get_credentials_dict", return_value=stored), \
         mock.patch.object(ol.creds_db, "delete_credentials") as delete, \
         mock.patch.object(ol.requests, "post", revoke or mock.Mock()) as post, \
         mock.patch.object(ol, "get_cookie_manager", lambda: cookies):
        ol.delete_my_data("alice")
    return delete, post, cookies


def test_delete_my_data_without_stored_tokens_still_signs_out(st):
    st.session_state["user_id"] = "alice"
    delete, post, cookies = _delete(st, None)
    post.assert_not_called()
    delete.assert_called_once_with("alice")
    assert cookies.deleted == ["user_id", "logged_in"]
    assert "user_id" not in st.session_state
    assert st.session_state["logged_in"] is False
    assert st.query_params["logged_in"] == "False"


def test_delete_my_data_deletes_even_when_revoke_fails(st):
    delete, _, _ = _delete(st, {"refresh_token": "r"}, revoke=mock.Mock(side_effect=OSError("offline")))
    delete.assert_called_once_with("alice")


def test_delete_my_data_survives_cookie_delete_error(st):
    cookies = _Cookies()
    cookies.delete = mock.Mock(side_effect=RuntimeError("cookie gone"))
    delete, _, _ = _delete(st, None, cookies=cookies)
    delete.assert_called_once_with("alice")
    assert st.session_state["logged_in"] is False


def test_delete_my_data_raises_when_firestore_delete_fails(st):
    with mock.patch.object(ol.creds_db, "get_credentials_dict", return_value=None), \
         mock.patch.object(ol.creds_db, "delete_credentials", side_effect=RuntimeError("firestore down")):
        with pytest.raises(RuntimeError):
            ol.delete_my_data("alice")


# --- get_valid_credentials (what every user_id lookup goes through) ----------

def _stored(**overrides):
    data = {
        "token": "access", "refresh_token": "refresh", "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "client", "client_secret": "secret", "scopes": ["openid"], "expiry": None, "email": "a@example.com",
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize("user_id", [None, ""])
def test_valid_credentials_need_a_user_id(user_id):
    with mock.patch.object(cs.creds_db, "get_credentials_dict") as get:
        assert cs.get_valid_credentials(user_id) is None
    get.assert_not_called()


def test_valid_credentials_none_for_unknown_user():
    with mock.patch.object(cs.creds_db, "get_credentials_dict", return_value=None):
        assert cs.get_valid_credentials("nobody") is None


def test_valid_credentials_none_when_firestore_raises():
    with mock.patch.object(cs.creds_db, "get_credentials_dict", side_effect=RuntimeError("down")):
        assert cs.get_valid_credentials("alice") is None


def test_valid_credentials_none_for_corrupted_expiry():
    with mock.patch.object(cs.creds_db, "get_credentials_dict", return_value=_stored(expiry="not a date")):
        assert cs.get_valid_credentials("alice") is None


def test_valid_credentials_none_when_refresh_is_revoked():
    expired = _stored(expiry="2000-01-01T00:00:00")
    with mock.patch.object(cs.creds_db, "get_credentials_dict", return_value=expired), \
         mock.patch.object(cs.Credentials, "refresh", side_effect=RefreshError("invalid_grant")), \
         mock.patch.object(cs.creds_db, "upsert_credentials") as upsert:
        assert cs.get_valid_credentials("alice") is None
    upsert.assert_not_called()


def test_valid_credentials_refresh_expired_token_and_save_it():
    expired = _stored(expiry="2000-01-01T00:00:00")

    def refresh(self, request):
        self.token = "new-access"

    with mock.patch.object(cs.creds_db, "get_credentials_dict", return_value=expired), \
         mock.patch.object(cs.Credentials, "refresh", refresh), \
         mock.patch.object(cs.creds_db, "upsert_credentials") as upsert:
        creds = cs.get_valid_credentials("alice")
    assert creds.token == "new-access"
    upsert.assert_called_once()


def test_valid_credentials_still_returned_when_saving_refresh_fails():
    expired = _stored(expiry="2000-01-01T00:00:00")
    with mock.patch.object(cs.creds_db, "get_credentials_dict", return_value=expired), \
         mock.patch.object(cs.Credentials, "refresh", lambda self, request: None), \
         mock.patch.object(cs.creds_db, "upsert_credentials", side_effect=RuntimeError("down")):
        assert cs.get_valid_credentials("alice") is not None


# --- creds_db -----------------------------------------------------------------

@pytest.mark.parametrize("user_id, creds", [("", mock.Mock(token="t")), (None, mock.Mock(token="t")),
                                            ("alice", None), ("alice", mock.Mock(token=None))])
def test_upsert_credentials_rejects_bad_input(user_id, creds):
    with mock.patch.object(creds_db, "_get_client") as client:
        with pytest.raises(ValueError):
            creds_db.upsert_credentials(user_id, creds)
    client.assert_not_called()


@pytest.mark.parametrize("user_id", ["", None])
def test_delete_credentials_rejects_empty_user_id(user_id):
    with pytest.raises(ValueError):
        creds_db.delete_credentials(user_id)


def test_get_credentials_dict_none_for_empty_user_id():
    assert creds_db.get_credentials_dict("") is None


def test_get_credentials_dict_none_when_firestore_raises():
    with mock.patch.object(creds_db, "_get_client", side_effect=RuntimeError("no ADC")):
        assert creds_db.get_credentials_dict("alice") is None


def test_get_credentials_dict_tolerates_undecryptable_fields():
    snapshot = mock.Mock(exists=True)
    snapshot.to_dict.return_value = {"access_token": "garbage", "refresh_token": creds_db._encrypt("r")}
    client = mock.Mock()
    client.collection.return_value.document.return_value.get.return_value = snapshot
    with mock.patch.object(creds_db, "_get_client", return_value=client):
        data = creds_db.get_credentials_dict("alice")
    assert data["token"] is None
    assert data["refresh_token"] == "r"
