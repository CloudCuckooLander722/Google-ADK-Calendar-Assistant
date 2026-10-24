"""
Autograder for authentication_login/oauth_login.py: OAuthLogin.login_link and
OAuthLogin.download_creds (REQUIREMENTS.md 9.1).

Simulates a real sign-in end to end, without the network:

  1. login_link() on the sign-in page builds Google's consent URL from a real
     client-secrets file and saves oauth_state and the PKCE code_verifier in
     the browser's cookies.
  2. Google redirects back with ?code=...&state=... in a NEW Streamlit session
     (empty session_state); only the browser's cookies survive.
  3. download_creds() sends the code and code_verifier to Google's token
     endpoint, which answers with a real-shaped token response (access token,
     refresh token, ID token), checks the ID token, and saves the credentials
     in Firestore under the user's Google ID ("sub").

Only the outside world is faked: Google's token endpoint (at the HTTP layer,
so the real google-auth-oauthlib / requests-oauthlib code runs), Google's ID
token check, Firestore, the browser's cookies, and Streamlit's page elements.
The fakes enforce the real APIs (st.link_button's signature and icon rules,
read-only st.context.cookies, CookieController's get/set/remove).

    cd my-adk-agent && ../.venv/bin/python -m pytest authentication_login_autograders -v

Set OAUTH_LOGIN_MODULE to grade a different module (default
authentication_login.oauth_login).
"""
import base64
import hashlib
import importlib
import inspect
import json
import os
import sys
import types
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import streamlit as real_st
from streamlit.string_util import validate_icon_or_emoji

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

class _State(dict):
    """st.session_state / st.query_params: a dict with attribute access."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None

    def __setattr__(self, key, value):
        self[key] = value


class FakeBrowserCookies:
    """streamlit_cookies_controller.CookieController, backed by one dict that
    plays the browser's cookie jar (it survives Google's redirect)."""

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


def _fake_link_button(calls):
    signature = inspect.signature(real_st.link_button)

    def link_button(*args, **kwargs):
        # Same arguments Streamlit accepts (TypeError otherwise), same icon rules.
        bound = signature.bind(*args, **kwargs)
        if bound.arguments.get("icon") is not None:
            validate_icon_or_emoji(bound.arguments["icon"])
        calls.append(bound.arguments)
        return False
    return link_button


class Browser:
    """One browser: a cookie jar that outlives Streamlit sessions."""

    def __init__(self):
        self.jar = {}
        self.cookies = FakeBrowserCookies(self.jar)


class Session:
    """One Streamlit session (a page load) in that browser."""

    def __init__(self, browser, query_params=None):
        self.browser = browser
        self.link_buttons = []
        st = mock.MagicMock(name="st")
        st.session_state = _State()
        st.query_params = _State(query_params or {})
        # st.context.cookies is read-only in Streamlit: the browser's cookies as
        # they were when the page loaded.
        st.context.cookies = types.MappingProxyType(dict(browser.jar))
        st.link_button = _fake_link_button(self.link_buttons)
        st.stop.side_effect = RuntimeError("st.stop() called")
        self.st = st


# --- Loading the module under test --------------------------------------------

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
    """authentication_login.oauth_login, imported with the outside world faked."""
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

    try:
        module = importlib.import_module(MODULE_UNDER_TEST)
    except Exception as error:  # SyntaxError, NameError, ImportError...
        pytest.fail(f"{MODULE_UNDER_TEST} does not import: {type(error).__name__}: {error}")

    # Whatever the module named it, every cookie API it uses is this browser's.
    for attr in ("cookie_controller", "controller"):
        if hasattr(module, attr):
            monkeypatch.setattr(module, attr, browser.cookies)
    if hasattr(module, "get_db"):
        monkeypatch.setattr(module, "get_db", lambda: firestore_db)
    monkeypatch.setattr(module, "credentials_path", client_secrets_file, raising=False)
    monkeypatch.setattr(module, "REDIRECT_URI", REDIRECT_URI, raising=False)
    return module


def _open(oauth, session, monkeypatch):
    """Points the module at this session's Streamlit and builds OAuthLogin."""
    monkeypatch.setattr(oauth, "st", session.st)
    try:
        login = oauth.OAuthLogin()
    except Exception as error:
        pytest.fail(f"OAuthLogin() raised {type(error).__name__}: {error}")
    login.creds_path = oauth.credentials_path
    login.redirect_uri = REDIRECT_URI
    return login


def _click_login_link(oauth, browser, monkeypatch):
    """The sign-in page: renders the "Login with Google" button."""
    page = Session(browser)
    login = _open(oauth, page, monkeypatch)
    login.login_link()
    assert len(page.link_buttons) == 1, "login_link() should render exactly one st.link_button"
    url = page.link_buttons[0]["url"]
    return page, url, parse_qs(urlparse(url).query)


def _google_redirect(browser, consent_url_params, code=AUTH_CODE, state=None):
    """Google sends the browser back to REDIRECT_URI: a new Streamlit session."""
    return Session(browser, query_params={
        "state": state if state is not None else consent_url_params["state"][0],
        "code": code,
        "scope": " ".join(GRANTED_SCOPES),
        "authuser": "0",
        "prompt": "consent",
        "iss": "https://accounts.google.com",
    })


def _download(oauth, callback, monkeypatch, token_endpoint=None, claims=None):
    token_endpoint = token_endpoint or FakeGoogleTokenEndpoint()
    verify = mock.Mock(return_value=dict(claims or ID_TOKEN_CLAIMS))
    login = _open(oauth, callback, monkeypatch)
    with mock.patch("requests_oauthlib.OAuth2Session.request", autospec=True, side_effect=token_endpoint), \
         mock.patch("google.oauth2.id_token.verify_oauth2_token", verify):
        result = login.download_creds()
    return result, token_endpoint, verify


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _saved_verifier(browser):
    return browser.jar.get("code_verifier") or browser.jar.get("oauth_code_verifier")


# --- login_link -----------------------------------------------------------------

def test_login_link_points_at_googles_consent_screen(oauth, browser, monkeypatch):
    _, url, params = _click_login_link(oauth, browser, monkeypatch)
    parsed = urlparse(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "accounts.google.com", "/o/oauth2/auth")
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [REDIRECT_URI]


def test_login_link_asks_for_offline_access_and_consent(oauth, browser, monkeypatch):
    """Without access_type=offline and prompt=consent Google sends no refresh
    token, and the user would have to sign in again every hour."""
    _, _, params = _click_login_link(oauth, browser, monkeypatch)
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]


def test_login_link_requests_calendar_tasks_and_identity_scopes(oauth, browser, monkeypatch):
    _, _, params = _click_login_link(oauth, browser, monkeypatch)
    scopes = set(params["scope"][0].split())
    for scope in ("openid", "https://www.googleapis.com/auth/userinfo.email",
                  "https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/tasks"):
        assert scope in scopes, f"missing scope {scope}"


def test_login_link_saves_state_cookie_matching_the_url(oauth, browser, monkeypatch):
    _, _, params = _click_login_link(oauth, browser, monkeypatch)
    assert params.get("state"), "the consent URL needs a state parameter"
    assert browser.jar.get("oauth_state") == params["state"][0], \
        "oauth_state must be saved in the browser's cookies (CookieController.set) to check Google's redirect"


def test_login_link_uses_pkce_and_saves_the_code_verifier(oauth, browser, monkeypatch):
    _, _, params = _click_login_link(oauth, browser, monkeypatch)
    assert params.get("code_challenge_method") == ["S256"]
    verifier = _saved_verifier(browser)
    assert verifier, "the PKCE code_verifier must be saved in the browser's cookies"
    assert params["code_challenge"] == [_pkce_challenge(verifier)], \
        "the saved code_verifier must be the one the consent URL's code_challenge was made from"


def test_login_link_renders_a_valid_streamlit_link_button(oauth, browser, monkeypatch):
    page, url, _ = _click_login_link(oauth, browser, monkeypatch)
    button = page.link_buttons[0]
    assert "google" in button["label"].lower()
    assert button["url"] == url


def test_each_login_link_gets_a_fresh_state_and_verifier(oauth, browser, monkeypatch):
    _, _, first = _click_login_link(oauth, browser, monkeypatch)
    first_verifier = _saved_verifier(browser)
    _, _, second = _click_login_link(oauth, browser, monkeypatch)
    assert first["state"] != second["state"]
    assert first_verifier != _saved_verifier(browser)


# --- download_creds: a real sign-in ---------------------------------------------

@pytest.fixture
def signed_in(oauth, browser, monkeypatch, firestore_db):
    """The whole flow: login_link, Google's redirect, download_creds."""
    _, _, params = _click_login_link(oauth, browser, monkeypatch)
    verifier_at_login = _saved_verifier(browser)
    callback = _google_redirect(browser, params)
    result, token_endpoint, verify = _download(oauth, callback, monkeypatch)
    return types.SimpleNamespace(
        result=result, callback=callback, token_endpoint=token_endpoint, verify=verify,
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
    assert audience == CLIENT_ID, "verify_oauth2_token's audience must be the OAuth client ID from the client-secrets file"


def test_download_creds_saves_one_firestore_document_keyed_by_google_user_id(signed_in):
    assert len(signed_in.docs) == 1, f"expected one Firestore document, got {list(signed_in.docs)}"
    (_, doc_id), doc = next(iter(signed_in.docs.items()))
    assert doc_id == GOOGLE_USER_ID
    assert doc.get("email") == GOOGLE_EMAIL


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
    doc = next(iter(signed_in.docs.values()))
    assert REFRESH_TOKEN in _readable(doc), "the refresh token must be part of what is saved"


def test_download_creds_saves_what_is_needed_to_refresh(signed_in):
    saved = _readable(next(iter(signed_in.docs.values())))
    assert CLIENT_ID in saved, "client_id is needed to refresh the access token"
    assert "https://oauth2.googleapis.com/token" in saved, "token_uri is needed to refresh the access token"


def test_download_creds_does_not_store_tokens_in_plain_text(signed_in):
    """The Privacy Policy says tokens are stored encrypted (Fernet, GOOGLE_CREDS_ENCRYPTION_KEY)."""
    stored = json.dumps(next(iter(signed_in.docs.values())))
    assert REFRESH_TOKEN not in stored, "refresh token saved in plain text"
    assert ACCESS_TOKEN not in stored, "access token saved in plain text"


def test_download_creds_puts_user_id_in_session_state(signed_in):
    assert signed_in.callback.st.session_state.get("user_id") == GOOGLE_USER_ID


def test_download_creds_saves_user_id_cookie(signed_in, browser):
    assert browser.jar.get("user_id") == GOOGLE_USER_ID, \
        "user_id must be saved with CookieController so a refresh can find the credentials"


def test_download_creds_never_puts_tokens_in_cookies(signed_in, browser):
    cookies = json.dumps(browser.jar)
    assert ACCESS_TOKEN not in cookies and REFRESH_TOKEN not in cookies


def test_download_creds_returns_working_credentials(signed_in):
    creds = signed_in.result
    if creds is None:
        pytest.skip("download_creds returns nothing; nothing to check")
    assert creds.token == ACCESS_TOKEN
    assert creds.refresh_token == REFRESH_TOKEN
    assert creds.client_id == CLIENT_ID


# --- download_creds: bad callbacks --------------------------------------------

def _assert_signed_out(browser, callback, firestore_db):
    assert firestore_db.docs == {}, "nothing may be saved to Firestore"
    assert "user_id" not in browser.jar, "no user_id cookie may be set"
    assert not callback.st.session_state.get("user_id")


def _download_rejected(oauth, callback, monkeypatch, **kwargs):
    """download_creds may signal failure by returning None or raising; it must
    never sign anyone in."""
    try:
        return _download(oauth, callback, monkeypatch, **kwargs)
    except Exception:
        return None, None, None


def test_download_creds_rejects_redirect_without_code(oauth, browser, monkeypatch, firestore_db):
    _, _, params = _click_login_link(oauth, browser, monkeypatch)
    callback = _google_redirect(browser, params)
    del callback.st.query_params["code"]
    _download_rejected(oauth, callback, monkeypatch)
    _assert_signed_out(browser, callback, firestore_db)


def test_download_creds_rejects_tampered_state(oauth, browser, monkeypatch, firestore_db):
    """A state that isn't the one login_link saved means the redirect didn't come
    from this browser's sign-in (CSRF): the code must not be exchanged."""
    _, _, params = _click_login_link(oauth, browser, monkeypatch)
    callback = _google_redirect(browser, params, state="attacker-chosen-state")
    endpoint = FakeGoogleTokenEndpoint()
    _download_rejected(oauth, callback, monkeypatch, token_endpoint=endpoint)
    assert endpoint.requests == [], "the code was sent to Google despite a state mismatch"
    _assert_signed_out(browser, callback, firestore_db)


def test_download_creds_rejects_when_code_verifier_cookie_is_gone(oauth, browser, monkeypatch, firestore_db):
    _, _, params = _click_login_link(oauth, browser, monkeypatch)
    browser.jar.pop("code_verifier", None)
    browser.jar.pop("oauth_code_verifier", None)
    callback = _google_redirect(browser, params)
    _download_rejected(oauth, callback, monkeypatch)
    _assert_signed_out(browser, callback, firestore_db)


def test_download_creds_handles_google_rejecting_the_code(oauth, browser, monkeypatch, firestore_db):
    """An expired or already used code: Google answers 400 invalid_grant."""
    _, _, params = _click_login_link(oauth, browser, monkeypatch)
    callback = _google_redirect(browser, params)
    endpoint = FakeGoogleTokenEndpoint(status=400, body={"error": "invalid_grant", "error_description": "Bad Request"})
    _download_rejected(oauth, callback, monkeypatch, token_endpoint=endpoint)
    _assert_signed_out(browser, callback, firestore_db)


def test_download_creds_handles_id_token_for_another_app(oauth, browser, monkeypatch, firestore_db):
    _, _, params = _click_login_link(oauth, browser, monkeypatch)
    callback = _google_redirect(browser, params)
    login = _open(oauth, callback, monkeypatch)
    with mock.patch("requests_oauthlib.OAuth2Session.request", autospec=True, side_effect=FakeGoogleTokenEndpoint()), \
         mock.patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("Token has wrong audience")):
        try:
            login.download_creds()
        except Exception:
            pass
    _assert_signed_out(browser, callback, firestore_db)
