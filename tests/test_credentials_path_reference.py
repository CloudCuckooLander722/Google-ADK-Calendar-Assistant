from pathlib import Path


def test_oauth_login_uses_credentials_path_variable_only():
    source = Path("my-adk-agent/google_oauth/oauth_login.py").read_text()

    assert "CREDS_PATH" not in source
    assert "def _resolve_credentials_path" in source
    assert "credentials_path = _resolve_credentials_path()" in source
    assert "self.creds_path = credentials_path" in source


def test_google_oauth_creds_db_uses_firestore_and_requires_encryption_key():
    source = Path("my-adk-agent/google_oauth/creds_db.py").read_text()

    assert "from google.cloud import firestore" in source
    assert "GOOGLE_CREDS_ENCRYPTION_KEY" in source
    assert "GOOGLE_DB_PATH" not in source
    assert "sqlite3" not in source


def test_calendar_agent_prompt_requires_cal_newport_style_creation_summary():
    source = Path("my-adk-agent/agents/calendar_agent.py").read_text()

    assert "Cal Newport" in source
    assert "AI-generated summary" in source
    assert "events and tasks created" in source


def test_creds_db_preserves_refresh_token_on_merge_and_supports_delete():
    source = Path("my-adk-agent/google_oauth/creds_db.py").read_text()

    assert "def upsert_credentials" in source
    assert "def get_credentials_dict" in source
    assert "def delete_credentials" in source
    assert "merge=True" in source


def test_oauth_login_supports_env_or_repo_credentials_fallback_and_homepage_about_style():
    oauth_source = Path("my-adk-agent/google_oauth/oauth_login.py").read_text()
    home_source = Path("my-adk-agent/ui/home.py").read_text()

    assert "def _resolve_credentials_path" in oauth_source
    assert "GOOGLE_CREDENTIALS_PATH" in oauth_source
    assert "credentials.json" in oauth_source
    assert "WorkFlow" in home_source
    assert "About WorkFlow" in home_source
    assert "manage your schedules" in home_source
