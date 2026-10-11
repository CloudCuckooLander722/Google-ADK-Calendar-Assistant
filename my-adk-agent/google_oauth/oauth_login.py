import os
from pathlib import Path
import sys
from dotenv import load_dotenv
load_dotenv()

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent          # my-adk-agent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)

# now it's safe to import google_oauth as a package
import streamlit as st
import extra_streamlit_components as stx
from google_auth_oauthlib.flow import Flow
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from google_oauth import creds_db
from google_oauth.credentials_store import get_valid_credentials
import functools
import json
import logging
import requests
from typing import Optional
from googleapiclient.discovery import build

_ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")

_DEV_REDIRECT_URI_FALLBACK = "https://fluffy-space-xylophone-5g4jv4qp99r72p7gq-8501.app.github.dev/"
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI")
if not REDIRECT_URI:
    if _ENVIRONMENT == "production":
        raise RuntimeError(
            "OAUTH_REDIRECT_URI must be set in production -- it has no dev "
            "fallback. Set it to this service's public HTTPS URL and add "
            "that same URL to the OAuth client's authorized redirect URIs "
            "in Google Cloud Console."
        )
    REDIRECT_URI = _DEV_REDIRECT_URI_FALLBACK


# Allow HTTP traffic for local/dev environments (Codespaces) only. Must stay
# unset in production so HTTPS is enforced on the OAuth redirect_uri.
if _ENVIRONMENT != "production":
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

creds_db.init_db()


def _secret_or_env(key: str, default: str | None = None) -> str | None:
    """
    Streamlit-first configuration fallback.

    1. Try `st.secrets['KEY']` when the calling context is a Streamlit app.
    2. If that raises or yields an empty value, fall back to `os.environ.get('KEY')`.
    3. If still empty, return the explicit default.

    This keeps credentials and token file paths portable across Streamlit,
    Codespaces, local dev, and Render-style deployment by avoiding hardcoded
    filesystem assumptions.
    """
    try:
        value = st.secrets[key]
        if value:
            return str(value)
    except Exception:
        pass

    value = os.environ.get(key)
    if value:
        return str(value)

    return default


def get_cookie_manager():
    if "cookie_manager" not in st.session_state:
        st.session_state.cookie_manager = stx.CookieManager()
    return st.session_state.cookie_manager


# FIX: added openid + userinfo.email so Google's token response includes an
# ID token we can decode for a stable, permanent user_id (the "sub" claim).
# This is what lets a *separate backend function* look up the same user's
# credentials later -- cookies alone can't get creds to a backend process.
DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.file'
SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/tasks',
    # Only the files the user picks in the Google Drive picker, never the whole Drive.
    DRIVE_SCOPE,
]


def _resolve_credentials_path() -> str:
    """
    Resolve Google OAuth client-secrets JSON path cross-platform.

    Prefer the configured environment or Streamlit secret (GOOGLE_CREDENTIALS_PATH),
    normalizing a relative value to the active my-adk-agent directory. In
    production this should point at a Secret Manager secret mounted as a
    volume (e.g. /secrets/credentials.json) -- there is no dev fallback
    there, so a missing/misconfigured path fails loudly at startup instead
    of surfacing as an opaque OAuth error later.
    """
    configured = _secret_or_env("GOOGLE_CREDENTIALS_PATH")
    if configured:
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute():
            configured_path = APP_ROOT / configured_path
        if configured_path.exists():
            return str(configured_path)
        if _ENVIRONMENT == "production":
            raise RuntimeError(
                f"GOOGLE_CREDENTIALS_PATH is set to '{configured_path}' but "
                "that file does not exist."
            )

    if _ENVIRONMENT == "production":
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_PATH must be set in production to the "
            "mounted OAuth client-secrets file (e.g. a Secret Manager "
            "volume mount)."
        )

    # Local dev fallback only; not used in production (see above).
    repo_config_credentials_path = APP_ROOT / "config" / "credentials.json"
    return str(repo_config_credentials_path)


credentials_path = _resolve_credentials_path()

# How many runs get_creds waits for the browser's cookies after Google's redirect.
OAUTH_COOKIE_WAITS_KEY = "oauth_cookie_waits"
MAX_OAUTH_COOKIE_WAITS = 3


class OAuthLogin:
    def __init__(self):
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        self.redirect_uri = REDIRECT_URI
        self.scopes = SCOPES
        self.cookie_manager = get_cookie_manager()
        self.creds_path = credentials_path

    def login(self):
        st.set_page_config(page_title="WorkFlow", layout="wide")
        st.title("Automated Scheduling Assistant (Powered by ADK & Gemini)")
        st.markdown("This application uses the Google Agent Development Kit (ADK) to automate your scheduling, implement + critique planning.")
        st.divider()
        if 'code' not in st.query_params:
            authorization_url = self.init_google_auth()
            st.link_button(
                "Log in with Google",
                authorization_url
            )
            st.stop()


    def init_google_auth(self):
        try:
            flow = Flow.from_client_secrets_file(
                client_secrets_file=self.creds_path,
                scopes=self.scopes,
            )
        except (FileNotFoundError, ValueError):
            logging.exception("Failed to load OAuth client secrets from %s", self.creds_path)
            st.error("Sign-in is misconfigured. Please contact the app administrator.")
            st.stop()
        flow.redirect_uri = self.redirect_uri

        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent',
        )
        # Keep the PKCE values in the Streamlit session for the immediate
        # OAuth callback; browser cookies are written asynchronously.
        st.session_state.oauth_state = state
        st.session_state.oauth_code_verifier = flow.code_verifier
        self.cookie_manager.set(cookie="oauth_state", val=state, key='state')
        self.cookie_manager.set(cookie="oauth_code_verifier", val=flow.code_verifier, key='verifier')

        return authorization_url

    def get_creds(self):
        try:
            cookies = self.cookie_manager.get_all(key="oauth_get_creds")
        except Exception:
            logging.exception("Failed to read cookies during sign-in")
            cookies = {}

        if not cookies and 'code' not in st.query_params:
            st.info("Loading secure session context...")
            st.stop()

        # A fresh ?code is handled before the saved user_id cookie, so signing in
        # again (e.g. to grant Google Drive) replaces the stored tokens instead
        # of being ignored for the old ones.
        if 'code' in st.query_params:
            # Google's redirect starts a new Streamlit session, and the browser's
            # cookies (with the oauth_state to check the code against) only
            # arrive one rerun later. Wait for them rather than failing the code.
            saved_state = st.session_state.get("oauth_state") or cookies.get("oauth_state")
            waits = st.session_state.get(OAUTH_COOKIE_WAITS_KEY, 0)
            if not saved_state and not cookies and waits < MAX_OAUTH_COOKIE_WAITS:
                st.session_state[OAUTH_COOKIE_WAITS_KEY] = waits + 1
                st.info("Finishing sign-in...")
                st.markdown('Stuck here? <a href="/login_page" target="_self">Start over</a>', unsafe_allow_html=True)
                st.stop()

            creds = self._exchange_code(cookies)
            if creds is not None:
                return creds

        # FIX: the cookie now only ever holds a user_id, never token material.
        # Actual credentials live in the encrypted DB (creds_db.py) and are
        # fetched/refreshed through the single shared helper
        # (credentials_store.get_valid_credentials), the same function the
        # backend function will call.
        if "user_id" in cookies:
            creds = get_valid_credentials(str(cookies["user_id"]))
            if creds is not None:
                return creds
            # Stored creds missing/corrupted -- fall through to re-auth.

        if 'code' in st.query_params:
            st.error("Sign-in failed. Please try logging in again.")
        return None

    def _exchange_code(self, cookies: dict):
        """Exchanges the ?code from Google's redirect for credentials and stores
        them. Returns None (after logging why) if the exchange fails."""
        code = st.query_params.get('code')
        saved_state = st.session_state.get("oauth_state") or cookies.get("oauth_state")
        # A malformed/incomplete callback URL (code present, state missing)
        # would otherwise raise an uncaught KeyError here.
        query_state = st.query_params.get("state")
        if not query_state:
            logging.warning("OAuth callback missing 'state' query param")
            return None
        try:
            if saved_state == query_state: #test before starting coding requirements
                flow = Flow.from_client_secrets_file(
                    client_secrets_file=self.creds_path,
                    scopes=self.scopes,
                    state=saved_state,
                )
                flow.redirect_uri = self.redirect_uri
                flow.code_verifier = (
                    st.session_state.get("oauth_code_verifier")
                    or cookies.get("oauth_code_verifier")
                )

                incoming_state = query_state
                current_url = f"{self.redirect_uri}?code={code}&state={incoming_state}"
                flow.fetch_token(authorization_response=current_url)

                creds = flow.credentials

                # FIX: decode the ID token Google returns alongside the
                # access token to get a stable user_id ("sub" claim) and
                # email, without an extra network round-trip via build().
                id_info = google_id_token.verify_oauth2_token(
                    creds.id_token,
                    google_requests.Request(),
                    audience=creds.client_id,
                )
                user_id = str(id_info["sub"])
                email = str(id_info.get("email"))
                st.query_params["user_id"] = user_id
                st.session_state["user_id"] = user_id

                # FIX: persist full credentials to the encrypted DB, keyed
                # by user_id. This is what the separate backend function
                # will read from later -- not cookies.
                creds_db.upsert_credentials(user_id, creds, email=email)



                # FIX: cookie now stores only the user_id (a pointer),
                # not token material. Much smaller trust surface for
                # anything that can read the user's browser storage.
                self.cookie_manager.set(cookie="user_id", val=str(user_id), key="set_user_id")
                self.cookie_manager.set(cookie="logged_in", val="true", key="set_logged_in")

                self.cookie_manager.delete("oauth_state", key="delete_oauth_state")

                st.query_params.clear()
                
                return creds
            logging.warning("OAuth callback state did not match the saved oauth_state (saved state present: %s)", bool(saved_state))
            return None
        except Exception:
            logging.exception("OAuth callback failed while exchanging authorization code")
            return None


def restore_signed_in_user() -> Optional[str]:
    """
    Puts the signed-in user's id back into st.session_state after a browser
    refresh, from the `user_id` cookie set at sign-in.

    A refresh starts a new Streamlit session, so session_state is empty and only
    the login page would otherwise create the cookie manager. Call this near the
    top of every signed-in page (not from inside agent tools: it renders the
    cookie component). The browser sends cookies on the component's first
    render, which triggers one automatic rerun, so this can return None on the
    very first run after a refresh and the id on the next.
    """
    user_id = st.session_state.get("user_id")
    if user_id:
        return user_id
    try:
        cookies = get_cookie_manager().get_all(key="restore_user_id")
    except Exception:
        logging.exception("Failed to read cookies while restoring the signed-in user")
        return None
    if isinstance(cookies, dict) and cookies.get("user_id"):
        st.session_state["user_id"] = str(cookies["user_id"])
    return st.session_state.get("user_id")

def fetch_creds():
    user_id = st.session_state.get("user_id") or st.query_params.get("user_id")

    if not user_id:
        cookie_manager = st.session_state.get("cookie_manager")
        if cookie_manager is not None:
            try:
                cookies = cookie_manager.get_all()
            except Exception:
                logging.exception("Failed to read cookies while fetching credentials")
                cookies = {}
            if isinstance(cookies, dict):
                user_id = cookies.get("user_id")

    if not user_id:
        return None

    user_id = str(user_id)
    st.session_state["user_id"] = user_id

    creds = get_valid_credentials(user_id)
    # Only drop user_id from the URL: clearing every param would also remove
    # logged_in, and the next refresh would land on the login page.
    if "user_id" in st.query_params:
        del st.query_params["user_id"]

    return creds

def get_calendar_service():
    creds = fetch_creds()
    if not creds:
        return None
    try:
        return build('calendar', 'v3', credentials=creds)
    except Exception:
        logging.exception("Failed to build the Google Calendar API client")
        return None

def get_tasks_service():
    creds = fetch_creds()
    if not creds:
        return None
    try:
        return build('tasks', 'v1', credentials=creds)
    except Exception:
        logging.exception("Failed to build the Google Tasks API client")
        return None


def get_drive_access_token() -> Optional[str]:
    """
    The signed-in user's access token for the Google Drive picker, or None if
    they signed in before WorkFlow asked for Drive (or declined it) and need to
    sign in again.
    """
    creds = fetch_creds()
    if not creds or DRIVE_SCOPE not in (creds.scopes or []):
        return None
    return creds.token


@functools.cache
def get_picker_app_id() -> Optional[str]:
    """
    The Cloud project number, which the Google Picker wants as its app ID. An
    OAuth client ID always starts with it: "<project number>-<hash>.apps.googleusercontent.com".
    """
    try:
        with open(credentials_path) as f:
            secrets = json.load(f)
        client = secrets.get("web") or secrets.get("installed") or {}
        return client["client_id"].split("-")[0]
    except (OSError, ValueError, KeyError):
        logging.exception("Failed to read the OAuth client ID from %s", credentials_path)
        return None


def delete_my_data(user_id: str) -> None:
    """
    Disconnects a user from WorkFlow: revokes the app's access at Google, deletes
    their stored (encrypted) tokens from Firestore, and signs them out of this
    browser. Raises if the stored tokens could not be deleted.
    """
    stored = creds_db.get_credentials_dict(user_id)
    token = stored and (stored.get("refresh_token") or stored.get("token"))
    if token:
        try:
            # Revoking either token revokes the whole grant for this app.
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
        except Exception:
            # Still delete our copy; the user can also revoke at
            # https://myaccount.google.com/permissions.
            logging.exception("Failed to revoke Google access for user_id=%s", user_id)

    creds_db.delete_credentials(user_id)

    cookie_manager = get_cookie_manager()
    for cookie in ("user_id", "logged_in"):
        try:
            cookie_manager.delete(cookie, key=f"delete_{cookie}")
        except Exception:
            # Harmless if the cookie is already gone: with the stored tokens
            # deleted, a leftover user_id cookie no longer signs anyone in.
            logging.exception("Failed to delete the %s cookie", cookie)
    st.session_state.pop("user_id", None)
    st.session_state.logged_in = False
    st.query_params.logged_in = "False"
