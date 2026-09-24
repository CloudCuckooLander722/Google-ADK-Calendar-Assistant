"""
credentials_store.py

Single shared entry point for turning a user_id into a valid, ready-to-use
google.oauth2.credentials.Credentials object -- refreshing it if needed.

Import this from BOTH:
  - the Streamlit app (after login, or on any page that needs creds)
  - the separate backend function that calls build()

so refresh logic exists in exactly one place.
"""
from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)

import logging
from datetime import datetime, timezone

from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from google_oauth import creds_db


def _dict_to_credentials(data: dict) -> Credentials | None:
    try:
        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes") or [],
        )
        # google-auth's Credentials.expiry is a naive UTC datetime; reattach it
        # so .expired / .refresh() behave correctly instead of always looking valid.
        expiry = data.get("expiry")
        if expiry:
            creds.expiry = datetime.fromisoformat(expiry)
        return creds
    except (TypeError, ValueError):
        # Malformed/corrupted stored data (e.g. an unparseable expiry
        # timestamp) - treat as "no usable credentials" rather than crash.
        logging.exception("Stored credentials for a user were malformed")
        return None


def get_valid_credentials(user_id: str) -> Credentials | None:
    """
    Fetch this user's stored credentials, refreshing the access token first
    if it's expired. Persists the refreshed token back to storage so the
    next caller (frontend or backend) doesn't have to refresh again.

    Returns None if the user has never logged in, has no stored creds, or
    the refresh fails (e.g. access was revoked) - callers treat that the
    same as "not signed in" and send the user back through login.
    """
    if not user_id:
        return None

    try:
        data = creds_db.get_credentials_dict(user_id)
    except Exception:
        logging.exception("Failed to read stored credentials for user_id=%s", user_id)
        return None

    if data is None:
        return None

    creds = _dict_to_credentials(data)
    if creds is None:
        return None

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except GoogleAuthError:
            # Refresh token revoked/expired, or Google's token endpoint is
            # unreachable - this is an expected failure mode, not a bug.
            logging.warning("Credential refresh failed for user_id=%s", user_id)
            return None

        try:
            # Persist the refreshed access token (and possibly new expiry)
            # so the DB stays current for whoever asks next.
            creds_db.upsert_credentials(user_id, creds, email=data.get("email"))
        except Exception:
            # The refreshed creds are still valid in-memory even if we
            # couldn't persist them - don't fail the caller over this.
            logging.exception("Failed to persist refreshed credentials for user_id=%s", user_id)

    return creds