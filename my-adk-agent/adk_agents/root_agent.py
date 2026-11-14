import os
import sys
from pathlib import Path
from typing import Literal, Optional, Union

from google.adk import Context, Event, Workflow
from google.adk.agents import LlmAgent
from google.adk.events import RequestInput
from google.adk.workflow import START, node
from pydantic import BaseModel, Field



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
from adk_agents.consultant_agent import consultant_agent
from adk_agents.workspace_agent import workspace_agent

#categorizes user input into categories for further processing
class RouteDecision(BaseModel):
    category: Literal["SCHEDULING", "CONSULTING", "WORKSPACE", "UNKNOWN"]
    confidence: float = Field(
        description="Probability score between 0.0 and 1.0 indicating confidence in this route."
    )


def save_user_prompt(callback_context) -> None:
    """before_agent_callback for router_agent: keeps the user's request in
    state["user_prompt"], because router_agent's own output is only the decision."""
    content = callback_context.user_content
    text = "".join(p.text for p in (content.parts or []) if p.text) if content else ""
    if text.strip():
        callback_context.state["user_prompt"] = text.strip()
    return None


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
    before_agent_callback=save_user_prompt,
)

# A SCHEDULING request below this goes to the consultant instead of straight to the calendar.
CONFIDENCE_THRESHOLD = 0.70
# A WORKSPACE / CONSULTING request below this also goes to the consultant (never a dead end).
ROUTE_CONFIDENCE_THRESHOLD = 0.75


def router(node_input: Union[RouteDecision, dict], ctx: Context) -> Event:
    """
    Evaluates the category and probability from router_agent (Jev)
    and emits an ADK Event(route=...) to branch the workflow.
    """
    request = ctx.state.get("user_prompt", "")
    if not node_input:
        return Event(route="UNKNOWN", output=request)

    decision = (
        node_input
        if isinstance(node_input, RouteDecision)
        else RouteDecision.model_validate(node_input)
    )
    ctx.state["last_route_decision"] = decision.model_dump()

    if decision.category == "UNKNOWN":
        return Event(route="UNKNOWN", output=request)

    # Requirement 1.11: Based on probability, decide between direct calendar_agent
    # scheduling vs. sending to consultant_agent to critique/build a plan first.
    if decision.category == "SCHEDULING" and decision.confidence >= CONFIDENCE_THRESHOLD:
        return Event(route="SCHEDULING", output=request)

    if decision.category == "WORKSPACE" and decision.confidence >= ROUTE_CONFIDENCE_THRESHOLD:
        return Event(route="WORKSPACE", output=request)

    # Study plans, and anything the router isn't sure about: consult/critique first.
    return Event(route="CONSULTING", output=request)


def reply_unknown(node_input: Optional[str] = None) -> str:
    """UNKNOWN route: ends the turn with a reply."""
    return (
        "I'm sorry, I couldn't determine the category of your request. "
        "Please provide more details or clarify your request."
    )


def reply_cancelled(node_input: Optional[str] = None) -> str:
    """The user declined the plan: ends the turn with a reply."""
    return "I understand. If you have any other requests or need assistance with something else, please let me know."


# --- Ask for the exam / due date before the consultant plans (AGENTS.md: "compare against the exam due date: ask")

class DeadlineAnswer(BaseModel):
    deadline: str = Field(
        default="",
        description="The exam or due date (e.g. 'Nov 24, 2026'), or empty if there isn't one.")


@node(rerun_on_resume=True)
async def ask_for_deadline(ctx: Context, node_input: Optional[str] = None):
    """Pauses for the exam / due date, then passes request + date to consultant_agent."""
    request = node_input or ctx.state.get("user_prompt", "")
    answer = ctx.resume_inputs.get("DEADLINE")
    if answer is None:
        yield RequestInput(
            interrupt_id="DEADLINE",
            message="When is the exam or due date for this? (Leave it blank if there isn't one.)",
            response_schema=DeadlineAnswer,
            payload={"request": request},
        )
        return

    deadline = DeadlineAnswer.model_validate(answer).deadline.strip()
    ctx.state["deadline"] = deadline
    yield Event(output=f"{request}\n\nExam / due date: {deadline or 'none given'}")


# --- The user reviews consultant_agent's plan before anything is scheduled

class EscalationDecision(BaseModel):
    """The user's answer to the plan review. The Runner puts it in ctx.resume_inputs["REVIEW_DECISION"]."""
    action: Literal["SCHEDULE", "REVISE", "CREATE_DOCS", "CANCEL"] = Field(
        description=(
            "SCHEDULE: put the plan on the calendar as shown. REVISE: send it back to the consultant "
            "with the note. CREATE_DOCS: have workspace_agent create the documents first. CANCEL: stop."))
    note: str = Field(
        default="",
        description="Optional note from the user: changes they want, or why they declined.")


def _plan_from(node_input, ctx: Context) -> dict:
    """consultant_agent's ConsultantPlan, from the node input or (on resume) from state."""
    plan = node_input if node_input else ctx.state.get("consultant_plan")
    if isinstance(plan, BaseModel):
        plan = plan.model_dump()
    if isinstance(plan, str):
        plan = {"summary": plan, "intentions": "", "milestones": []}
    return plan or {}


def _format_plan(plan: dict) -> str:
    lines = [plan.get("summary", "")]
    if plan.get("intentions"):
        lines += ["", f"Intentions: {plan['intentions']}"]
    milestones = plan.get("milestones") or []
    if milestones:
        lines += ["", "Milestones:"]
        lines += [
            f"{i}. {m.get('title')} ({m.get('kind', 'event')}, {m.get('date')})"
            + (f" - {m['description']}" if m.get("description") else "")
            for i, m in enumerate(milestones, 1)
        ]
    return "\n".join(lines).strip()


@node(rerun_on_resume=True)
async def escalation_to_human(ctx: Context, node_input: Union[dict, str, None] = None):
    """
    Shows consultant_agent's plan (summary + intentions + milestones) to the user and
    pauses. The Runner fills ctx.resume_inputs["REVIEW_DECISION"] from the user's reply
    to this RequestInput, then reruns this node, which routes on the user's choice.
    """
    plan = _plan_from(node_input, ctx)
    answer = ctx.resume_inputs.get("REVIEW_DECISION")
    if answer is None:
        yield RequestInput(
            interrupt_id="REVIEW_DECISION",
            message=(
                f"{_format_plan(plan)}\n\n"
                + (f"Before scheduling: {plan['open_questions']}\n\n" if plan.get("open_questions") else "")
                + "Would you like me to schedule this plan on your calendar, revise it, "
                  "create the documents for it first, or cancel?"
            ),
            response_schema=EscalationDecision,
            payload=plan,
        )
        return

    decision = EscalationDecision.model_validate(answer)
    ctx.state["review_decision"] = decision.model_dump()
    note = f"\n\nUser's note: {decision.note}" if decision.note else ""

    if decision.action == "SCHEDULE":
        yield Event(route="APPROVED", output=(
            "Schedule every milestone in this approved plan, one event or task per milestone, "
            "with its steps in the description.\n\n" + _format_plan(plan) + note))
    elif decision.action == "REVISE":
        yield Event(route="REVISE", output=(
            "Revise this plan based on the user's note.\n\n" + _format_plan(plan) + note))
    elif decision.action == "CREATE_DOCS":
        yield Event(route="CREATE_DOCS", output=(
            "Create the documents this plan needs (e.g. problem sets, study guides) and save them to "
            "the shared Drive folder.\n\n" + _format_plan(plan) + note))
    else:
        yield Event(route="DENIED", output=decision.note)


root_agent = Workflow(
    name="root_agent",
    description="Root agent that routes user requests to the appropriate sub-agent based on the request category.",
    edges=[
        (START, router_agent, router),

        (
            router,
            {
                "SCHEDULING": calendar_agent,
                "CONSULTING": ask_for_deadline,
                "WORKSPACE": workspace_agent,
                "UNKNOWN": reply_unknown,
            },
        ),

        # Chapter 10: ask for the exam date, then plan.
        (ask_for_deadline, consultant_agent),

        # Problem set / Document A: make or read the document, then plan.
        (workspace_agent, consultant_agent),

        # Every plan is reviewed by the user before anything is scheduled.
        (consultant_agent, escalation_to_human),

        (
            escalation_to_human,
            {
                "APPROVED": calendar_agent,
                "REVISE": consultant_agent,
                "CREATE_DOCS": workspace_agent,
                "DENIED": reply_cancelled,
            },
        ),
    ],
)
