"""
credentials_store.py

Single shared entry point for turning a user_id into a valid, ready-to-use
google.oauth2.credentials.Credentials object -- refreshing it if needed.

Import this from BOTH:
  - the Streamlit app (after login, or on any page that needs creds)
  - the separate backend function that calls build()

so refresh logic exists in exactly one place.
"""

from datetime import datetime, timezone
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from google_oauth import creds_db


def _dict_to_credentials(data: dict) -> Credentials:
    creds = Credentials(
        token=data["access_token"],
        refresh_token=data["refresh_token"],
        token_uri=data["token_uri"],
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data["scopes"],
    )
    # google-auth's Credentials.expiry is a naive UTC datetime; reattach it
    # so .expired / .refresh() behave correctly instead of always looking valid.
    if data["expiry"]:
        creds.expiry = datetime.fromisoformat(data["expiry"])
    return creds


def get_valid_credentials(user_id: str) -> Credentials | None:
    """
    Fetch this user's stored credentials, refreshing the access token first
    if it's expired. Persists the refreshed token back to storage so the
    next caller (frontend or backend) doesn't have to refresh again.

    Returns None if the user has never logged in / has no stored creds.
    """
    data = creds_db.get_credentials_dict(user_id)
    if data is None:
        return None

    creds = _dict_to_credentials(data)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Persist the refreshed access token (and possibly new expiry)
        # so the DB stays current for whoever asks next.
        creds_db.upsert_credentials(user_id, creds, email=data.get("email"))

    return creds