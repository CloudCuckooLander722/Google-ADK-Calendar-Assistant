import os
import sys
from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool
from google.adk.planners import BuiltInPlanner
from google.adk.runners import InMemoryRunner
from google.genai import types
from agents.tool_errors import report_tool_error
  

INSTRUCTION = """
"""


workspace_agent = LLmAgent(
    name="workspace_agent",
    description="The agent that creates Google Drive folders, necessary documents."
    instruction=INSTRUCTION,
    planner=BuiltInPlanner(thinking_config=types.ThinkingConfig(
                            include_thoughts=True,
                            thinking_budget=1024
                        )),
    tools=[],
    on_tool_error_callback=report_tool_error,
)

