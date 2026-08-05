from google.adk.agents import LlmAgent
from google.genai import types
from google.adk.tools import google_maps_grounding, AgentTool

google_maps_agent = LlmAgent(
    name="google_maps_agent",
    description="An agent that can provide information about addresses.",
    instruction="You are a helpful assistant that provides information about addresses.",
    tools=[google_maps_grounding]

)

google_maps_tool = AgentTool(google_maps_agent)
