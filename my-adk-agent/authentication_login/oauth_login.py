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

import json
import logging
import streamlit as st
from google_auth_oauthlib.flow import Flow
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from services.cookie_service import controller 
from authentication_login import creds_db

cookie_controller = controller()

SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/tasks',
    # Only the files the user picks in the Google Drive picker, never the whole Drive.
    'https://www.googleapis.com/auth/drive.file',
]

_ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")


def _secret_or_env(key: str, default: str | None = None) -> str | None:
    """
    Reads st.secrets[key], then os.environ[key], then default.
    """
    try:
        value = st.secrets[key]
        if value:
            return str(value)
    except Exception:
        pass
    return os.environ.get(key) or default


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

class OAuthLogin:
    """
    Handles the OAuth login flow for Google APIs.
    """

    def __init__(self):
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        self.redirect_uri = REDIRECT_URI
        self.scopes = SCOPES
        self.creds_path = credentials_path

    def _build_flow(self, state=None) -> Flow:
        return Flow.from_client_secrets_file(
            self.creds_path,
            scopes=self.scopes,
            redirect_uri=self.redirect_uri,
            state=state,
        )

    def _client_id(self) -> str:
        with open(self.creds_path) as f:
            secrets = json.load(f)
        return (secrets.get("web") or secrets.get("installed"))["client_id"]

    def init_flow(self):
        """
        Initializes the OAuth flow using the client secrets file and scopes.
        Saves oauth_state and the PKCE code_verifier in cookies (never the URL)
        so download_creds can check Google's redirect.
        Returns: authorization_url, flow
        """
        flow = self._build_flow()
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent',
        )

        cookie_controller.set("oauth_state", state)
        cookie_controller.set("code_verifier", flow.code_verifier)

        return authorization_url, flow

    def login_link(self):
        """
        Handles the login process, including redirecting to the Google OAuth consent screen.
        """
        authorization_url, _ = self.init_flow()

        st.link_button(
            label="Login with Google",
            url=authorization_url,
        )

    def pkce_verifier(self):
        """
        Does PKCE verifier then retrieves credentials for downloading.
        Args: self
        Returns: creds, or None if this isn't a valid redirect back from Google
        """
        auth_code = st.query_params.get('code')
        if not auth_code:
            return None

        # The state Google sends back must be the one this browser saved at
        # login; otherwise the redirect didn't come from this sign-in (CSRF).
        saved_state = cookie_controller.get("oauth_state")
        if not saved_state or st.query_params.get("state") != saved_state:
            raise ValueError("OAuth state does not match the one saved at login.")

        # Only the cookie: a code_verifier in the URL was put there by someone else.
        pkce_verifier = cookie_controller.get("code_verifier")
        if not pkce_verifier:
            raise ValueError("No PKCE code_verifier saved for this sign-in.")

        flow = self._build_flow(state=saved_state)
        flow.code_verifier = pkce_verifier
        flow.fetch_token(code=auth_code)

        # The code and verifier are single use.
        cookie_controller.remove("oauth_state")
        cookie_controller.remove("code_verifier")

        return flow.credentials

    def download_creds(self):
        """
        Downloads credentials to Firestore and saves user_id to session_state
        and cookie_controller for later retrieval.
        Returns: creds, or None if nobody signed in
        """
        try:
            creds = self.pkce_verifier()
            if creds is None:
                return None

            if not creds.id_token:
                raise ValueError("Google sent no ID token; was the openid scope granted?")

            id_info = id_token.verify_oauth2_token(
                creds.id_token,
                google_requests.Request(),
                self._client_id(),
            )
        except Exception as error:
            logging.warning("Google sign-in failed: %s", error)
            st.error("Sign-in with Google failed. Please try again.")
            return None

        user_id = id_info.get("sub")
        user_email = id_info.get("email")

        if not user_id or not user_email:
            st.error("Google did not say who signed in. Please try again.")
            return None

        # Storage first: the cookie is only a pointer to it.
        creds_db.upsert_credentials(user_id, creds, email=user_email)

        st.session_state["user_id"] = user_id
        st.query_params["user_id"] = user_id
        cookie_controller.set("user_id", user_id)

        return creds
