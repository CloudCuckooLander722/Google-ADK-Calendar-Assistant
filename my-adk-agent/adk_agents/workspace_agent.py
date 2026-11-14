from google.adk.agents import LlmAgent
from google.genai import types
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.tool_errors import report_tool_error

# PLACEHOLDER: root_agent's WORKSPACE route needs this node to exist. It has no
# Google Drive tools yet (services/api_services.get_drive_service is there to build
# them on), so for now it only drafts the content and hands it to consultant_agent.
INSTRUCTION = """
You prepare study material and documents for a planning workflow.

- If the request asks you to make something (e.g. a problem set on derivatives for Calc I), draft it in full.
- If the request refers to an existing document or plan (e.g. "Document A"), list every plan, step, and date it contains from what the user provided.
- You cannot save to or read from Google Drive yet: say so in one line instead of claiming you saved or scanned anything.
- End with a short summary for the planning step: what was made or found, how much work it is, and any exam or due date mentioned.
"""

workspace_agent = LlmAgent(
    name="workspace_agent",
    model="gemini-2.5-flash",
    description="Drafts documents such as problem sets, and lists the plans in a document the user provides.",
    instruction=INSTRUCTION,
    generate_content_config=types.GenerateContentConfig(temperature=0.2),
    tools=[],
    on_tool_error_callback=report_tool_error,
)
