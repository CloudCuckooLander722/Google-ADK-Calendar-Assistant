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

import streamlit as st
import extra_streamlit_components as stx
from google_auth_oauthlib.flow import Flow
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from google_oauth import creds_db
from google_oauth.credentials_store import get_valid_credentials
import datetime
import functools
import json
import logging
import requests
from typing import Optional
from googleapiclient.discovery import build
from cookie_service import controller as cookie_controller
from google.oauth2 import id_token
from authentication_login.creds_db import get_db


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
        self.cookie_manager = get_cookie_manager()
        self.creds_path = credentials_path
       

    def init_flow(self) -> Flow:
        """
        Initializes the OAuth flow using the client secrets file and scopes.
        """
        flow = Flow.from_client_secrets_file(
            self.creds_path,
            scopes=self.scopes,
            redirect_uri=self.redirect_uri
        )
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent',
        )
        code_verifier = flow.code_verifier

        cookie_controller.set_cookie("oauth_state", state)
        cookie_controller.set_cookie("code_verifier", code_verifier)
        

        return authorization_url, flow

    def login_link(self):
        """
        Handles the login process, including redirecting to the Google OAuth consent screen.
        """
        authorization_url, _ = self.init_flow()
        
        st.link_button(
            label="Login with Google",
            url=authorization_url,
            new_tab=True
        )

        
    def pkce_verifier(self):
        """
        Does PKCE verifier then retrieves credentials for downloading.
        Args: self
        Returns: creds
        """
        pcke_verifier = None

        auth_code = st.query_params.get('code')
        if not auth_code:
            return None

        if cookie_controller.get("code_verifier"):
            pkce_verifier = cookie_controller.get("code_verifier")

        assert pkce_verifier is not None, "pkce_verifier is None."
        
        _, flow = self.init_flow()

        assert flow is not None, "Flow should have been initialized"

        if not flow:
            raise ValueError("Failed to initialize a valid application flow.")

        flow.code_verifier = pkce_verifier

        flow.fetch_token(code=auth_code)

        creds = flow.credentials

        return creds

    def download_creds(self):
        """
        Downloads credentials to Firestore and saves user_id to cookie_controller for later retrieval.
        """
        creds = self.pkce_verifier()

        id_info = id_token.verify_oauth2_token(
            creds.id_token, 
            google_requests.Request(), 
            "YOUR_GOOGLE_CLIENT_://googleusercontent.com"
        )

        user_id = id_info["sub"]
        user_email = id_info["email"]

        if not user_id or not user_email:
            raise ValueError("No user_id or user_email, sorry.")

        db = get_db()

        assert db is not None, "db should have been initialized"

        db.collection("users").document(user_id).set({
            "email": user_email,
            "creds_json": creds.to_json()  # Saves token, refresh_token, scopes, etc.
        })





        



       
        


        
