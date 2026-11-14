"""
Autograder for adk_agents/calendar_agent.py (adk_agents/AGENTS.md section 1.2).

calendar_agent is an ADK 2.x Workflow with the same layout as root_agent:
command_classifier (an LlmAgent with an output schema) sorts the request into a
category, the command_router function turns category + confidence into a route,
and each route runs an executor LlmAgent that holds the Google Calendar / Tasks
tools for that one command.

Three kinds of tests:

  Static     Read calendar_agent.py without importing it, so a broken file
             still gets a precise message: names, imports, edges.
  Behaviour  Import the module (needs google-adk 2.x) and check the router's
             routes, the graph, and which tools each executor holds. The
             create_event tool is run against a fake Calendar service for
             the AGENTS.md 1.22 test case. Nothing calls Gemini or Google.
  Live       Opt-in (RUN_LIVE_GEMINI=1, GEMINI_API_KEY in .env): send real
             requests through command_classifier and check the category.

    cd my-adk-agent && ../.venv/bin/python -m pytest adk_agents_tests/calendar_agent_test.py -v
    cd my-adk-agent && RUN_LIVE_GEMINI=1 ../.venv/bin/python -m pytest adk_agents_tests/calendar_agent_test.py -v -k live
"""
import ast
import asyncio
import builtins
import importlib
import importlib.metadata
import os
import sys
import typing
import uuid
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
MODULE_UNDER_TEST = "adk_agents.calendar_agent"
MODULE_PATH = APP_ROOT / "adk_agents" / "calendar_agent.py"

# Names each ADK 2.x module provides (google-adk 2.14.1).
ADK_EXPORTS = {
    "google.adk": {"Agent", "Context", "Event", "Runner", "Workflow"},
    "google.adk.workflow": {
        "BaseNode", "DEFAULT_ROUTE", "Edge", "FunctionNode", "JoinNode", "Node",
        "NodeTimeoutError", "RetryConfig", "START", "Workflow", "node",
    },
    "google.adk.events": {"Event", "EventActions", "RequestInput", "get_active_events"},
}

# AGENTS.md 1.21: the categories requests must be sorted into.
REQUIRED_CATEGORIES = {
    "SCHEDULE_EVENT", "DELETE_EVENT", "SCHEDULE_TASK", "DELETE_TASK", "LIST_TASKS", "LIST_EVENTS",
}

# For each category: tools its executor must hold to carry the command out,
# and tools it must not hold (a list request must never be able to delete).
WRITE_TOOLS = {
    "create_event", "update_event", "delete_event", "create_task", "patch_task", "delete_task",
}
EXECUTOR_TOOLS = {
    "SCHEDULE_EVENT": {"create_event", "parse_natural_language_datetime"},
    "UPDATE_EVENT": {"search_events", "update_event", "parse_natural_language_datetime"},
    "DELETE_EVENT": {"search_events", "delete_event"},
    "LIST_EVENTS": {"search_events", "list_events"},
    "SCHEDULE_TASK": {"create_task", "parse_natural_language_datetime"},
    "UPDATE_TASK": {"search_tasks", "patch_task"},
    "DELETE_TASK": {"search_tasks", "delete_task"},
    "LIST_TASKS": {"search_tasks"},
}
ALLOWED_WRITES = {
    "SCHEDULE_EVENT": {"create_event"},
    "UPDATE_EVENT": {"update_event"},
    "DELETE_EVENT": {"delete_event"},
    "LIST_EVENTS": set(),
    "SCHEDULE_TASK": {"create_task"},
    "UPDATE_TASK": {"patch_task"},
    "DELETE_TASK": {"delete_task"},
    "LIST_TASKS": set(),
}

# --- Simulated requests ------------------------------------------------------------

DR_CHONG = "Request to schedule an appointment with Dr. Chong from 3:20 PM - 4:00 PM"
BARBECUE = "Schedule a barbecue for Walter White on Saturday at noon"
REQUESTS = {
    "SCHEDULE_EVENT": DR_CHONG,
    "UPDATE_EVENT": "Move my dentist appointment on Friday to 4 PM",
    "DELETE_EVENT": "Cancel the barbecue for Walter White on Saturday",
    "LIST_EVENTS": "What's on my calendar tomorrow?",
    "SCHEDULE_TASK": "Add a task to finish PIQ 3 by October 21",
    "UPDATE_TASK": "Mark the 'buy milk' task as done",
    "DELETE_TASK": "Delete the task 'buy milk'",
    "LIST_TASKS": "Show me my to-do list",
}
OFF_TOPIC = "What's the capital of France?"


# --- Reading the file -----------------------------------------------------------

def _source() -> str:
    return MODULE_PATH.read_text()


def _tree() -> ast.Module:
    try:
        return ast.parse(_source())
    except SyntaxError as error:
        pytest.fail(f"calendar_agent.py has a syntax error at line {error.lineno}: {error.msg}")


def _undefined_names(tree: ast.Module) -> list[tuple[str, int]]:
    """Names the module loads but never binds (imports, defs, assignments, params)."""
    defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__package__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            defined.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
    seen = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in defined:
            seen.setdefault(node.id, node.lineno)
    return sorted(seen.items(), key=lambda item: item[1])


def _workflow_call(tree: ast.Module) -> ast.Call:
    """The Workflow(...) call that builds calendar_agent."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "calendar_agent" for t in node.targets):
            if isinstance(node.value, ast.Call) and getattr(node.value.func, "id", None) == "Workflow":
                return node.value
    pytest.fail("calendar_agent.py must define calendar_agent = Workflow(...), the same layout as root_agent")


def _edges_list(tree: ast.Module) -> ast.List:
    for keyword in _workflow_call(tree).keywords:
        if keyword.arg == "edges" and isinstance(keyword.value, ast.List):
            return keyword.value
    pytest.fail("calendar_agent = Workflow(...) must pass edges=[...]")


# --- Static: the file builds ------------------------------------------------------

def test_module_defines_every_name_it_uses():
    missing = _undefined_names(_tree())
    assert not missing, "uses names it never defines or imports: " + ", ".join(
        f"{name} (line {line})" for name, line in missing)


def test_imports_names_adk_actually_provides():
    wrong = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and node.module in ADK_EXPORTS:
            for alias in node.names:
                if alias.name not in ADK_EXPORTS[node.module]:
                    wrong.append(f"{alias.name} from {node.module} (line {node.lineno})")
    assert not wrong, "ADK has no " + "; ".join(wrong)


def test_imported_tools_exist():
    """Every `from tools.X import Y` / `from agents.X import Y` needs Y defined in that file."""
    missing = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in {"tools", "agents"}:
            path = APP_ROOT / (node.module.replace(".", "/") + ".py")
            if not path.exists():
                missing.append(f"{path.relative_to(APP_ROOT)} (line {node.lineno})")
                continue
            defined = {n.name for n in ast.parse(path.read_text()).body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            missing += [f"{a.name} in {path.relative_to(APP_ROOT)} (line {node.lineno})"
                        for a in node.names if a.name not in defined]
    assert not missing, "missing: " + ", ".join(missing)


def test_edges_are_separated_by_commas():
    """`(a, b)\\n(c, d)` without a comma between is a call: 'tuple' object is not callable."""
    calls = [n.lineno for n in ast.walk(_edges_list(_tree()))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Tuple)]
    assert not calls, f"missing comma after the edge ending on line(s) {calls}"


def test_route_targets_are_nodes_not_strings():
    strings = [
        f"{key.value!r} (line {value.lineno})"
        for node in ast.walk(_edges_list(_tree())) if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value != "START"
    ]
    assert not strings, "make a function node that replies instead of a string for route(s): " + ", ".join(strings)


def test_no_start_node_argument():
    assert not any(k.arg == "start_node" for k in _workflow_call(_tree()).keywords)


def test_confidence_threshold_is_a_named_constant():
    bare = sorted({
        n.lineno for n in ast.walk(_tree())
        if isinstance(n, ast.Compare)
        and any(isinstance(side, ast.Attribute) and side.attr == "confidence" for side in [n.left, *n.comparators])
        and any(isinstance(side, ast.Constant) and isinstance(side.value, float) for side in [n.left, *n.comparators])
    })
    assert not bare, f"compare confidence against a named constant, not a number (line(s) {bare})"


# --- Behaviour: importing the module (needs ADK 2.x) ----------------------------------

@pytest.fixture(scope="module")
def cal():
    version = importlib.metadata.version("google-adk")
    if int(version.split(".")[0]) < 2:
        pytest.fail(f"google-adk {version} is installed; calendar_agent needs 2.x (Workflow, START)")
    os.environ.setdefault("ENVIRONMENT", "development")
    if not os.environ.get("GOOGLE_CREDS_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet
        os.environ["GOOGLE_CREDS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    try:
        return importlib.import_module(MODULE_UNDER_TEST)
    except SyntaxError as error:
        pytest.fail(f"syntax error at line {error.lineno}: {error.msg}")
    except Exception as error:
        pytest.fail(f"{MODULE_UNDER_TEST} does not import: {type(error).__name__}: {error}")


class FakeContext:
    """The router and the clarification node only need ctx.state."""

    def __init__(self, state=None):
        self.state = dict(state or {})


def _route(cal, category, confidence, request="", missing_info=None, ctx=None):
    """What command_router does with the classifier's output: (route, output)."""
    command = {"category": category, "confidence": confidence, "request": request}
    if missing_info is not None:
        command["missing_info"] = missing_info
    event = cal.command_router(command, ctx or FakeContext())
    return getattr(event.actions, "route", None), event.output


def _graph(cal):
    graph = getattr(cal.calendar_agent, "graph", None)
    assert graph is not None, "calendar_agent has no compiled graph"
    return [(e.from_node, e.to_node, e.route) for e in graph.edges]


def _route_target(cal, route):
    """The node the router sends `route` to (exactly one)."""
    targets = [to for frm, to, r in _graph(cal)
               if frm.name == cal.command_router.__name__ and (r == route or (isinstance(r, list) and route in r))]
    assert len(targets) == 1, f"route {route!r} should lead to exactly one node, found {[t.name for t in targets]}"
    return targets[0]


def _executor(cal, category):
    """The LlmAgent behind a category's route."""
    node = _route_target(cal, category)
    agent = getattr(node, "agent", node)  # LlmAgents are wrapped as nodes in some ADK builds
    assert hasattr(agent, "tools"), f"route {category!r} should run an LlmAgent with tools, got {type(node).__name__}"
    return agent


def _tool_names(agent):
    return {getattr(tool, "__name__", None) or getattr(tool, "name", None) for tool in agent.tools}


# --- The classifier (AGENTS.md 1.21: sort requests into categories) -------------------

def test_classifier_has_every_required_category(cal):
    schema = cal.command_classifier.output_schema
    assert schema is not None, "command_classifier needs an output_schema so the router gets a category"
    categories = set(typing.get_args(schema.model_fields["category"].annotation))
    assert REQUIRED_CATEGORIES <= categories, f"missing categories: {sorted(REQUIRED_CATEGORIES - categories)}"
    assert "UNKNOWN" in categories, "requests that aren't calendar commands need an UNKNOWN category"


def test_classifier_returns_a_confidence_and_the_full_request(cal):
    fields = cal.command_classifier.output_schema.model_fields
    assert "confidence" in fields, "the router decides by probability, so the classifier must return one"
    assert "request" in fields, "the executors need the request restated with all its details"


def test_classifier_instruction_names_every_category(cal):
    instruction = cal.command_classifier.instruction
    missing = [c for c in REQUIRED_CATEGORIES if c not in instruction]
    assert not missing, f"the classifier's instruction never explains {missing}"


# --- The router -------------------------------------------------------------------

@pytest.mark.parametrize("category", sorted(EXECUTOR_TOOLS))
def test_router_sends_each_category_to_its_executor(cal, category):
    route, output = _route(cal, category, 0.95, REQUESTS[category])
    assert route == category
    assert output == REQUESTS[category], "the executor must get the restated request"


def test_router_accepts_the_schema_object_as_well_as_a_dict(cal):
    command = cal.CalendarCommand(category="SCHEDULE_EVENT", confidence=0.9, request=BARBECUE)
    event = cal.command_router(command, FakeContext())
    assert (event.actions.route, event.output) == ("SCHEDULE_EVENT", BARBECUE)


def test_router_low_confidence_asks_instead_of_acting(cal):
    """A guess must never delete or create anything."""
    route, _ = _route(cal, "DELETE_EVENT", cal.CONFIDENCE_THRESHOLD - 0.01, REQUESTS["DELETE_EVENT"])
    assert route == "UNKNOWN"


def test_router_threshold_is_inclusive(cal):
    route, _ = _route(cal, "LIST_TASKS", cal.CONFIDENCE_THRESHOLD, REQUESTS["LIST_TASKS"])
    assert route == "LIST_TASKS"


def test_router_off_topic_is_unknown(cal):
    assert _route(cal, "UNKNOWN", 0.99, OFF_TOPIC)[0] == "UNKNOWN"


def test_router_empty_input_is_unknown(cal):
    event = cal.command_router({}, FakeContext())
    assert event.actions.route == "UNKNOWN"


def test_router_saves_the_decision_in_state(cal):
    ctx = FakeContext()
    _route(cal, "SCHEDULE_EVENT", 0.9, DR_CHONG, ctx=ctx)
    saved = ctx.state.get("last_calendar_command")
    assert saved and saved["category"] == "SCHEDULE_EVENT" and saved["request"] == DR_CHONG


def test_clarification_names_what_is_missing(cal):
    ctx = FakeContext()
    _route(cal, "SCHEDULE_EVENT", 0.3, "Schedule lunch with Sam", missing_info="no date given", ctx=ctx)
    reply = cal.ask_for_clarification(None, ctx)
    assert "no date given" in reply


def test_clarification_without_details_lists_what_it_can_do(cal):
    reply = cal.ask_for_clarification(None, FakeContext())
    assert reply and all(word in reply.lower() for word in ("schedule", "delete", "list"))


# --- The graph --------------------------------------------------------------------

def test_graph_starts_with_the_classifier_then_the_router(cal):
    edges = _graph(cal)
    starts = {to.name for frm, to, _ in edges if frm.name in {"__START__", "START"}}
    assert starts == {cal.command_classifier.name}
    after = {to.name for frm, to, _ in edges if frm.name == cal.command_classifier.name}
    assert after == {cal.command_router.__name__}


@pytest.mark.parametrize("category", sorted(EXECUTOR_TOOLS))
def test_each_category_has_its_own_executor(cal, category):
    _executor(cal, category)


def test_executors_are_all_different(cal):
    names = [_route_target(cal, c).name for c in EXECUTOR_TOOLS]
    assert len(set(names)) == len(names), f"two categories share an executor: {names}"


def test_unknown_route_replies_and_ends(cal):
    reply = _route_target(cal, "UNKNOWN")
    assert reply.name == cal.ask_for_clarification.__name__
    assert not [to for frm, to, _ in _graph(cal) if frm.name == reply.name], "the reply should end the turn"


def test_executors_end_the_workflow(cal):
    """calendar_agent's last node is the executor, so root_agent gets its reply."""
    executors = {_route_target(cal, c).name for c in EXECUTOR_TOOLS}
    leaving = {frm.name for frm, _, _ in _graph(cal)} & executors
    assert not leaving, f"executors should be terminal nodes: {sorted(leaving)}"


# --- The executors' tools (AGENTS.md 1.21: functions execute the commands) ------------

@pytest.mark.parametrize("category", sorted(EXECUTOR_TOOLS))
def test_executor_has_the_tools_to_carry_out_its_command(cal, category):
    have = _tool_names(_executor(cal, category))
    missing = EXECUTOR_TOOLS[category] - have
    assert not missing, f"{category} executor is missing {sorted(missing)}"


@pytest.mark.parametrize("category", sorted(EXECUTOR_TOOLS))
def test_executor_cannot_make_other_kinds_of_changes(cal, category):
    """e.g. a LIST_EVENTS request can never reach delete_event."""
    extra = (_tool_names(_executor(cal, category)) & WRITE_TOOLS) - ALLOWED_WRITES[category]
    assert not extra, f"{category} executor should not hold {sorted(extra)}"


@pytest.mark.parametrize("category", sorted(EXECUTOR_TOOLS))
def test_executor_turns_tool_errors_into_results(cal, category):
    from agents.tool_errors import report_tool_error
    assert _executor(cal, category).on_tool_error_callback is report_tool_error


@pytest.mark.parametrize("category", ["DELETE_EVENT", "DELETE_TASK"])
def test_delete_executors_search_before_deleting_and_ask_when_unsure(cal, category):
    instruction = _executor(cal, category).instruction.lower()
    assert "exactly one" in instruction and "ask" in instruction, \
        f"{category} should delete only a single clear match and ask otherwise"


# --- AGENTS.md 1.22: Dr. Chong, 3:20 PM - 4:00 PM ------------------------------------

def test_dr_chong_routes_to_the_event_scheduler(cal):
    route, output = _route(cal, "SCHEDULE_EVENT", 0.95, DR_CHONG)
    assert _route_target(cal, route) is _route_target(cal, "SCHEDULE_EVENT")
    assert "Dr. Chong" in output and "3:20 PM" in output and "4:00 PM" in output


def test_scheduler_is_told_to_add_additional_information(cal):
    """'along with additional information': the event gets a description even if the user gave none."""
    instruction = _executor(cal, "SCHEDULE_EVENT").instruction
    assert "description" in instruction.lower()
    assert "resolve_address" in instruction, "place names should become real addresses"


class FakeCalendarService:
    """Records the body create_event sends to events().insert()."""

    def __init__(self):
        self.inserted = []

    def events(self):
        return self

    def insert(self, calendarId, body):
        self.inserted.append((calendarId, body))
        return self

    def execute(self):
        return {"htmlLink": "https://calendar.google.com/event?eid=fake"}


def test_dr_chong_create_event_sends_the_exact_times_and_description(cal, monkeypatch):
    """Runs the scheduler's create_event tool the way the model would call it, against a fake service."""
    import tools.calendar_tools as calendar_tools

    service = FakeCalendarService()
    monkeypatch.setattr(calendar_tools, "_calendar_service", lambda: service)
    monkeypatch.setattr(calendar_tools, "get_user_timezone", lambda: "America/Los_Angeles")

    create_event = next(t for t in _executor(cal, "SCHEDULE_EVENT").tools if getattr(t, "__name__", "") == "create_event")
    result = create_event(
        summary="Appointment with Dr. Chong",
        start_datetime="2026-11-16T15:20:00",
        end_datetime="2026-11-16T16:00:00",
        description="Appointment with Dr. Chong. Bring your insurance card and a list of questions.",
    )

    assert "Event created" in result
    calendar_id, body = service.inserted[0]
    assert calendar_id == "primary"
    assert body["summary"] == "Appointment with Dr. Chong"
    assert "15:20" in body["start"]["dateTime"] and "16:00" in body["end"]["dateTime"]
    assert body["description"], "the event should carry additional information"


# --- Live: the real classifier (opt-in) -----------------------------------------------

LIVE = pytest.mark.skipif(os.environ.get("RUN_LIVE_GEMINI") != "1",
                          reason="set RUN_LIVE_GEMINI=1 (and GEMINI_API_KEY) to call Gemini")


async def _classify(cal, text):
    from google.adk import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    app, user, session_id = "calendar_agent_test", "test", uuid.uuid4().hex
    sessions = InMemorySessionService()
    await sessions.create_session(app_name=app, user_id=user, session_id=session_id)
    runner = Runner(agent=cal.command_classifier, app_name=app, session_service=sessions)
    message = types.Content(role="user", parts=[types.Part(text=text)])
    async for _ in runner.run_async(user_id=user, session_id=session_id, new_message=message):
        pass
    session = await sessions.get_session(app_name=app, user_id=user, session_id=session_id)
    return session.state.get("calendar_command")


@pytest.fixture(scope="module")
def live_cal(cal):
    from dotenv import load_dotenv
    load_dotenv(APP_ROOT / ".env")
    if os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]
    if not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("no GEMINI_API_KEY / GOOGLE_API_KEY")
    return cal


@LIVE
@pytest.mark.parametrize("category", sorted(EXECUTOR_TOOLS))
def test_live_classifier_sorts_each_request(live_cal, category):
    command = asyncio.run(_classify(live_cal, REQUESTS[category]))
    assert command, "the classifier returned nothing"
    assert command["category"] == category, f"{REQUESTS[category]!r} -> {command}"
    assert command["confidence"] >= live_cal.CONFIDENCE_THRESHOLD


@LIVE
def test_live_dr_chong_keeps_the_times(live_cal):
    command = asyncio.run(_classify(live_cal, DR_CHONG))
    assert command["category"] == "SCHEDULE_EVENT"
    assert "chong" in command["request"].lower()
    assert "3:20" in command["request"] and "4:00" in command["request"]


@LIVE
def test_live_off_topic_is_unknown(live_cal):
    command = asyncio.run(_classify(live_cal, OFF_TOPIC))
    assert command["category"] == "UNKNOWN" or command["confidence"] < live_cal.CONFIDENCE_THRESHOLD
