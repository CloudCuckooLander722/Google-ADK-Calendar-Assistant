"""
Autograder for authentication_login/creds_db.py (REQUIREMENTS.md 2.2 and 9.2).

creds_db reads back the credentials oauth_login saved: one Firestore document
per user in "users", keyed by the Google user ID, with access_token,
refresh_token and client_secret encrypted with GOOGLE_CREDS_ENCRYPTION_KEY
(Fernet). It finds the user from ?user_id=... in st.query_params, or else from
the user_id cookie, and turns the document into a google-auth Credentials.

Pages that read st.query_params run as real Streamlit scripts with AppTest.
Only the outside world is faked: Firestore and the browser's cookie jar behind
CookieController.

A name the module uses but never defines (e.g. InvalidToken) fails
test_module_defines_every_name_it_uses and is filled in with a stand-in for the
other tests, so one missing name doesn't hide every other result. A syntax
error still stops everything: Python can't load the file at all.

    cd my-adk-agent && ../.venv/bin/python -m pytest authentication_login_autograders/creds_db_test.py -v

Set CREDS_DB_MODULE to grade a different module (default
authentication_login.creds_db).
"""
import ast
import builtins
import contextlib
import datetime
import importlib
import importlib.util
import logging
import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest
from cryptography.fernet import Fernet, InvalidToken
from streamlit.testing.v1 import AppTest

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
MODULE_UNDER_TEST = os.environ.get("CREDS_DB_MODULE", "authentication_login.creds_db")

# --- Simulated data -----------------------------------------------------------

ENCRYPTION_KEY = Fernet.generate_key().decode()
FERNET = Fernet(ENCRYPTION_KEY.encode())

PROJECT_ID = "workflow-test"
GOOGLE_USER_ID = "109876543210987654321"
OTHER_USER_ID = "100000000000000000001"
GOOGLE_EMAIL = "test.user@example.com"
CLIENT_ID = "123456789012-a1b2c3d4e5f6g7h8i9j0.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-simulated-client-secret"
ACCESS_TOKEN = "ya29.a0AfB_simulated-access-token"
REFRESH_TOKEN = "1//0gSimulated-refresh-token"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]
# Firestore hands timestamps back as timezone-aware UTC datetimes.
EXPIRY = datetime.datetime(2026, 10, 25, 18, 30, tzinfo=datetime.timezone.utc)


def _stored_doc(user_id=GOOGLE_USER_ID, access_token=ACCESS_TOKEN, **overrides):
    """A users/{user_id} document as oauth_login saves it."""
    doc = {
        "user_id": user_id,
        "email": GOOGLE_EMAIL,
        "access_token": FERNET.encrypt(access_token.encode()).decode(),
        "refresh_token": FERNET.encrypt(REFRESH_TOKEN.encode()).decode(),
        "token_uri": TOKEN_URI,
        "client_id": CLIENT_ID,
        "client_secret": FERNET.encrypt(CLIENT_SECRET.encode()).decode(),
        "scopes": list(SCOPES),
        "expiry": EXPIRY,
    }
    doc.update(overrides)
    return doc


# --- Fakes for the outside world ----------------------------------------------

class CookieController:
    """streamlit_cookies_controller.CookieController (same methods) over a dict."""

    def __init__(self, jar: dict):
        self.jar = jar

    def get(self, name):
        return self.jar.get(name)

    def getAll(self):
        return dict(self.jar)

    def set(self, name, value, path="/", expires=None, max_age=None, domain=None,
            secure=None, same_site="strict", partitioned=None):
        self.jar[name] = value

    def remove(self, name, path="/", domain=None):
        self.jar.pop(name, None)

    def refresh(self):
        pass


class FakeFirestore:
    """firestore.Client: collection(name).document(id).set/get, in memory."""

    def __init__(self):
        self.docs: dict[tuple[str, str], dict] = {}
        self.reads: list[tuple[str, str]] = []

    def collection(self, name):
        db = self

        class _Collection:
            def document(self, doc_id):
                class _Doc:
                    id = doc_id

                    def set(self, data, merge=False):
                        key = (name, doc_id)
                        db.docs[key] = {**db.docs.get(key, {}), **data} if merge else dict(data)

                    def get(self):
                        db.reads.append((name, doc_id))
                        data = db.docs.get((name, doc_id))
                        return mock.Mock(exists=data is not None, to_dict=lambda: dict(data) if data else None,
                                         id=doc_id)
                return _Doc()
        return _Collection()


# --- Loading the module under test --------------------------------------------

def _module_path() -> Path:
    spec = importlib.util.find_spec(MODULE_UNDER_TEST)
    if spec is None or not spec.origin:
        pytest.fail(f"{MODULE_UNDER_TEST} not found")
    return Path(spec.origin)


def _undefined_names(path: Path) -> list[str]:
    """Names the module reads but never defines, imports or gets from builtins."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__package__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            defined.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
    return sorted({
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in defined
    })


# `value` stands in for the parameter a function checks for None but named
# something else: any non-None object lets the function go on to its real work.
STAND_INS = {
    "InvalidToken": InvalidToken,
    "logging": logging,
    "logger": logging.getLogger("creds_db"),
    "value": object(),
}


@pytest.fixture
def firestore_db():
    return FakeFirestore()


@pytest.fixture
def cookie_jar():
    return {}


def _fake_modules(monkeypatch, cookies):
    cookie_module = types.ModuleType("cookie_service")
    cookie_module.controller = cookies
    for name in ("cookie_service", "services.cookie_service"):
        monkeypatch.setitem(sys.modules, name, cookie_module)
    monkeypatch.delitem(sys.modules, MODULE_UNDER_TEST, raising=False)


@pytest.fixture
def creds_db(monkeypatch, firestore_db, cookie_jar):
    """The module under test, imported with the outside world faked."""
    monkeypatch.setenv("GOOGLE_CREDS_ENCRYPTION_KEY", ENCRYPTION_KEY)
    monkeypatch.setenv("PROJECT_ID", PROJECT_ID)
    cookies = CookieController(cookie_jar)
    _fake_modules(monkeypatch, cookies)

    for name in _undefined_names(_module_path()):
        if name in STAND_INS:
            monkeypatch.setattr(builtins, name, STAND_INS[name], raising=False)

    try:
        module = importlib.import_module(MODULE_UNDER_TEST)
    except SyntaxError as error:
        pytest.fail(f"{MODULE_UNDER_TEST} has a syntax error at line {error.lineno}, so no test can run: {error.msg}")
    except Exception as error:
        pytest.fail(f"{MODULE_UNDER_TEST} does not import: {type(error).__name__}: {error}")

    for attr in ("cookie_controller", "controller"):
        if attr in vars(module):
            monkeypatch.setattr(module, attr, cookies)
    real_get_db = getattr(module, "get_db", None)
    monkeypatch.setattr(module, "get_db", lambda: firestore_db, raising=False)
    module._real_get_db = real_get_db
    return module


def test_module_defines_every_name_it_uses():
    path = _module_path()
    try:
        ast.parse(path.read_text())
    except SyntaxError as error:
        pytest.fail(f"syntax error at line {error.lineno}: {error.msg}")
    missing = _undefined_names(path)
    assert not missing, (
        f"{path.name} uses names it never defines or imports: {', '.join(missing)}. "
        "The other tests filled them in with stand-ins; the real app would raise NameError."
    )


# --- Import time --------------------------------------------------------------

def test_import_fails_loudly_without_an_encryption_key(monkeypatch):
    """REQUIREMENTS.md 2.2: no key must stop the import, never fall back to plain text."""
    monkeypatch.delenv("GOOGLE_CREDS_ENCRYPTION_KEY", raising=False)
    _fake_modules(monkeypatch, CookieController({}))
    with pytest.raises(RuntimeError):
        importlib.import_module(MODULE_UNDER_TEST)


def test_get_db_uses_the_project_id(creds_db):
    get_db = creds_db._real_get_db
    assert get_db is not None, "creds_db must define get_db()"
    with contextlib.suppress(AttributeError):
        get_db.clear()  # st.cache_resource would hand back a client from an earlier test
    with mock.patch("google.cloud.firestore.Client") as client:
        db = get_db()
    client.assert_called_once()
    assert client.call_args.kwargs.get("project") == PROJECT_ID
    assert db is client.return_value


# --- _encrypt / _decrypt ------------------------------------------------------

def test_encrypt_hides_the_value(creds_db):
    encrypted = creds_db._encrypt(REFRESH_TOKEN)
    assert isinstance(encrypted, str), "encrypted values must be str so Firestore can store them"
    assert REFRESH_TOKEN not in encrypted


def test_encrypt_uses_the_encryption_key(creds_db):
    encrypted = creds_db._encrypt(REFRESH_TOKEN)
    assert FERNET.decrypt(encrypted.encode()).decode() == REFRESH_TOKEN, \
        "values must be Fernet-encrypted with GOOGLE_CREDS_ENCRYPTION_KEY"


def test_decrypt_reads_a_stored_value(creds_db):
    stored = FERNET.encrypt(ACCESS_TOKEN.encode()).decode()
    assert creds_db._decrypt(stored) == ACCESS_TOKEN


def test_decrypt_undoes_encrypt(creds_db):
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET):
        assert creds_db._decrypt(creds_db._encrypt(secret)) == secret


def test_encrypt_and_decrypt_pass_none_through(creds_db):
    """A document may have no refresh token; that must stay None, not crash."""
    assert creds_db._encrypt(None) is None
    assert creds_db._decrypt(None) is None


def test_decrypt_returns_none_for_unreadable_data(creds_db):
    """Corrupted data or a rotated key: None, not a crash in every caller."""
    assert creds_db._decrypt("not-a-fernet-token") is None
    other_key = Fernet(Fernet.generate_key()).encrypt(ACCESS_TOKEN.encode()).decode()
    assert creds_db._decrypt(other_key) is None


# --- Pages of the app -----------------------------------------------------------

PAGE_SCRIPT = """
import builtins
builtins.CREDS_DB_AUTOGRADER_PAGE()
"""


def _run_page(tmp_path, body, query_params=None):
    """Runs `body()` as a Streamlit page; returns (result, error)."""
    outcome = {}

    def page():
        try:
            outcome["result"] = body()
        except Exception as error:
            outcome["error"] = error

    script = tmp_path / "page.py"
    script.write_text(PAGE_SCRIPT)
    builtins.CREDS_DB_AUTOGRADER_PAGE = page
    try:
        at = AppTest.from_file(str(script), default_timeout=30)
        for key, value in (query_params or {}).items():
            at.query_params[key] = value
        at.run()
    finally:
        del builtins.CREDS_DB_AUTOGRADER_PAGE
    return outcome.get("result"), outcome.get("error")


def _describe(error) -> str:
    return f"{type(error).__name__}: {error}"


def _get_creds_dict(creds_db, tmp_path, query_params=None):
    function = getattr(creds_db, "_get_creds_dict", None) or getattr(creds_db, "get_creds_dict", None)
    assert function is not None, "creds_db must define get_creds_dict() (or _get_creds_dict())"
    return _run_page(tmp_path, function, query_params)


def _signed_in_dict(creds_db, tmp_path, firestore_db, **page):
    result, error = _get_creds_dict(creds_db, tmp_path, **page)
    if error is not None:
        pytest.fail(f"get_creds_dict() raised {_describe(error)}")
    assert isinstance(result, dict), f"get_creds_dict() should return a dict, got {result!r}"
    return result


@pytest.fixture
def stored(firestore_db):
    """Two signed-up users, so reading the wrong one shows."""
    firestore_db.docs[("users", GOOGLE_USER_ID)] = _stored_doc()
    firestore_db.docs[("users", OTHER_USER_ID)] = _stored_doc(OTHER_USER_ID, access_token="ya29.other-users-token")
    return firestore_db


# --- get_creds_dict -----------------------------------------------------------

def test_get_creds_dict_finds_the_user_from_query_params(creds_db, tmp_path, stored):
    creds = _signed_in_dict(creds_db, tmp_path, stored, query_params={"user_id": GOOGLE_USER_ID})
    assert stored.reads == [("users", GOOGLE_USER_ID)], \
        f"should read users/{GOOGLE_USER_ID} once, read {stored.reads}"
    assert creds["user_id"] == GOOGLE_USER_ID
    assert creds["email"] == GOOGLE_EMAIL


def test_get_creds_dict_falls_back_to_the_user_id_cookie(creds_db, tmp_path, stored, cookie_jar):
    """After a refresh the URL may have no ?user_id=, but the cookie still does."""
    cookie_jar["user_id"] = GOOGLE_USER_ID
    creds = _signed_in_dict(creds_db, tmp_path, stored)
    assert creds["user_id"] == GOOGLE_USER_ID
    assert creds["access_token"] == ACCESS_TOKEN


def test_get_creds_dict_prefers_query_params_over_the_cookie(creds_db, tmp_path, stored, cookie_jar):
    cookie_jar["user_id"] = OTHER_USER_ID
    creds = _signed_in_dict(creds_db, tmp_path, stored, query_params={"user_id": GOOGLE_USER_ID})
    assert creds["user_id"] == GOOGLE_USER_ID


def test_get_creds_dict_decrypts_the_tokens(creds_db, tmp_path, stored):
    creds = _signed_in_dict(creds_db, tmp_path, stored, query_params={"user_id": GOOGLE_USER_ID})
    assert creds["access_token"] == ACCESS_TOKEN
    assert creds["refresh_token"] == REFRESH_TOKEN
    assert creds["client_secret"] == CLIENT_SECRET


def test_get_creds_dict_returns_what_is_needed_to_refresh(creds_db, tmp_path, stored):
    creds = _signed_in_dict(creds_db, tmp_path, stored, query_params={"user_id": GOOGLE_USER_ID})
    assert creds["client_id"] == CLIENT_ID
    assert creds["token_uri"] == TOKEN_URI
    assert creds["scopes"] == SCOPES
    assert creds["expiry"] == EXPIRY


def test_get_creds_dict_handles_a_missing_refresh_token(creds_db, tmp_path, firestore_db):
    firestore_db.docs[("users", GOOGLE_USER_ID)] = _stored_doc(refresh_token=None)
    creds = _signed_in_dict(creds_db, tmp_path, firestore_db, query_params={"user_id": GOOGLE_USER_ID})
    assert creds["refresh_token"] is None
    assert creds["access_token"] == ACCESS_TOKEN


def test_get_creds_dict_without_a_user_id_raises_value_error(creds_db, tmp_path, stored):
    """No ?user_id= and no cookie: nobody is signed in."""
    _, error = _get_creds_dict(creds_db, tmp_path)
    assert isinstance(error, ValueError), \
        f"should raise ValueError when there is no user_id, got {error and _describe(error)}"
    assert stored.reads == []


def test_get_creds_dict_for_a_user_with_no_document(creds_db, tmp_path, firestore_db):
    """A user_id with nothing saved must not come back as credentials full of None."""
    result, error = _get_creds_dict(creds_db, tmp_path, query_params={"user_id": GOOGLE_USER_ID})
    assert not isinstance(error, (AttributeError, TypeError, KeyError, NameError)), \
        f"crashed instead of reporting the missing document: {_describe(error)}"
    assert result is None, f"should return None or raise, returned {result!r}"


# --- Credentials --------------------------------------------------------------

def _creds_dict(**overrides):
    creds = {
        "user_id": GOOGLE_USER_ID,
        "email": GOOGLE_EMAIL,
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "token_uri": TOKEN_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scopes": list(SCOPES),
        "expiry": EXPIRY,
    }
    creds.update(overrides)
    return creds


def test_create_creds_object_builds_google_credentials(creds_db):
    from google.oauth2.credentials import Credentials
    creds = creds_db._create_creds_object(_creds_dict())
    assert isinstance(creds, Credentials)
    assert creds.token == ACCESS_TOKEN
    assert creds.refresh_token == REFRESH_TOKEN
    assert creds.token_uri == TOKEN_URI
    assert creds.client_id == CLIENT_ID
    assert creds.client_secret == CLIENT_SECRET


def test_create_creds_object_keeps_the_scopes(creds_db):
    creds = creds_db._create_creds_object(_creds_dict())
    assert list(creds.scopes or []) == SCOPES


def test_create_creds_object_reattaches_the_expiry(creds_db):
    """REQUIREMENTS.md 2.3: without expiry, .expired is always False and the
    access token is never refreshed."""
    past = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    creds = creds_db._create_creds_object(_creds_dict(expiry=past))
    assert creds.expiry is not None, "expiry must be set on the Credentials"
    assert creds.expiry.replace(tzinfo=None) == past.replace(tzinfo=None)
    assert creds.expired, "a credential whose expiry has passed must report expired"


def test_get_valid_credentials_returns_the_users_credentials(creds_db, tmp_path, stored):
    result, error = _run_page(tmp_path, creds_db.get_valid_credentials, {"user_id": GOOGLE_USER_ID})
    if error is not None:
        pytest.fail(f"get_valid_credentials() raised {_describe(error)}")
    assert result is not None, "get_valid_credentials() should return the credentials"
    assert result.token == ACCESS_TOKEN
    assert result.refresh_token == REFRESH_TOKEN
    assert result.client_id == CLIENT_ID


# --- upsert_credentials -------------------------------------------------------

def _google_credentials(**overrides):
    from google.oauth2.credentials import Credentials
    fields = dict(token=ACCESS_TOKEN, refresh_token=REFRESH_TOKEN, token_uri=TOKEN_URI,
                  client_id=CLIENT_ID, client_secret=CLIENT_SECRET, scopes=list(SCOPES))
    fields.update(overrides)
    creds = Credentials(**fields)
    creds.expiry = EXPIRY.replace(tzinfo=None)  # google-auth keeps expiry as naive UTC
    return creds


def _upsert(creds_db, *args, **kwargs):
    function = getattr(creds_db, "upsert_credentials", None)
    assert function is not None, "creds_db must define upsert_credentials(user_id, creds, email)"
    function(*args, **kwargs)


def test_upsert_credentials_saves_one_document_keyed_by_user_id(creds_db, firestore_db):
    _upsert(creds_db, GOOGLE_USER_ID, _google_credentials(), email=GOOGLE_EMAIL)
    _upsert(creds_db, GOOGLE_USER_ID, _google_credentials(), email=GOOGLE_EMAIL)
    assert list(firestore_db.docs) == [("users", GOOGLE_USER_ID)], \
        f"signing in twice must update users/{GOOGLE_USER_ID}, not add documents: {list(firestore_db.docs)}"
    assert firestore_db.docs[("users", GOOGLE_USER_ID)]["email"] == GOOGLE_EMAIL


def test_upsert_credentials_encrypts_the_tokens(creds_db, firestore_db):
    _upsert(creds_db, GOOGLE_USER_ID, _google_credentials(), email=GOOGLE_EMAIL)
    doc = firestore_db.docs[("users", GOOGLE_USER_ID)]
    stored = repr(doc)
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET):
        assert secret not in stored, f"{secret[:12]}... saved in plain text"
    assert FERNET.decrypt(doc["refresh_token"].encode()).decode() == REFRESH_TOKEN


def test_upsert_credentials_keeps_the_refresh_token_when_none_is_given(creds_db, firestore_db):
    """REQUIREMENTS.md 2.2: an access-token refresh has no refresh token, and
    must not erase the stored one."""
    _upsert(creds_db, GOOGLE_USER_ID, _google_credentials(), email=GOOGLE_EMAIL)
    _upsert(creds_db, GOOGLE_USER_ID, _google_credentials(token="ya29.refreshed", refresh_token=None))
    doc = firestore_db.docs[("users", GOOGLE_USER_ID)]
    assert doc.get("refresh_token"), "the stored refresh token was erased"
    assert FERNET.decrypt(doc["refresh_token"].encode()).decode() == REFRESH_TOKEN
    assert FERNET.decrypt(doc["access_token"].encode()).decode() == "ya29.refreshed"
    assert doc.get("email") == GOOGLE_EMAIL, "a write without email must keep the stored email"


def test_upsert_then_get_valid_credentials_round_trips(creds_db, tmp_path, firestore_db):
    _upsert(creds_db, GOOGLE_USER_ID, _google_credentials(), email=GOOGLE_EMAIL)
    result, error = _run_page(tmp_path, creds_db.get_valid_credentials, {"user_id": GOOGLE_USER_ID})
    if error is not None:
        pytest.fail(f"get_valid_credentials() raised {_describe(error)}")
    assert result.token == ACCESS_TOKEN
    assert result.refresh_token == REFRESH_TOKEN
    assert result.client_secret == CLIENT_SECRET
    assert list(result.scopes or []) == SCOPES
    assert result.expiry == EXPIRY.replace(tzinfo=None)
