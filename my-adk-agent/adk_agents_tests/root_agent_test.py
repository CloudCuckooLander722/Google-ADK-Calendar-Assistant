"""
Autograder for adk_agents/root_agent.py (adk_agents/AGENTS.md section 1.1).

root_agent is an ADK 2.x Workflow: router_agent (in lieu of Jev) classifies
the request with a confidence, the router function turns that into a route,
and the graph sends it to calendar_agent, consultant_agent or workspace_agent,
with the user reviewing a plan before anything is scheduled.

Two kinds of tests:

  Static   Read root_agent.py without importing it, so they run on any ADK
           version and point at the exact line: names, imports, edges.
  Behaviour Import the module (needs google-adk 2.x) and check the router's
           routes and the graph's paths for each AGENTS.md test case:

    "Schedule a hangout with Kaoruko Waguri at the Cheesecake Factory."
        router -> calendar_agent
    "Make me a problem set for Calc I and schedule events"
        router -> workspace_agent -> consultant_agent -> review -> calendar_agent
    "I have a plan from Document A. Scan it ... and schedule events"
        ... -> consultant_agent -> review: schedule / revise (back to the
        consultant with the user's note) / cancel
    "I want to schedule a plan to study Chapter 10 test for Acc Geo / Alg 2 Trig"
        router -> ask for the exam date -> consultant_agent, optionally
        -> ask to create documents -> workspace_agent, then review -> calendar_agent

Nothing calls Gemini: the router function is called directly with the
decision router_agent would have made, and the graph is checked as data.

    cd my-adk-agent && ../.venv/bin/python -m pytest adk_agents_tests/root_agent_test.py -v
"""
import ast
import builtins
import importlib
import importlib.metadata
import os
import re
import sys
from collections import deque
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
MODULE_UNDER_TEST = "adk_agents.root_agent"
MODULE_PATH = APP_ROOT / "adk_agents" / "root_agent.py"

# Names each ADK 2.x module provides (google-adk 2.14.1).
ADK_EXPORTS = {
    "google.adk": {"Agent", "Context", "Event", "Runner", "Workflow"},
    "google.adk.workflow": {
        "BaseNode", "DEFAULT_ROUTE", "Edge", "FunctionNode", "JoinNode", "Node",
        "NodeTimeoutError", "RetryConfig", "START", "Workflow", "node",
    },
    "google.adk.events": {"Event", "EventActions", "RequestInput", "get_active_events"},
}

# --- Simulated requests (AGENTS.md 1.12) -----------------------------------------

HANGOUT = "Schedule a hangout with Kaoruko Waguri at the Cheesecake Factory."
PROBLEM_SET = "Make me a problem set for Calc I and schedule events"
DOCUMENT_A = ("I have plan from Document A. Scan it, list all of the plans, and then "
              "schedule events with steps in calendar description.")
CHAPTER_10 = "I want to schedule a plan to study Chapter 10 test for Acc Geo / Alg 2 Trig"
OFF_TOPIC = "What's the capital of France?"


# --- Reading the file -----------------------------------------------------------

def _source() -> str:
    return MODULE_PATH.read_text()


def _tree() -> ast.Module:
    try:
        return ast.parse(_source())
    except SyntaxError as error:
        pytest.fail(f"root_agent.py has a syntax error at line {error.lineno}: {error.msg}")


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
    """The Workflow(...) call that builds root_agent."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "root_agent" for t in node.targets):
            if isinstance(node.value, ast.Call):
                return node.value
    pytest.fail("root_agent.py must define root_agent = Workflow(...)")


def _edges_list(tree: ast.Module) -> ast.List:
    call = _workflow_call(tree)
    for keyword in call.keywords:
        if keyword.arg == "edges" and isinstance(keyword.value, ast.List):
            return keyword.value
    pytest.fail("root_agent = Workflow(...) must pass edges=[...]")


def _function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _uses(node: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node))


# --- Static: the file builds ------------------------------------------------------

def test_module_defines_every_name_it_uses():
    missing = _undefined_names(_tree())
    assert not missing, "uses names it never defines or imports: " + ", ".join(
        f"{name} (line {line})" for name, line in missing)


def test_imports_names_adk_actually_provides():
    """e.g. WorkFlow -> Workflow, RequestInterrupt -> RequestInput."""
    wrong = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and node.module in ADK_EXPORTS:
            for alias in node.names:
                if alias.name not in ADK_EXPORTS[node.module]:
                    wrong.append(f"{alias.name} from {node.module} (line {node.lineno})")
    assert not wrong, "ADK has no " + "; ".join(wrong)


def test_sub_agent_modules_exist():
    """Every `from adk_agents.X import Y` needs adk_agents/X.py defining Y."""
    missing = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("adk_agents."):
            path = APP_ROOT / (node.module.replace(".", "/") + ".py")
            if not path.exists():
                missing.append(f"{path.relative_to(APP_ROOT)} (line {node.lineno})")
                continue
            names = {n.id for t in ast.walk(ast.parse(path.read_text()))
                     if isinstance(t, ast.Assign) for n in t.targets if isinstance(n, ast.Name)}
            for alias in node.names:
                if alias.name not in names:
                    missing.append(f"{alias.name} in {path.relative_to(APP_ROOT)} (line {node.lineno})")
    assert not missing, "missing: " + ", ".join(missing)


def test_edges_are_separated_by_commas():
    """`(a, b)\\n(c, d)` without a comma between is a call: 'tuple' object is not callable."""
    calls = [n.lineno for n in ast.walk(_edges_list(_tree()))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Tuple)]
    assert not calls, f"missing comma after the edge ending on line(s) {calls}"


def test_route_targets_are_nodes_not_strings():
    """A route target must be an agent, tool or function; a string (other than 'START') can't run."""
    strings = [
        f"{key.value!r} (line {value.lineno})"
        for node in ast.walk(_edges_list(_tree())) if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value != "START"
    ]
    assert not strings, "make a function node that replies instead of a string for route(s): " + ", ".join(strings)


def test_no_start_node_argument():
    """Workflow has no start_node field (it's silently ignored); START in edges is the start."""
    assert not any(k.arg == "start_node" for k in _workflow_call(_tree()).keywords)


def test_the_users_request_is_saved_for_the_router():
    """The router forwards ctx.state['user_prompt']; something has to put the request there."""
    tree = _tree()
    writes = [
        n for n in ast.walk(tree)
        if (isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
            and isinstance(n.slice, ast.Constant) and n.slice.value == "user_prompt")
        or (isinstance(n, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "user_prompt" for k in n.keys))
        or (isinstance(n, ast.keyword) and n.arg == "output_key"
            and isinstance(n.value, ast.Constant) and n.value.value == "user_prompt")
    ]
    assert writes, "nothing saves the user's request as 'user_prompt', so every agent after the router gets ''"


def test_confidence_thresholds_are_named_constants():
    tree = _tree()
    bare = sorted({
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Compare)
        and any(isinstance(side, ast.Attribute) and side.attr == "confidence" for side in [n.left, *n.comparators])
        and any(isinstance(side, ast.Constant) and isinstance(side.value, float) for side in [n.left, *n.comparators])
    })
    assert not bare, f"compare confidence against a named constant, not a number (line(s) {bare})"


def test_requirements_pin_adk_2():
    pins = re.findall(r"^google-adk==(\d+)\.", (APP_ROOT / "requirements.txt").read_text(), re.M)
    assert pins and int(pins[0]) >= 2, (
        f"requirements.txt pins google-adk {pins[0] if pins else '?'}.x; Workflow, START, node and "
        "RequestInput need google-adk 2.x")


def test_the_app_runs_this_root_agent():
    """services/adk_service.py still runs agents/agent.py's root_agent (AGENTS.md wiring)."""
    service = ast.parse((APP_ROOT / "services" / "adk_service.py").read_text())
    modules = [n.module for n in ast.walk(service)
               if isinstance(n, ast.ImportFrom) and any(a.name == "root_agent" for a in n.names)]
    assert modules == ["adk_agents.root_agent"], f"adk_service imports root_agent from {modules}"


# --- Behaviour: importing the module (needs ADK 2.x) ----------------------------------

@pytest.fixture(scope="module")
def root():
    version = importlib.metadata.version("google-adk")
    if int(version.split(".")[0]) < 2:
        pytest.fail(f"google-adk {version} is installed; root_agent needs 2.x (Workflow, START, node, RequestInput)")
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
    """The router only needs ctx.state, after the request was saved as 'user_prompt'."""

    def __init__(self, request):
        self.state = {"user_prompt": request}


def _route(root, request, category, confidence):
    """What the router does with router_agent's decision for this request: (route, output)."""
    event = root.router({"category": category, "confidence": confidence}, FakeContext(request))
    actions = getattr(event, "actions", None)
    return getattr(actions, "route", None), getattr(event, "output", None)


# --- router (AGENTS.md 1.11: by probability, calendar or consultant) -------------------

def test_router_sends_a_ready_event_to_the_calendar(root):
    route, output = _route(root, HANGOUT, "SCHEDULING", 0.95)
    assert route == "SCHEDULING"
    assert output == HANGOUT, "the calendar agent must get the user's request"


def test_router_unsure_scheduling_goes_to_the_consultant(root):
    route, output = _route(root, HANGOUT, "SCHEDULING", 0.4)
    assert route == "CONSULTING" and output == HANGOUT


def test_router_sends_document_work_to_the_workspace(root):
    assert _route(root, PROBLEM_SET, "WORKSPACE", 0.9) == ("WORKSPACE", PROBLEM_SET)


def test_router_sends_study_plans_to_the_consultant(root):
    assert _route(root, CHAPTER_10, "CONSULTING", 0.9) == ("CONSULTING", CHAPTER_10)


@pytest.mark.parametrize("category, request_text", [("WORKSPACE", DOCUMENT_A), ("CONSULTING", CHAPTER_10)])
def test_router_unsure_requests_go_to_the_consultant_not_a_dead_end(root, category, request_text):
    """AGENTS.md 1.11: low probability means the consultant, not "I couldn't determine the category"."""
    route, output = _route(root, request_text, category, 0.5)
    assert route == "CONSULTING" and output == request_text


def test_router_off_topic_requests_are_unknown(root):
    assert _route(root, OFF_TOPIC, "UNKNOWN", 0.99)[0] == "UNKNOWN"


# --- The graph (AGENTS.md 1.12 test cases) ----------------------------------------------

def _graph(root):
    graph = getattr(root.root_agent, "graph", None)
    assert graph is not None, "root_agent has no compiled graph"
    return [(e.from_node.name, e.to_node.name, e.route) for e in graph.edges]


def _targets(edges, source, route=None):
    return {to for frm, to, r in edges if frm == source
            and (route is None or r == route or (isinstance(r, list) and route in r))}


def _path_exists(edges, start, goal, avoid=()):
    queue, seen = deque([start]), {start}
    while queue:
        name = queue.popleft()
        if name == goal:
            return True
        for nxt in _targets(edges, name):
            if nxt not in seen and nxt not in avoid:
                seen.add(nxt)
                queue.append(nxt)
    return False


def _review_nodes(root, edges):
    """Nodes (other than the router) that lead to the calendar: where the user reviews the plan."""
    calendar = root.calendar_agent.name
    return {frm for frm, to, _ in edges if to == calendar and frm != root.router.__name__}


def _yields_request_input(root, node_name):
    function = _function(_tree(), node_name)
    return function is not None and _uses(function, "RequestInput")


def test_graph_starts_with_the_router(root):
    edges = _graph(root)
    assert _targets(edges, "__START__") | _targets(edges, "START") == {root.router_agent.name}
    assert root.router.__name__ in _targets(edges, root.router_agent.name)


def test_hangout_goes_straight_to_the_calendar(root):
    """AGENTS.md: "Schedule a hangout..." routes to calendar_agent, which schedules it directly."""
    assert root.calendar_agent.name in _targets(_graph(root), root.router.__name__, "SCHEDULING")


def test_off_topic_gets_a_reply_node(root):
    edges = _graph(root)
    replies = _targets(edges, root.router.__name__, "UNKNOWN")
    assert replies, "the UNKNOWN route needs a node that replies"
    assert all(not _targets(edges, reply) for reply in replies), "the reply should end the turn"


def test_problem_set_goes_workspace_then_consultant_then_review_then_calendar(root):
    edges = _graph(root)
    workspace, consultant, calendar = root.workspace_agent.name, root.consultant_agent.name, root.calendar_agent.name
    assert workspace in _targets(edges, root.router.__name__, "WORKSPACE")
    assert _path_exists(edges, workspace, consultant, avoid={calendar}), "workspace_agent -> consultant_agent"
    reviews = _review_nodes(root, edges)
    assert reviews, "the plan must be reviewed by the user before calendar_agent schedules it"
    assert any(_path_exists(edges, consultant, review, avoid={calendar}) for review in reviews)


def test_review_asks_the_user(root):
    edges = _graph(root)
    for review in _review_nodes(root, edges):
        assert _yields_request_input(root, review), f"{review} should ask the user (yield RequestInput)"


def test_document_a_review_can_send_the_plan_back_for_revision(root):
    """AGENTS.md: "asks for revision or direct scheduling. if revision, then revised plan"."""
    edges = _graph(root)
    consultant = root.consultant_agent.name
    reviews = _review_nodes(root, edges)
    assert any(consultant in _targets(edges, review) for review in reviews), \
        "the review needs a 'revise' route back to consultant_agent (with the user's note)"


def test_document_a_review_can_cancel(root):
    edges = _graph(root)
    agents = {root.calendar_agent.name, root.consultant_agent.name, root.workspace_agent.name}
    for review in _review_nodes(root, edges):
        assert _targets(edges, review) - agents, f"{review} needs a 'cancel' route to a reply node"


def test_chapter_10_asks_for_the_exam_date_before_the_consultant(root):
    """AGENTS.md: "compare against the exam due date: ask"."""
    edges = _graph(root)
    consultant = root.consultant_agent.name
    first = _targets(edges, root.router.__name__, "CONSULTING")
    askers = [n for n in first if n != consultant and _yields_request_input(root, n)]
    assert askers, "the CONSULTING route should first ask for the exam date (a node that yields RequestInput)"
    assert all(_path_exists(edges, n, consultant) for n in askers)


def test_chapter_10_consultant_can_ask_to_create_documents(root):
    """AGENTS.md: "may prompt the user if its okay to ask workspace_agent to create documents"."""
    edges = _graph(root)
    consultant, workspace = root.consultant_agent.name, root.workspace_agent.name
    askers = [n for n in _targets(edges, consultant) if _yields_request_input(root, n)]
    assert any(workspace in _targets(edges, n) for n in askers), \
        "consultant_agent -> (ask permission) -> workspace_agent"
