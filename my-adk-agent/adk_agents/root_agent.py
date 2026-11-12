import os
import sys
from google.adk import WorkFlow
from typing import Literal
from google.adk.workflow import START
from pydantic import BaseModel
from google.adk.events import RequestInput
from google.adk.agents import LlmAgent


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    
    from google.genai import types
except Exception:
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)

    
    from google.genai import types

from adk_agents.calendar_agent import calendar_agent

#planned
from adk_agents.consultant_agent import consultant_agent
from adk_agents.workspace_agent import workspace_agent

#categorizes user input into categories for further processing
class RouteDecision(BaseModel):
    category: Literal["SCHEDULING", "CONSULTING", "WORKSPACE", "UNKNOWN"]
    confidence: float = Field(
        description="Probability score between 0.0 and 1.0 indicating confidence in this route."
    )

#classifier agent... in lieu of Jev
router_agent = LlmAgent(
    name="router_agent",
    model="gemini-2.5-flash",
    instruction=(
        "You are the Jev decision-making router. Classify the user request into "
        "'SCHEDULING', 'CONSULTING', 'WORKSPACE', or 'UNKNOWN' and output a probability confidence (0.0 to 1.0):\n"
        "- 'SCHEDULING': Direct, concrete calendar events ready to book immediately "
        "(e.g., 'Schedule an appointment with Dr. Chong', 'Schedule a hangout with Kaoruko Waguri at the Cheesecake Factory').\n"
        "- 'WORKSPACE': Requests that first need to generate a document/problem set in Drive OR scan an existing document "
        "(e.g., 'Make me a problem set for Calc I...', 'I have a plan from Document A. Scan it...').\n"
        "- 'CONSULTING': Requests to build a study plan, pace sessions before an exam date, or critique an existing plan "
        "(e.g., 'I want to schedule a plan to study Chapter 10 test for Acc Geo / Alg 2 Trig').\n"
        "- 'UNKNOWN': Anything unrelated to scheduling, planning, or workspace documents."
    ),
    output_schema=RouteDecision,
    output_key="router",
)

CONFIDENCE_THRESHOLD = 0.70

def router(node_input: Union[RouteDecision, dict], ctx: Context) -> Event:
    """
    Evaluates the category and probability from router_agent (Jev)
    and emits an ADK Event(route=...) to branch the workflow.
    """
    if not node_input:
        return Event(route="UNKNOWN")

    decision = (
        node_input
        if isinstance(node_input, RouteDecision)
        else RouteDecision.model_validate(node_input)
    )
    ctx.state["last_route_decision"] = decision.model_dump()

    # Requirement 1.11: Based on probability, decide between direct calendar_agent
    # scheduling vs. sending to consultant_agent to critique/build a plan first.
    if decision.category == "SCHEDULING":
        if decision.confidence >= CONFIDENCE_THRESHOLD:
            return Event(route="SCHEDULING", output=ctx.state.get("user_prompt", ""))
        else:
            # Low confidence that the event is ready to book -> consult/critique first
            return Event(route="CONSULTING", output=ctx.state.get("user_prompt", ""))

    elif decision.category == "WORKSPACE" and decision.confidence >= 0.75:
        return Event(route="WORKSPACE", output=ctx.state.get("user_prompt", ""))

    elif decision.category == "CONSULTING" and decision.confidence >= 0.75:
        return Event(route="CONSULTING", output=ctx.state.get("user_prompt", ""))

    return Event(route="UNKNOWN")

class EscalationDecision(BaseModel):
    approve: bool = Field(
        description="Indicates whether the request should be escalated to a human agent.")
    note: str = Field(
        description="Optional note from the agent explaining the decision to escalate or not.")

@node(rerun_on_resume=True)
async def escalation_to_human(ctx: Context, node_input: dict):
    answer = ctx.resume_inputs.get("REVIEW_DECISION")
    if answer is None:
        yield RequestInterrupt(
            interrupt_id="REVIEW_DECISION",
            message="Have the user review the request.",
            response_schema=EscalationDecision,
            payload=node_input,
        )
    else:
        yield Event(
            route="APPROVED" if answer.get("approve") else "DENIED",
            output=answer.get("note", ""),
        )



root_agent = WorkFlow(
    name="root_agent",
    description="Root agent that routes user requests to the appropriate sub-agent based on the request category.",
    start_node=START,
    edges=[
        (START, router_agent, router),

        (
            router,
            {
                "SCHEDULING": calendar_agent,
                "CONSULTING": consultant_agent,
                "WORKSPACE": workspace_agent,
                "UNKNOWN": "Response: I'm sorry, I couldn't determine the category of your request. Please provide more details or clarify your request."
            }
        ),

        (workspace_agent, consultation_agent),

        (escalation_to_human, {"APPROVED":
        calendar_agent,
        "DENIED": "Response: I understand. If you have any other requests or need assistance with something else, please let me know."}),

        
           
    ]
)