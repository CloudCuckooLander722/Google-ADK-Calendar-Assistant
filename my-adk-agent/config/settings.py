import os
import logging
from dotenv import load_dotenv
load_dotenv() # Load environment variables from a .env file. This is crucial for keeping sensitive data like API keys out of your main codebase.
# Default to INFO so app logs reach Cloud Logging on Cloud Run; override with LOG_LEVEL.
# Noisy third-party libraries are turned down separately below.
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
for _noisy_logger in ("google", "google_genai", "google_adk", "urllib3", "httpx", "httpcore"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)
MODEL_GEMINI = "gemini-2.0-flash" # Specifies the Google Gemini model to be used by the ADK agent.
APP_NAME_FOR_ADK = "greeting_app" # A unique name for your application within ADK, used for session management.
USER_ID = "ketanraj" # A default user ID. In a real application, this would be dynamic (e.g., from a login system).
# Defines the initial state for new ADK sessions. This provides default values for user information.
INITIAL_STATE = {
    "user_name": "Ketan Raj",
    "user_hobbies": "Coding, Reading, Gaming",
    "user_interests": "AI, Technology, Open Source"
}
MESSAGE_HISTORY_KEY = "messages_final_mem_v2" # Key used by Streamlit to store the chat history in its session state.
ADK_SESSION_KEY = "adk_session_id" # Key used by Streamlit to store the unique ADK session ID.
def get_api_key():
    """Retrieves the Google API Key from environment variables."""
    api_key = os.environ.get("GEMINI_API_KEY")
    # Basic check to ensure the key is present and not the placeholder.
    if not api_key or "YOUR_GOOGLE_API_KEY" in api_key:
        return None
    return api_key