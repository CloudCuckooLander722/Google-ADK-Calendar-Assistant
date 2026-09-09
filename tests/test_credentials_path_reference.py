from pathlib import Path


def test_oauth_login_uses_credentials_path_variable_only():
    source = Path("my-adk-agent/google_oauth/oauth_login.py").read_text()

    assert "CREDS_PATH" not in source
    assert "def _resolve_credentials_path" in source
    assert "credentials_path = _resolve_credentials_path()" in source
    assert "self.creds_path = credentials_path" in source


def test_google_oauth_creds_db_uses_google_db_path_and_safe_fallback():
    source = Path("my-adk-agent/google_oauth/creds_db.py").read_text()

    assert '"GOOGLE_DB_PATH"' in source
    assert 'google_oauth_creds.db' in source
    assert "GOOGLE_CREDS_DB_PATH" not in source


def test_calendar_agent_prompt_requires_cal_newport_style_creation_summary():
    source = Path("my-adk-agent/agents/calendar_agent.py").read_text()

    assert "Cal Newport" in source
    assert "AI-generated summary" in source
    assert "events and tasks created" in source


def test_creds_db_resolves_codespaces_and_render_safe_database_paths():
    source = Path("my-adk-agent/google_oauth/creds_db.py").read_text()

    assert "def _resolve_db_path" in source
    assert "RENDER_DB_PATH" in source
    assert "LOCAL_DB_PATH" in source
    assert "GOOGLE_DB_PATH" in source
    assert "PermissionError" in source


def test_oauth_login_supports_env_or_repo_credentials_fallback_and_homepage_about_style():
    oauth_source = Path("my-adk-agent/google_oauth/oauth_login.py").read_text()
    home_source = Path("my-adk-agent/ui/home.py").read_text()

    assert "def _resolve_credentials_path" in oauth_source
    assert "GOOGLE_CREDENTIALS_PATH" in oauth_source
    assert "credentials.json" in oauth_source
    assert "WorkFlow" in home_source
    assert "About WorkFlow" in home_source
    assert "manage your schedules" in home_source
