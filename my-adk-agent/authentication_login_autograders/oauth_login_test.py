"""
Autograder for authentication_login/oauth_login.py: OAuthLogin.login_link and
OAuthLogin.download_creds (REQUIREMENTS.md 9.1).

Walks through a real sign-in to WorkFlow. Each step is a real Streamlit page,
run with Streamlit's AppTest, so the real st.link_button, st.query_params,
st.session_state and read-only st.context.cookies are used:

  1. The sign-in page: OAuthLogin().login_link() renders "Login with Google",
     built from a real client-secrets file, and saves oauth_state and the PKCE
     code_verifier in the browser's cookies.
  2. The user signs in at Google, which redirects the browser back with
     ?code=...&state=... That is a NEW Streamlit session (empty session_state):
     only the browser's cookies carried over.
  3. That page calls OAuthLogin().download_creds(): the code and code_verifier
     go to Google's token endpoint, which answers like the real one (access
     token, refresh token, ID token); the ID token is checked; the credentials
     are saved to Firestore under the user's Google ID ("sub"); user_id goes
     into st.session_state and a cookie.

Only the outside world is faked: Google's token endpoint (at the HTTP layer,
so the real google-auth-oauthlib / requests-oauthlib code runs), Google's ID
token check, Firestore, and the browser's cookie jar behind CookieController.

A name the module uses but never defines (e.g. SCOPES) fails
test_module_defines_every_name_it_uses and is filled in with a stand-in for the
other tests, so one missing name doesn't hide every other result. A syntax
error still stops everything: Python can't load the file at all.

    cd my-adk-agent && ../.venv/bin/python -m pytest authentication_login_autograders -v

Set OAUTH_LOGIN_MODULE to grade a different module (default
authentication_login.oauth_login).
"""
import ast
import base64
import builtins
import contextlib
import hashlib
import importlib
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from streamlit.testing.v1 import AppTest

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
MODULE_UNDER_TEST = os.environ.get("OAUTH_LOGIN_MODULE", "authentication_login.oauth_login")

# --- Simulated data -----------------------------------------------------------

REDIRECT_URI = "https://workflow-test.example.run.app/"
CLIENT_ID = "123456789012-a1b2c3d4e5f6g7h8i9j0.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-simulated-client-secret"
CLIENT_SECRETS = {
    "web": {
        "client_id": CLIENT_ID,
        "project_id": "workflow-test",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": CLIENT_SECRET,
        "redirect_uris": [REDIRECT_URI],
    }
}

GOOGLE_USER_ID = "109876543210987654321"  # the ID token's "sub": stable per Google account
GOOGLE_EMAIL = "test.user@example.com"
AUTH_CODE = "4/0AVMBsJh-simulated-authorization-code-from-google"
ACCESS_TOKEN = "ya29.a0AfB_simulated-access-token"
REFRESH_TOKEN = "1//0gSimulated-refresh-token"
ID_TOKEN = "eyJhbGciOiJSUzI1NiIsImtpZCI6InNpbXVsYXRlZCJ9.eyJzdWIiOiJzaW11bGF0ZWQifQ.c2lnbmF0dXJl"
GRANTED_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/drive.file",
]
ID_TOKEN_CLAIMS = {
    "iss": "https://accounts.google.com",
    "aud": CLIENT_ID,
    "sub": GOOGLE_USER_ID,
    "email": GOOGLE_EMAIL,
    "email_verified": True,
}


def _token_response(**overrides):
    body = {
        "access_token": ACCESS_TOKEN,
        "expires_in": 3599,
        "refresh_token": REFRESH_TOKEN,
        "scope": " ".join(GRANTED_SCOPES),
        "token_type": "Bearer",
        "id_token": ID_TOKEN,
    }
    body.update(overrides)
    return body


# --- Fakes for the outside world ----------------------------------------------

class CookieController:
    """streamlit_cookies_controller.CookieController (same methods), backed by
    one dict that plays the browser's cookie jar: it survives Google's redirect."""

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


class Browser:
    """One browser: a cookie jar that outlives Streamlit sessions."""

    def __init__(self):
        self.jar = {}
        self.cookies = CookieController(self.jar)


class FakeFirestore:
    """firestore.Client: collection(name).document(id).set/get, in memory."""

    def __init__(self):
        self.docs: dict[tuple[str, str], dict] = {}

    def collection(self, name):
        db = self

        class _Collection:
            def document(self, doc_id):
                class _Doc:
                    def set(self, data, merge=False):
                        key = (name, doc_id)
                        db.docs[key] = {**db.docs.get(key, {}), **data} if merge else dict(data)

                    def get(self):
                        data = db.docs.get((name, doc_id))
                        return mock.Mock(exists=data is not None, to_dict=lambda: dict(data or {}), id=doc_id)
                return _Doc()
        return _Collection()


class FakeGoogleTokenEndpoint:
    """Google's https://oauth2.googleapis.com/token, answering at the HTTP layer."""

    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else _token_response()
        self.requests = []

    def __call__(self, session, method, url, data=None, headers=None, **kwargs):
        form = parse_qs(data) if isinstance(data, str) else {k: [v] for k, v in (data or {}).items()}
        self.requests.append({"method": method, "url": url, "form": {k: v[0] for k, v in form.items()}})
        response = requests.Response()
        response.status_code = self.status
        response._content = json.dumps(self.body).encode()
        response.headers["Content-Type"] = "application/json"
        response.encoding = "utf-8"
        response.url = url
        response.request = requests.Request(method, url, data=data, headers=headers).prepare()
        return response


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


def _stand_ins(browser) -> dict:
    """What a missing name most likely meant, so the rest can still be graded."""
    return {
        "SCOPES": list(GRANTED_SCOPES),
        "_secret_or_env": lambda key, default=None: os.environ.get(key) or default,
        "get_cookie_manager": lambda: browser.cookies,
        "cookie_controller": browser.cookies,
        "controller": browser.cookies,
    }


@pytest.fixture
def client_secrets_file(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(CLIENT_SECRETS))
    return str(path)


@pytest.fixture
def firestore_db():
    return FakeFirestore()


@pytest.fixture
def browser():
    return Browser()


@pytest.fixture
def oauth(monkeypatch, client_secrets_file, firestore_db, browser):
    """The module under test, imported with the outside world faked."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("OAUTH_REDIRECT_URI", REDIRECT_URI)
    monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", client_secrets_file)
    if not os.environ.get("GOOGLE_CREDS_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet
        monkeypatch.setenv("GOOGLE_CREDS_ENCRYPTION_KEY", Fernet.generate_key().decode())

    cookie_module = types.ModuleType("cookie_service")
    cookie_module.controller = browser.cookies
    creds_db_module = types.ModuleType("authentication_login.creds_db")
    creds_db_module.get_db = lambda: firestore_db
    for name, module in {
        "cookie_service": cookie_module,
        "services.cookie_service": cookie_module,
        "authentication_login.creds_db": creds_db_module,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, MODULE_UNDER_TEST, raising=False)

    # Fill in names the module never defines (graded separately) through
    # builtins, where Python looks after the module's own globals.
    stand_ins = _stand_ins(browser)
    for name in _undefined_names(_module_path()):
        if name in stand_ins:
            monkeypatch.setattr(builtins, name, stand_ins[name], raising=False)

    try:
        module = importlib.import_module(MODULE_UNDER_TEST)
    except SyntaxError as error:
        pytest.fail(f"{MODULE_UNDER_TEST} has a syntax error at line {error.lineno}, so no test can run: {error.msg}")
    except Exception as error:  # NameError, ImportError...
        pytest.fail(f"{MODULE_UNDER_TEST} does not import: {type(error).__name__}: {error}")

    # Whatever the module named it, every cookie API it uses is this browser's.
    for attr in ("cookie_controller", "controller"):
        if attr in vars(module):
            monkeypatch.setattr(module, attr, browser.cookies)
    if "get_db" in vars(module):
        monkeypatch.setattr(module, "get_db", lambda: firestore_db)
    monkeypatch.setattr(module, "credentials_path", client_secrets_file, raising=False)
    monkeypatch.setattr(module, "REDIRECT_URI", REDIRECT_URI, raising=False)
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


# --- Pages of the app -----------------------------------------------------------

PAGE_SCRIPT = """
import builtins
builtins.OAUTH_AUTOGRADER_PAGE()
"""


class Page:
    """One page load in the browser, run as a real Streamlit script."""

    def __init__(self, at, result=None, error=None):
        self.at = at
        self.result = result
        self.error = error

    @property
    def session_state(self) -> dict:
        state = self.at.session_state
        return {key: state[key] for key in state.filtered_state}


def _run_page(oauth, tmp_path, body, query_params=None, patches=()):
    """Runs `body(OAuthLogin())` as a Streamlit page."""
    outcome = {}

    def page():
        try:
            login = oauth.OAuthLogin()
            login.creds_path = oauth.credentials_path
            login.redirect_uri = REDIRECT_URI
            outcome["result"] = body(login)
        except Exception as error:
            outcome["error"] = error
            raise

    script = tmp_path / "page.py"
    script.write_text(PAGE_SCRIPT)
    builtins.OAUTH_AUTOGRADER_PAGE = page
    try:
        at = AppTest.from_file(str(script), default_timeout=30)
        for key, value in (query_params or {}).items():
            at.query_params[key] = value
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            at.run()
    finally:
        del builtins.OAUTH_AUTOGRADER_PAGE
    return Page(at, outcome.get("result"), outcome.get("error"))


def _describe(error) -> str:
    return f"{type(error).__name__}: {error}"


def _click_login_link(oauth, tmp_path):
    """The sign-in page: renders the "Login with Google" button."""
    page = _run_page(oauth, tmp_path, lambda login: login.login_link())
    if page.error is not None:
        pytest.fail(f"login_link() raised {_describe(page.error)}")
    buttons = page.at.get("link_button")
    assert len(buttons) == 1, f"login_link() should render one st.link_button, rendered {len(buttons)}"
    button = buttons[0].proto
    return page, button, button.url, parse_qs(urlparse(button.url).query)


def _redirect_params(consent_url_params, code=AUTH_CODE, state=None):
    """What Google puts on its redirect back to REDIRECT_URI."""
    return {
        "state": state if state is not None else consent_url_params["state"][0],
        "code": code,
        "scope": " ".join(GRANTED_SCOPES),
        "authuser": "0",
        "prompt": "consent",
        "iss": "https://accounts.google.com",
    }


def _return_from_google(oauth, tmp_path, query_params, token_endpoint=None, verify=None):
    """Google's redirect: a new session whose page calls download_creds()."""
    token_endpoint = token_endpoint or FakeGoogleTokenEndpoint()
    verify = verify or mock.Mock(return_value=dict(ID_TOKEN_CLAIMS))
    patches = (
        mock.patch("requests_oauthlib.OAuth2Session.request", autospec=True, side_effect=token_endpoint),
        mock.patch("google.oauth2.id_token.verify_oauth2_token", verify),
    )
    page = _run_page(oauth, tmp_path, lambda login: login.download_creds(),
                     query_params=query_params, patches=patches)
    return page, token_endpoint, verify


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _saved_verifier(browser):
    return browser.jar.get("code_verifier") or browser.jar.get("oauth_code_verifier")


# --- Step 1: the sign-in page (login_link) ------------------------------------

def test_login_link_points_at_googles_consent_screen(oauth, tmp_path):
    _, _, url, params = _click_login_link(oauth, tmp_path)
    parsed = urlparse(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "accounts.google.com", "/o/oauth2/auth")
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [REDIRECT_URI]


def test_login_link_asks_for_offline_access_and_consent(oauth, tmp_path):
    """Without access_type=offline and prompt=consent Google sends no refresh
    token, and the user would have to sign in again every hour."""
    _, _, _, params = _click_login_link(oauth, tmp_path)
    assert params.get("access_type") == ["offline"]
    assert params.get("prompt") == ["consent"]


def test_login_link_requests_calendar_tasks_and_identity_scopes(oauth, tmp_path):
    _, _, _, params = _click_login_link(oauth, tmp_path)
    scopes = set(params["scope"][0].split())
    for scope in ("openid", "https://www.googleapis.com/auth/userinfo.email",
                  "https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/tasks"):
        assert scope in scopes, f"missing scope {scope}"


def test_login_link_saves_state_cookie_matching_the_url(oauth, tmp_path, browser):
    _, _, _, params = _click_login_link(oauth, tmp_path)
    assert params.get("state"), "the consent URL needs a state parameter"
    assert browser.jar.get("oauth_state") == params["state"][0], \
        "oauth_state must be saved in the browser's cookies (CookieController.set) to check Google's redirect"


def test_login_link_uses_pkce_and_saves_the_code_verifier(oauth, tmp_path, browser):
    _, _, _, params = _click_login_link(oauth, tmp_path)
    assert params.get("code_challenge_method") == ["S256"]
    verifier = _saved_verifier(browser)
    assert verifier, "the PKCE code_verifier must be saved in the browser's cookies"
    assert params["code_challenge"] == [_pkce_challenge(verifier)], \
        "the saved code_verifier must be the one the consent URL's code_challenge was made from"


def test_login_link_button_says_google(oauth, tmp_path):
    _, button, _, _ = _click_login_link(oauth, tmp_path)
    assert "google" in button.label.lower()


def test_each_login_link_gets_a_fresh_state_and_verifier(oauth, tmp_path, browser):
    _, _, _, first = _click_login_link(oauth, tmp_path)
    first_verifier = _saved_verifier(browser)
    _, _, _, second = _click_login_link(oauth, tmp_path)
    assert first["state"] != second["state"]
    assert first_verifier != _saved_verifier(browser)


# --- Steps 2-3: back from Google (download_creds) -----------------------------

@pytest.fixture
def signed_in(oauth, tmp_path, browser, firestore_db):
    """The whole sign-in: the sign-in page, Google, and the page Google returns to."""
    _, _, _, params = _click_login_link(oauth, tmp_path)
    verifier_at_login = _saved_verifier(browser)
    page, token_endpoint, verify = _return_from_google(oauth, tmp_path, _redirect_params(params))
    if page.error is not None:
        pytest.fail(f"download_creds() raised {_describe(page.error)}")
    return types.SimpleNamespace(
        page=page, result=page.result, token_endpoint=token_endpoint, verify=verify,
        verifier_at_login=verifier_at_login, docs=firestore_db.docs,
    )


def test_download_creds_exchanges_the_code_at_googles_token_endpoint(signed_in):
    assert len(signed_in.token_endpoint.requests) == 1, "the code must be sent to Google exactly once"
    request = signed_in.token_endpoint.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://oauth2.googleapis.com/token"
    assert request["form"]["grant_type"] == "authorization_code"
    assert request["form"]["code"] == AUTH_CODE
    assert request["form"]["redirect_uri"] == REDIRECT_URI


def test_download_creds_sends_the_pkce_verifier_saved_at_login(signed_in):
    sent = signed_in.token_endpoint.requests[0]["form"].get("code_verifier")
    assert sent == signed_in.verifier_at_login, \
        "Google rejects the code (invalid_grant) unless the code_verifier matches the one from login_link"


def test_download_creds_checks_the_id_token_for_this_client(signed_in):
    signed_in.verify.assert_called_once()
    args, kwargs = signed_in.verify.call_args
    assert args[0] == ID_TOKEN
    audience = kwargs.get("audience", args[2] if len(args) > 2 else None)
    assert audience == CLIENT_ID, \
        f"verify_oauth2_token's audience must be the OAuth client ID from the client-secrets file, got {audience!r}"


def test_download_creds_saves_one_firestore_document_keyed_by_google_user_id(signed_in):
    assert len(signed_in.docs) == 1, f"expected one Firestore document, got {list(signed_in.docs)}"
    (_, doc_id), doc = next(iter(signed_in.docs.items()))
    assert doc_id == GOOGLE_USER_ID
    assert doc.get("email") == GOOGLE_EMAIL


def _saved_doc(signed_in) -> dict:
    if not signed_in.docs:
        pytest.fail("nothing was saved to Firestore")
    return next(iter(signed_in.docs.values()))


def _readable(doc) -> str:
    """Everything in a Firestore document, with any value encrypted with
    GOOGLE_CREDS_ENCRYPTION_KEY decrypted, whatever the field names."""
    from cryptography.fernet import Fernet, InvalidToken
    fernet = Fernet(os.environ["GOOGLE_CREDS_ENCRYPTION_KEY"].encode())
    parts = []

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, (str, bytes)):
            text = value.decode() if isinstance(value, bytes) else value
            parts.append(text)
            try:
                parts.append(fernet.decrypt(text.encode()).decode())
            except (InvalidToken, ValueError):
                pass
        else:
            parts.append(str(value))
    walk(doc)
    return "\n".join(parts)


def test_download_creds_saves_the_refresh_token(signed_in):
    """Without the refresh token the stored credentials stop working after an hour."""
    assert REFRESH_TOKEN in _readable(_saved_doc(signed_in)), "the refresh token must be part of what is saved"


def test_download_creds_saves_what_is_needed_to_refresh(signed_in):
    saved = _readable(_saved_doc(signed_in))
    assert CLIENT_ID in saved, "client_id is needed to refresh the access token"
    assert "https://oauth2.googleapis.com/token" in saved, "token_uri is needed to refresh the access token"


def test_download_creds_does_not_store_tokens_in_plain_text(signed_in):
    """The Privacy Policy says tokens are stored encrypted (Fernet, GOOGLE_CREDS_ENCRYPTION_KEY)."""
    stored = json.dumps(_saved_doc(signed_in))
    assert REFRESH_TOKEN not in stored, "refresh token saved in plain text"
    assert ACCESS_TOKEN not in stored, "access token saved in plain text"


def test_download_creds_puts_user_id_in_session_state(signed_in):
    assert signed_in.page.session_state.get("user_id") == GOOGLE_USER_ID


def test_download_creds_saves_user_id_cookie(signed_in, browser):
    assert browser.jar.get("user_id") == GOOGLE_USER_ID, \
        "user_id must be saved with CookieController so a refresh can find the credentials"


def test_download_creds_never_puts_tokens_in_cookies(signed_in, browser):
    cookies = json.dumps(browser.jar)
    assert ACCESS_TOKEN not in cookies and REFRESH_TOKEN not in cookies


def test_download_creds_returns_working_credentials(signed_in):
    creds = signed_in.result
    assert creds is not None, "download_creds() should return the credentials"
    assert creds.token == ACCESS_TOKEN
    assert creds.refresh_token == REFRESH_TOKEN
    assert creds.client_id == CLIENT_ID


# --- Back from Google, but something is wrong ---------------------------------

def _assert_signed_out(page, browser, firestore_db):
    assert firestore_db.docs == {}, "nothing may be saved to Firestore"
    assert "user_id" not in browser.jar, "no user_id cookie may be set"
    assert not page.session_state.get("user_id"), "no user_id may be put in session_state"


def test_download_creds_rejects_redirect_without_code(oauth, tmp_path, browser, firestore_db):
    _, _, _, params = _click_login_link(oauth, tmp_path)
    query = _redirect_params(params)
    del query["code"]
    page, endpoint, _ = _return_from_google(oauth, tmp_path, query)
    assert endpoint.requests == []
    _assert_signed_out(page, browser, firestore_db)


def test_download_creds_rejects_tampered_state(oauth, tmp_path, browser, firestore_db):
    """A state that isn't the one login_link saved means the redirect didn't come
    from this browser's sign-in (CSRF): the code must not be exchanged."""
    _, _, _, params = _click_login_link(oauth, tmp_path)
    page, endpoint, _ = _return_from_google(oauth, tmp_path, _redirect_params(params, state="attacker-chosen-state"))
    assert endpoint.requests == [], "the code was sent to Google despite a state mismatch"
    _assert_signed_out(page, browser, firestore_db)


def test_download_creds_ignores_a_code_verifier_put_in_the_url(oauth, tmp_path, browser, firestore_db):
    """Google never sends code_verifier on its redirect. One in the URL was put
    there by someone else and must not replace the cookie saved at login."""
    _, _, _, params = _click_login_link(oauth, tmp_path)
    verifier_at_login = _saved_verifier(browser)
    query = {**_redirect_params(params), "code_verifier": "attacker-chosen-verifier"}
    page, endpoint, _ = _return_from_google(oauth, tmp_path, query)
    sent = [r["form"].get("code_verifier") for r in endpoint.requests]
    assert "attacker-chosen-verifier" not in sent
    assert sent in ([], [verifier_at_login])


def test_download_creds_rejects_when_code_verifier_cookie_is_gone(oauth, tmp_path, browser, firestore_db):
    _, _, _, params = _click_login_link(oauth, tmp_path)
    browser.jar.pop("code_verifier", None)
    browser.jar.pop("oauth_code_verifier", None)
    page, endpoint, _ = _return_from_google(oauth, tmp_path, _redirect_params(params))
    assert endpoint.requests == [], "there is no code_verifier to send, so the code must not be sent"
    _assert_signed_out(page, browser, firestore_db)


def test_download_creds_handles_google_rejecting_the_code(oauth, tmp_path, browser, firestore_db):
    """An expired or already used code: Google answers 400 invalid_grant."""
    _, _, _, params = _click_login_link(oauth, tmp_path)
    endpoint = FakeGoogleTokenEndpoint(status=400, body={"error": "invalid_grant", "error_description": "Bad Request"})
    page, _, _ = _return_from_google(oauth, tmp_path, _redirect_params(params), token_endpoint=endpoint)
    _assert_signed_out(page, browser, firestore_db)


def test_download_creds_handles_id_token_for_another_app(oauth, tmp_path, browser, firestore_db):
    _, _, _, params = _click_login_link(oauth, tmp_path)
    verify = mock.Mock(side_effect=ValueError("Token has wrong audience"))
    page, _, _ = _return_from_google(oauth, tmp_path, _redirect_params(params), verify=verify)
    _assert_signed_out(page, browser, firestore_db)
