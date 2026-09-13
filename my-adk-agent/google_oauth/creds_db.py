"""
creds_db.py

Persistent, encrypted storage for Google OAuth credentials, keyed by user_id,
backed by Firestore.

Both the Streamlit app (OAuthLogin) and the separate backend function
read/write through this module, so there's exactly one source of truth
and one place that knows the schema.

Why Firestore and not the local SQLite file this used to be: Cloud Run runs
multiple stateless instances with no shared disk, and can scale an instance
to zero at any time. A user's credentials written to instance A's local
SQLite file were invisible to instance B and gone on the next cold start.
Firestore gives every instance a consistent, durable view of the same
per-user document, with no infrastructure to run.

Auth: uses Application Default Credentials via google-cloud-firestore. On
Cloud Run this is the attached service account automatically. For local
dev, run `gcloud auth application-default login` once, or point
GOOGLE_APPLICATION_CREDENTIALS at a service account key file.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from google.cloud import firestore

_COLLECTION = os.environ.get("FIRESTORE_CREDS_COLLECTION", "users_google_creds")
_PROJECT_ID = os.environ.get("PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")

# FIX: Encryption key MUST come from environment / secrets manager, never hardcoded.
# Generate one once with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# and store it as an env var (e.g. GOOGLE_CREDS_ENCRYPTION_KEY) outside version control.
_ENCRYPTION_KEY = os.environ.get("GOOGLE_CREDS_ENCRYPTION_KEY")
if not _ENCRYPTION_KEY:
    raise RuntimeError(
        "GOOGLE_CREDS_ENCRYPTION_KEY is not set. Generate one with "
        "Fernet.generate_key() and set it as an environment variable "
        "before storing any credentials."
    )
_fernet = Fernet(_ENCRYPTION_KEY.encode() if isinstance(_ENCRYPTION_KEY, str) else _ENCRYPTION_KEY)

_client: firestore.Client | None = None


def _get_client() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(project=_PROJECT_ID) if _PROJECT_ID else firestore.Client()
    return _client


def _encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _fernet.encrypt(value.encode()).decode()


def _decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _fernet.decrypt(value.encode()).decode()


def init_db():
    """No schema to create in Firestore. Kept so existing callers that run
    this once at app startup don't need to change."""
    return None


def upsert_credentials(user_id: str, creds, email: str | None = None):
    """
    Save (insert or update) a google.oauth2.credentials.Credentials object
    for a given user_id. Tokens are encrypted at rest.
    """
    doc = {
        "user_id": user_id,
        "email": email,
        "access_token": _encrypt(creds.token),
        "client_id": creds.client_id,
        "client_secret": _encrypt(creds.client_secret),
        "token_uri": creds.token_uri,
        "scopes": list(creds.scopes) if creds.scopes else [],
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # NOTE: refresh_token is only ever sent by Google on the *first*
    # consent (or when prompt=consent is forced). Omitting the key here
    # (rather than writing None) combined with merge=True means a later
    # token refresh won't wipe out the refresh_token already stored.
    if creds.refresh_token:
        doc["refresh_token"] = _encrypt(creds.refresh_token)

    _get_client().collection(_COLLECTION).document(user_id).set(doc, merge=True)


def get_credentials_dict(user_id: str) -> dict | None:
    """
    Returns the decrypted, raw fields for a user, or None if not found.
    Callers reconstruct a Credentials object from this (see credentials_store.py)
    rather than getting one back directly, to keep this module free of
    google-auth import requirements on the backend side if not needed.
    """
    snapshot = _get_client().collection(_COLLECTION).document(user_id).get()
    if not snapshot.exists:
        return None

    row = snapshot.to_dict()
    return {
        "user_id": row.get("user_id", user_id),
        "email": row.get("email"),
        "token": _decrypt(row.get("access_token")),
        "refresh_token": _decrypt(row.get("refresh_token")),
        "token_uri": row.get("token_uri"),
        "client_id": row.get("client_id"),
        "client_secret": _decrypt(row.get("client_secret")),
        "scopes": row.get("scopes") or [],
        "expiry": row.get("expiry"),
    }


def delete_credentials(user_id: str):
    """Use on logout / revoke."""
    _get_client().collection(_COLLECTION).document(user_id).delete()
