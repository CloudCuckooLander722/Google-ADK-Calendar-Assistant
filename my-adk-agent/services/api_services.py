import logging
import sys
from pathlib import Path
from google.auth.transport.requests import Request
from googleapiclient.discovery import build


MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)

from authentication_login.creds_db import get_valid_credentials, upsert_credentials, _find_user_id
from authentication_login.oauth_login import DRIVE_SCOPE


def check_creds():
    """
    The signed-in user's Google credentials, refreshed (and saved back to
    Firestore) if they expired. None if nobody is signed in, or if the access
    token expired with no refresh token to renew it: either way the user has
    to sign in again.
    """
    try:
        user_id = _find_user_id()
        creds = get_valid_credentials(user_id)
    except ValueError:
        return None
    except Exception:
        logging.exception("Failed to load stored Google credentials")
        return None

    if not creds:
        return None

    if creds.expired:
        if not creds.refresh_token:
            return None
        try:
            creds.refresh(Request())
        except Exception:
            # Revoked at Google, or the grant expired.
            logging.warning("Failed to refresh Google credentials for user_id=%s", user_id, exc_info=True)
            return None
        # Save the new access token so the next caller doesn't refresh again.
        upsert_credentials(user_id, creds)

    return creds


def _build(api: str, version: str):
    creds = check_creds()
    if not creds:
        return None
    try:
        return build(api, version, credentials=creds)
    except Exception:
        logging.exception("Failed to build the Google %s API client", api)
        return None


def get_tasks_service():
    """
    Returns build object enabling access to tasks, or None if not signed in.
    """
    return _build('tasks', 'v1')


def get_calendar_service():
    """
    Returns build() object enabling access to calendar, or None if not signed in.
    """
    return _build('calendar', 'v3')


def get_drive_service():
    """
    Returns build() object enabling access to drive, or None if not signed in.
    """
    return _build('drive', 'v3')


def get_drive_access_token():
    """
    The signed-in user's access token for the Google Drive picker, or None if
    they signed in before WorkFlow asked for Drive (or declined it) and need to
    sign in again.
    """
    creds = check_creds()
    if not creds or DRIVE_SCOPE not in (creds.scopes or []):
        return None
    return creds.token
