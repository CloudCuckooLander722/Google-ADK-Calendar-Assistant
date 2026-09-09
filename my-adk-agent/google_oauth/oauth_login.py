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
import traceback
from googleapiclient.discovery import build

# Allow HTTP traffic for local/dev environments (Codespaces)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

creds_db.init_db()


def get_cookie_manager():
    if "cookie_manager" not in st.session_state:
        st.session_state.cookie_manager = stx.CookieManager()
    return st.session_state.cookie_manager

REDIRECT_URI = "https://fluffy-space-xylophone-5g4jv4qp99r72p7gq-8501.app.github.dev/"

# FIX: added openid + userinfo.email so Google's token response includes an
# ID token we can decode for a stable, permanent user_id (the "sub" claim).
# This is what lets a *separate backend function* look up the same user's
# credentials later -- cookies alone can't get creds to a backend process.
SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/tasks',
]


def _resolve_credentials_path() -> str:
    """
    Resolve a Google OAuth client-secrets file path in a portable way.

    Environment-first: respect GOOGLE_CREDENTIALS_PATH when it exists.
    But if Render or a Codespaces shell exposes a missing /etc/secrets mount,
    fall back to the credentials file already present inside this repo:
    my-adk-agent/google_oauth/credentials.json.
    """
    configured = os.getenv("GOOGLE_CREDENTIALS_PATH")
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.exists():
            return str(configured_path)

    # Preferred repo location in the present workspace.
    repo_google_oauth_path = Path(__file__).resolve().parent / "credentials.json"
    if repo_google_oauth_path.exists():
        return str(repo_google_oauth_path)

    # Also support a top-level repo fallback for a locally checked-in export.
    repo_root_path = Path(__file__).resolve().parent.parent / "credentials.json"
    if repo_root_path.exists():
        return str(repo_root_path)

    # Final fallback for developer environments that only carry the package file.
    return str(repo_google_oauth_path)


credentials_path = _resolve_credentials_path()

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
                "Log in",
                authorization_url
            )
            st.stop()


    def init_google_auth(self):
        flow = Flow.from_client_secrets_file(
            client_secrets_file=self.creds_path,
            scopes=self.scopes,
        )
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
        cookies = self.cookie_manager.get_all(key="oauth_get_creds")

        if not cookies and 'code' not in st.query_params:
            st.info("Loading secure session context...")
            st.stop()

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
            code = st.query_params.get('code')
            saved_state = st.session_state.get("oauth_state") or cookies.get("oauth_state")
            query_state = st.query_params["state"]
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
                
            except Exception as e:
                st.error(f"FULL TRACEBACK: {e}\n")  # check your server logs
                return None

        return None

def fetch_creds():
    user_id = st.session_state.get("user_id") or st.query_params.get("user_id")

    if not user_id:
        cookie_manager = st.session_state.get("cookie_manager")
        if cookie_manager is not None:
            cookies = cookie_manager.get_all()
            if isinstance(cookies, dict):
                user_id = cookies.get("user_id")

    if not user_id:
        return None

    user_id = str(user_id)
    st.session_state["user_id"] = user_id

    creds = get_valid_credentials(user_id)
    if "user_id" in st.query_params:
        st.query_params.clear()

    return creds

def get_calendar_service():
    creds = fetch_creds()
    if not creds:
        return None
    service = build('calendar', 'v3', credentials=creds)
    return service

def get_tasks_service():
    creds = fetch_creds()
    if not creds:
        return None
    service = build('tasks', 'v1', credentials=creds)
    return service