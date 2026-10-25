"""
Handles google db stuff for later.
"""
import datetime
import logging
import os
from google.cloud import firestore
from cryptography.fernet import Fernet, InvalidToken
import streamlit as st
from services.cookie_service import controller as cookie_controller
from google.oauth2.credentials import Credentials

_ENCRYPTION_KEY = os.environ.get("GOOGLE_CREDS_ENCRYPTION_KEY")
if not _ENCRYPTION_KEY:
    raise RuntimeError(
        "GOOGLE_CREDS_ENCRYPTION_KEY is not set. Generate one with "
        "Fernet.generate_key() and set it as an environment variable "
        "before storing any credentials."
    )
_fernetCipher = Fernet(_ENCRYPTION_KEY.encode() if isinstance(_ENCRYPTION_KEY, str) else _ENCRYPTION_KEY)

@st.cache_resource
def get_db():
    return firestore.Client(project=os.environ.get("PROJECT_ID"))


def _encrypt(input):
    if input is None:
        return None

    return _fernetCipher.encrypt(input.encode()).decode()

def _decrypt(input):
    if input is None:
        return None
    try:
        return _fernetCipher.decrypt(input.encode()).decode()
    except InvalidToken:
        # Corrupted data or the encryption key was rotated since this value
        # was written - treat as unreadable rather than crashing every
        # caller in the auth chain.
        logging.error("Failed to decrypt a stored credential field; key may have rotated.")
        return None

def upsert_credentials(user_id, creds, email=None):
    """
    Saves a Credentials object to users/{user_id}, encrypting the tokens.
    A write without a refresh_token keeps the one already stored.
    Args: user_id, creds, email
    Returns: None
    """
    row = {
        "user_id": user_id,
        "access_token": _encrypt(creds.token),
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": _encrypt(creds.client_secret),
        "scopes": list(creds.scopes or []),
        # google-auth keeps expiry as naive UTC; Firestore needs it tz-aware.
        "expiry": creds.expiry.replace(tzinfo=datetime.timezone.utc) if creds.expiry else None,
    }
    if creds.refresh_token:
        row["refresh_token"] = _encrypt(creds.refresh_token)
    if email:
        row["email"] = email

    get_db().collection("users").document(user_id).set(row, merge=True)

def _get_creds_dict():
    """
    Retrieves the dictionary of credentials from the Firestore database.
    Args: None
    returns: dict, or None if the user has no stored credentials
    """
    db = get_db()

    if st.query_params.get("user_id"):
        user_id = st.query_params.get("user_id")
    elif cookie_controller.get("user_id"):
        user_id = cookie_controller.get("user_id")
    else:
        raise ValueError("No user_id detected.")

    doc = db.collection("users").document(user_id).get()

    if not doc.exists:
        return None

    row = doc.to_dict() or {}

    return {
        "user_id": row.get("user_id", user_id),
        "email": row.get("email"),
        "access_token": _decrypt(row.get("access_token")),
        "refresh_token": _decrypt(row.get("refresh_token")),
        "token_uri": row.get("token_uri"),
        "client_id": row.get("client_id"),
        "client_secret": _decrypt(row.get("client_secret")),
        "scopes": row.get("scopes") or [],
        "expiry": row.get("expiry"),
    }

def _create_creds_object(creds_dict):
    """
    Receives a creds_dict and makes the Credentials object.
    Args: creds_dict
    Returns: Credentials object creds
    """

    creds = Credentials(
        token = creds_dict["access_token"],
        refresh_token = creds_dict["refresh_token"],
        token_uri = creds_dict["token_uri"],
        client_id = creds_dict["client_id"],
        client_secret = creds_dict["client_secret"],
        scopes = creds_dict["scopes"] or None,
    )

    # .expired compares against naive UTC, so a Firestore timestamp is converted.
    expiry = creds_dict.get("expiry")
    if isinstance(expiry, str):
        expiry = datetime.datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    if expiry is not None and expiry.tzinfo is not None:
        expiry = expiry.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    creds.expiry = expiry

    return creds


def get_valid_credentials():
    """
    Arranges for the retrieval of credentials for purposes.
    Args: none
    returns: creds in Credentials object, or None if the user has none stored.
    """

    creds_dict = _get_creds_dict()
    if creds_dict is None:
        return None
    creds = _create_creds_object(creds_dict=creds_dict)

    return creds
