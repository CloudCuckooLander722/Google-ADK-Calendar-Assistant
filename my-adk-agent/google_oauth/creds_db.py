"""
creds_db.py

Persistent, encrypted storage for Google OAuth credentials, keyed by user_id.

Both the Streamlit app (OAuthLogin) and the separate backend function
read/write through this module, so there's exactly one source of truth
and one place that knows the schema.

Swap SQLite for Postgres later by changing only `_get_connection()` and
the SQL dialect bits (SQLite's `?` -> psycopg2's `%s`, etc.) -- the
public functions (upsert_credentials / get_credentials / delete_credentials)
should not need to change shape.
"""

from pathlib import Path
import sys
from google.oauth2.credentials import Credentials

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)


import os
import sqlite3
import json
from contextlib import contextmanager
from cryptography.fernet import Fernet


# Prefer a database location in a portable relative project path.
# Many environments (Render, Streamlit Cloud, Codespaces, local dev)
# export credentials via secrets or environment variables instead of baking
# a hardcoded /var/data path into the app.

RENDER_DB_PATH = Path('/var/data/google_oauth_creds.db')
LOCAL_DB_PATH = APP_ROOT / 'data' / 'google_oauth_creds.db'


def _secret_or_env(key: str, default: str | None = None) -> str | None:
    """
    Streamlit-first secret/env fallback helper used for cross-platform file config.

    Example:
        try:
            val = st.secrets[key]
        except Exception:
            val = os.environ.get(key)
        if not val:
            val = default
    """
    try:
        import streamlit as st
        value = st.secrets[key]
        if value:
            return str(value)
    except Exception:
        pass

    value = os.environ.get(key)
    if value:
        return str(value)

    return default


def _resolve_db_path() -> str:
    """
    Choose a portable SQLite DB file path.

    Priority:
      1. GOOGLE_DB_PATH via Streamlit secrets first then environment variable.
      2. RENDER_DB_PATH field if a Render-style environment variable or secret
         is configured and writable.
      3. LOCAL_DB_PATH under the active my-adk-agent data directory.

    Relative DATABASE URI values are resolved against APP_ROOT so the
    requested active directory context stays consistent for local/dev,
    Codespaces, and generated Docker or file-operation artifacts.
    """
    configured_path = _secret_or_env("GOOGLE_DB_PATH") or os.environ.get("GOOGLE_DB_PATH")
    if configured_path:
        candidate = Path(configured_path).expanduser()
        if not candidate.is_absolute():
            candidate = APP_ROOT / candidate
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            return str(candidate)
        except (PermissionError, OSError):
            return str(LOCAL_DB_PATH)

    render_path = _secret_or_env("RENDER_DB_PATH") or os.environ.get("RENDER_DB_PATH")
    if render_path:
        candidate = Path(render_path).expanduser()
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            return str(candidate)
        except (PermissionError, OSError):
            return str(LOCAL_DB_PATH)

    # Final cross-platform fallback: active-directory relative DB path.
    return str(LOCAL_DB_PATH)


DB_PATH = _resolve_db_path()

# Ensure the configured directory exists for the selected DB location.
try:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError):
    DB_PATH = str(LOCAL_DB_PATH)
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

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


def _encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _fernet.encrypt(value.encode()).decode()


def _decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _fernet.decrypt(value.encode()).decode()


@contextmanager
def _get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Call once at app startup to make sure the table exists."""
    with _get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users_google_creds (
                user_id        TEXT PRIMARY KEY,
                email          TEXT,
                access_token   TEXT NOT NULL,
                refresh_token  TEXT,
                client_id      TEXT NOT NULL,
                client_secret  TEXT NOT NULL,
                token_uri      TEXT NOT NULL,
                scopes         TEXT NOT NULL,
                expiry         TEXT,
                updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def upsert_credentials(user_id: str, creds, email: str | None = None):
    """
    Save (insert or update) a google.oauth2.credentials.Credentials object
    for a given user_id. Tokens are encrypted at rest.
    """
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users_google_creds
                (user_id, email, access_token, refresh_token, client_id,
                 client_secret, token_uri, scopes, expiry, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                email=excluded.email,
                access_token=excluded.access_token,
                refresh_token=COALESCE(excluded.refresh_token, users_google_creds.refresh_token),
                client_id=excluded.client_id,
                client_secret=excluded.client_secret,
                token_uri=excluded.token_uri,
                scopes=excluded.scopes,
                expiry=excluded.expiry,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                user_id,
                email,
                _encrypt(creds.token),
                _encrypt(creds.refresh_token),
                creds.client_id,
                _encrypt(creds.client_secret),
                creds.token_uri,
                json.dumps(list(creds.scopes) if creds.scopes else []),
                creds.expiry.isoformat() if creds.expiry else None,
            ),
        )
        # NOTE: refresh_token is only ever sent by Google on the *first*
        # consent (or when prompt=consent is forced). The COALESCE above
        # means a later token refresh (which has no new refresh_token)
        # won't wipe out the one you already stored.


def get_credentials_dict(user_id: str) -> dict | None:
    """
    Returns the decrypted, raw fields for a user, or None if not found.
    Callers reconstruct a Credentials object from this (see credentials_store.py)
    rather than getting one back directly, to keep this module free of
    google-auth import requirements on the backend side if not needed.
    """
    

    with _get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users_google_creds WHERE user_id = ?", (user_id,)
        ).fetchone()

    if row is None:
        return None

    return {
        "user_id": row["user_id"],
        "email": row["email"],
        "token": _decrypt(row["access_token"]),
        "refresh_token": _decrypt(row["refresh_token"]),
        "token_uri": row["token_uri"],
        "client_id": row["client_id"],
        "client_secret": _decrypt(row["client_secret"]),
        "scopes": json.loads(row["scopes"]),
        "expiry": row["expiry"]
    }


def delete_credentials(user_id: str):
    """Use on logout / revoke."""
    with _get_connection() as conn:
        conn.execute("DELETE FROM users_google_creds WHERE user_id = ?", (user_id,))


