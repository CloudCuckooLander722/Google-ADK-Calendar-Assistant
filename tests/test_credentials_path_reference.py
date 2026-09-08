from pathlib import Path


def test_oauth_login_uses_credentials_path_variable_only():
    source = Path("my-adk-agent/google_oauth/oauth_login.py").read_text()

    assert "CREDS_PATH" not in source
    assert "credentials_path = os.getenv(" in source
    assert "self.creds_path = credentials_path" in source


def test_google_oauth_creds_db_uses_google_db_path_and_safe_fallback():
    source = Path("my-adk-agent/google_oauth/creds_db.py").read_text()

    assert '"GOOGLE_DB_PATH"' in source
    assert 'google_oauth_creds.db' in source
    assert "GOOGLE_CREDS_DB_PATH" not in source
