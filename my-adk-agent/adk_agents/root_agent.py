import os
import sys
from google.adk import WorkFlow
from google.adk.integrations.typesafe import TypeSafeClassifier
from google.adk.labs.classifier import Choice, ClassifierNode
from google.adk.workflow import START

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

classifier = TypesafeClassifier(
    model="jev-latest",  # Reads TYPESAFE_API_KEY from environment by default
)

#decides which agent to route the request to based on Choice
ROUTING_QUESTION = Choice(
    instructions="Choose the best agent to handle the request.",
    criteria={
        "SCHEDULING": "The request is related to scheduling or calendar events.",
        "CONSULTING": "The request is related to consulting or advice.",
        "WORKSPACE_AGENT": "The request is related to workspace management or tools.",
    },
)

#fed in the router
typesafe_jev_router = ClassifierNode(
    name="typesafe_jev_router",
    description=(
        "Assesses the incoming prompt with Jev via TypesafeClassifier and "
        "routes to the selected model tier."
    ),
    classifier=classifier,
    question=ROUTING_QUESTION,
    answer_key="jev_routing",
)

root_agent = Workflow(
    name="adk_typesafe_model_router_workflow",
    description=(
        "ADK 2.0 Workflow that classifies prompts with TypeSafe's Jev decision "
        "model (TypesafeClassifier) and routes to the selected model tier."
    ),
    edges=[
        (
            START,
            typesafe_jev_router,
            {
                "SCHEDULING": calendar_agent, #work on schematics of this...
                "CONSULTING": consultant_agent,
                "WORKSPACE_AGENT": workspace_agent,
            },
        ),

        (workspace_agent, consultant_agent, {"next": "CONSULTING"}),

        (
            consultant_agent,
            {
                "REVISE": consultant_agent,
                "APPROVED": calendar_agent
            }
        )

    ],
)

