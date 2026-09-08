from pathlib import Path


def test_oauth_login_uses_credentials_path_variable_only():
    source = Path("my-adk-agent/google_oauth/oauth_login.py").read_text()

    assert "CREDS_PATH" not in source
    assert "credentials_path = os.getenv(" in source
    assert "self.creds_path = credentials_path" in source
