"""REQUIREMENTS.md 8.11: fetch_creds.

- fetch_creds checks for a user_id in st.session_state, then LocalStorage.
- It converts the user_id to a string and passes it to get_valid_credentials.
- Exceptions are handled gracefully, with a warning and an error on screen.
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent / "my-adk-agent"
sys.path.insert(0, str(APP_ROOT))

import google_oauth.oauth_login as ol  # noqa: E402


class _State(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None

    def __setattr__(self, key, value):
        self[key] = value


class _LocalStorage:
    """The streamlit-local-storage API: getItem / setItem."""
    instances = 0

    def __init__(self, items=None, raises=False):
        self.items = dict(items or {})
        self.raises = raises

    def __call__(self, *args, **kwargs):
        _LocalStorage.instances += 1
        return self

    def getItem(self, key):
        return self.getAll().get(key)

    def getAll(self):
        if self.raises:
            raise RuntimeError("local storage component failed")
        return self.items

    def setItem(self, key=None, value=None, *args, **kwargs):
        self.items[key] = value


@pytest.fixture
def st():
    fake = mock.MagicMock()
    fake.session_state = _State()
    fake.query_params = _State()
    with mock.patch.object(ol, "st", fake), \
         mock.patch.object(ol, "get_cookie_manager", lambda: mock.Mock(get_all=lambda key=None: {})):
        yield fake


def _fetch(storage, creds="CREDS"):
    with mock.patch.object(ol, "LocalStorage", storage, create=True), \
         mock.patch.object(ol, "get_valid_credentials", return_value=creds) as get:
        return ol.fetch_creds(), get


def test_uses_user_id_from_session_state(st):
    st.session_state["user_id"] = "alice"
    result, get = _fetch(_LocalStorage({"user_id": "mallory"}))
    assert result == "CREDS"
    get.assert_called_once_with("alice")


def test_falls_back_to_local_storage(st):
    result, get = _fetch(_LocalStorage({"user_id": "alice"}))
    assert result == "CREDS"
    get.assert_called_once_with("alice")
    assert st.session_state["user_id"] == "alice"


def test_converts_user_id_to_string(st):
    st.session_state["user_id"] = 12345
    _, get = _fetch(_LocalStorage())
    get.assert_called_once_with("12345")
    assert isinstance(get.call_args.args[0], str)


@pytest.mark.parametrize("stored", [None, ""])
def test_no_user_anywhere_returns_none_without_lookup(st, stored):
    result, get = _fetch(_LocalStorage({"user_id": stored}))
    assert result is None
    # Never look up (or save) the literal string "None" as if it were a user.
    get.assert_not_called()
    assert st.session_state.get("user_id") in (None, "")


def test_local_storage_error_is_handled_gracefully(st):
    with mock.patch.object(ol.logging, "warning") as warning, \
         mock.patch.object(ol.logging, "exception") as exception:
        result, _ = _fetch(_LocalStorage(raises=True))
    assert result is None
    assert warning.called or exception.called
    st.error.assert_called()


def test_credential_lookup_error_is_handled_gracefully(st):
    st.session_state["user_id"] = "alice"
    with mock.patch.object(ol, "LocalStorage", _LocalStorage(), create=True), \
         mock.patch.object(ol, "get_valid_credentials", side_effect=RuntimeError("firestore down")), \
         mock.patch.object(ol.logging, "warning") as warning, \
         mock.patch.object(ol.logging, "exception") as exception:
        assert ol.fetch_creds() is None
    assert warning.called or exception.called
    st.error.assert_called()


def test_can_be_called_twice_in_one_run(st):
    """The calendar panel fetches events and then tasks in the same run, so
    fetch_creds must not create a second LocalStorage widget with the same key."""
    st.session_state["user_id"] = "alice"
    storage = _LocalStorage()
    _LocalStorage.instances = 0
    _fetch(storage)
    _fetch(storage)
    assert _LocalStorage.instances <= 1


def test_does_not_leave_user_id_in_the_url(st):
    st.session_state["user_id"] = "alice"
    _fetch(_LocalStorage())
    assert "user_id" not in st.query_params
