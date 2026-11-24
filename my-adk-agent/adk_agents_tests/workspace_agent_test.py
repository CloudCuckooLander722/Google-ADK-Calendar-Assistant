"""
Autograder for adk_agents/workspace_agent.py (adk_agents/AGENTS.md section 1.3).

workspace_agent is an ADK 2.x Workflow with the same layout as calendar_agent:
router_agent (an LlmAgent with output_schema WorkspaceCommand) sorts the request
into DOCUMENT_CREATION, FOLDER_CREATION, FOLDER_DELETION or DOCUMENT_MOVEMENT,
a router function turns category + confidence into a route, and each route runs
the agents / tools that carry the command out on Google Drive.

What AGENTS.md 1.32 asks for, and where it is checked:

  Quiz on related rates     DOCUMENT_CREATION: generate the problems, ask for an
    (Google Doc)            answer key / human confirmation before anything is
                            written, find the matching Drive folder by similarity
                            search, create the folder if none matches, put the Doc in it.
  Report -> slide deck      Same, plus reading the source document first.
  PDF or infographic        Same, as a PDF.
  Move PDF to "Atlantic     DOCUMENT_MOVEMENT: similarity search for the folder,
    Salmon Research..."     create it if missing, move the file.

Tools are matched by what they do, not by exact name (see CAPABILITIES), so
search_drive_folders / find_similar_folder / match_folder all count as a folder
search. Rename the patterns if your tools use other words.

Three kinds of tests:

  Static     Read workspace_agent.py without importing it: names, imports, edges,
             definition order, pydantic config.
  Behaviour  Import the module (needs google-adk 2.x): the schema, the router's
             routes, the graph, which tools each route can reach, and that every
             path to a Drive write goes through a human confirmation.
  Live       Opt-in (RUN_LIVE_GEMINI=1, GEMINI_API_KEY in .env): the AGENTS.md
             requests through the real router_agent.

    cd my-adk-agent && ../.venv/bin/python -m pytest adk_agents_tests/workspace_agent_test.py -v
    cd my-adk-agent && RUN_LIVE_GEMINI=1 ../.venv/bin/python -m pytest adk_agents_tests/workspace_agent_test.py -v -k live
"""
import ast
import asyncio
import builtins
import importlib
import importlib.metadata
import os
import re
import sys
import typing
import uuid
from collections import deque
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
MODULE_UNDER_TEST = "adk_agents.workspace_agent"
MODULE_PATH = APP_ROOT / "adk_agents" / "workspace_agent.py"

# Names each ADK 2.x module provides (google-adk 2.14.1).
ADK_EXPORTS = {
    "google.adk": {"Agent", "Context", "Event", "Runner", "Workflow"},
    "google.adk.workflow": {
        "BaseNode", "DEFAULT_ROUTE", "Edge", "FunctionNode", "JoinNode", "Node",
        "NodeTimeoutError", "RetryConfig", "START", "Workflow", "node",
    },
    "google.adk.events": {"Event", "EventActions", "RequestInput", "get_active_events"},
}

# AGENTS.md 1.31 (it writes "FOLDER CREATION" / "FOLDER DELETION"; route values use underscores).
REQUIRED_CATEGORIES = {"DOCUMENT_CREATION", "FOLDER_CREATION", "FOLDER_DELETION", "DOCUMENT_MOVEMENT"}

# What a tool does, by its name.
CAPABILITIES = {
    "folder_search": r"(search|find|match|similar|lookup|list).*folder|folder.*(search|find|match|similar|lookup)",
    "create_folder": r"(create|make|new).*folder",
    "delete_folder": r"(delete|remove|trash).*folder",
    "move_file": r"move",
    "create_doc": r"(create|make|new|upload|write).*(doc|file)",
    "create_slides": r"(create|make|new|upload|write).*(slide|presentation|deck|file)",
    "create_pdf": r"(create|make|new|upload|write|export).*(pdf|file)",
    "read_document": r"(read|get|export|download|fetch|open|scan).*(doc|file|content|report)",
}
# Tools that change Drive. Anything else (search, read) is read-only.
WRITE_CAPABILITIES = ("create_folder", "delete_folder", "move_file", "create_doc", "create_slides", "create_pdf")

# What each route must be able to reach (AGENTS.md 1.32).
ROUTE_NEEDS = {
    "DOCUMENT_CREATION": {"folder_search", "create_folder", "create_doc", "create_slides", "create_pdf", "read_document"},
    "FOLDER_CREATION": {"create_folder"},
    "FOLDER_DELETION": {"folder_search", "delete_folder"},
    "DOCUMENT_MOVEMENT": {"folder_search", "create_folder", "move_file"},
}
# Drive writes each route may never reach (e.g. moving a file must not be able to delete a folder).
ROUTE_FORBIDS = {
    "DOCUMENT_CREATION": {"delete_folder", "move_file"},
    "FOLDER_CREATION": {"delete_folder", "move_file"},
    "FOLDER_DELETION": {"create_doc", "create_slides", "create_pdf", "move_file"},
    "DOCUMENT_MOVEMENT": {"delete_folder"},
}

# --- Simulated requests (AGENTS.md 1.32) ------------------------------------------

QUIZ = "I want a quiz on curveball related rates problems that test my concepts"
SLIDES = "Summarize this report into a slidedeck."
PDF = "Create a PDF for me or an infographic"
MOVE = 'Move this PDF to drive folder "Atlantic Salmon Research Project"'
REQUESTS = {
    "DOCUMENT_CREATION": QUIZ,
    "FOLDER_CREATION": "Make a new Drive folder called Chem Labs",
    "FOLDER_DELETION": "Delete my Drive folder called Old Drafts",
    "DOCUMENT_MOVEMENT": MOVE,
}
CALENDAR_REQUEST = "Schedule an appointment with Dr. Chong from 3:20 PM - 4:00 PM"


# --- Reading the file -----------------------------------------------------------

def _source() -> str:
    return MODULE_PATH.read_text()


def _tree() -> ast.Module:
    try:
        return ast.parse(_source())
    except SyntaxError as error:
        pytest.fail(f"workspace_agent.py has a syntax error at line {error.lineno}: {error.msg}\n"
                    f"    {(error.text or '').strip()}\n"
                    "A route value is one node. To run two nodes in sequence, route to the first and add "
                    "its own edge: \"ROUTE\": first_node, then (first_node, second_node). "
                    "(A tuple value, \"ROUTE\": (a, b), runs both at once.)")


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


def _workflow_assign(tree: ast.Module) -> ast.Assign:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "workspace_agent" for t in node.targets):
            if isinstance(node.value, ast.Call) and getattr(node.value.func, "id", None) == "Workflow":
                return node
    pytest.fail("workspace_agent.py must define workspace_agent = Workflow(...), the same layout as calendar_agent")


def _edges_list(tree: ast.Module) -> ast.List:
    for keyword in _workflow_assign(tree).value.keywords:
        if keyword.arg == "edges" and isinstance(keyword.value, ast.List):
            return keyword.value
    pytest.fail("workspace_agent = Workflow(...) must pass edges=[...]")


def _function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _yields_request_input(name: str) -> bool:
    function = _function(_tree(), name)
    return function is not None and any(isinstance(n, ast.Name) and n.id == "RequestInput" for n in ast.walk(function))


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


def test_nodes_are_defined_before_the_workflow():
    """Module code runs top to bottom: an agent defined below `workspace_agent = Workflow(...)` is a NameError."""
    tree = _tree()
    workflow_line = _workflow_assign(tree).lineno
    defined_at = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined_at.setdefault(t.id, node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined_at.setdefault(node.name, node.lineno)
    late = sorted({f"{n.id} (defined on line {defined_at[n.id]})"
                   for n in ast.walk(_edges_list(tree))
                   if isinstance(n, ast.Name) and defined_at.get(n.id, 0) > workflow_line})
    assert not late, f"move these above line {workflow_line}: " + ", ".join(late)


def test_edges_are_separated_by_commas():
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
def ws():
    version = importlib.metadata.version("google-adk")
    if int(version.split(".")[0]) < 2:
        pytest.fail(f"google-adk {version} is installed; workspace_agent needs 2.x (Workflow, START)")
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
    """The router and reply nodes only need ctx.state."""

    def __init__(self):
        self.state = {}


def _router_name(ws) -> str:
    """The function node right after router_agent (the edges call it `router`)."""
    after = [to for frm, to, _ in _edges(ws) if frm.name == ws.router_agent.name]
    assert len(after) == 1, f"router_agent should lead to one router function, found {[n.name for n in after]}"
    return after[0].name


def _router_fn(ws):
    fn = getattr(ws, _router_name(ws), None)
    assert callable(fn), f"workspace_agent.py should define the router function {_router_name(ws)!r}"
    return fn


def _route(ws, category, confidence, request="", missing_info=None, ctx=None):
    command = {"category": category, "confidence": confidence, "request": request}
    if missing_info is not None:
        command["missing_info"] = missing_info
    event = _router_fn(ws)(command, ctx or FakeContext())
    return getattr(event.actions, "route", None), event.output


def _edges(ws):
    graph = getattr(ws.workspace_agent, "graph", None)
    assert graph is not None, "workspace_agent has no compiled graph"
    return [(e.from_node, e.to_node, e.route) for e in graph.edges]


def _targets(ws, source_name, route=None):
    return [to for frm, to, r in _edges(ws) if frm.name == source_name
            and (route is None or r == route or (isinstance(r, list) and route in r))]


def _route_target(ws, route):
    targets = _targets(ws, _router_name(ws), route)
    assert len(targets) == 1, f"route {route!r} should lead to exactly one node, found {[t.name for t in targets]}"
    return targets[0]


def _reachable(ws, start, avoid=frozenset()):
    """Nodes reachable from `start` (inclusive) without passing through `avoid`."""
    if start.name in avoid:
        return []
    seen, queue = {start.name: start}, deque([start])
    while queue:
        node = queue.popleft()
        for nxt in _targets(ws, node.name):
            if nxt.name not in seen and nxt.name not in avoid:
                seen[nxt.name] = nxt
                queue.append(nxt)
    return list(seen.values())


def _calls_in(function_name: str, seen=None) -> set[str]:
    """Names a module-level function calls, following calls into the module's own helpers.
    Function nodes (e.g. document_creation_agent) call Drive routines directly instead of holding tools."""
    seen = seen if seen is not None else set()
    if function_name in seen:
        return set()
    seen.add(function_name)
    tree = _tree()
    function = _function(tree, function_name)
    if function is None:
        return set()
    module_functions = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    called = set()
    for n in ast.walk(function):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            called.add(n.id)
            if n.id in module_functions:
                called |= _calls_in(n.id, seen)
    # Names bound to a dict of routines (e.g. CREATORS = {"DOC": _create_document, ...}).
    for assign in tree.body:
        if isinstance(assign, ast.Assign) and any(isinstance(t, ast.Name) and t.id in called for t in assign.targets):
            called |= {v.id for v in ast.walk(assign.value) if isinstance(v, ast.Name)}
    return called


def _imported_tools() -> set[str]:
    """Names workspace_agent.py imports from the tools package."""
    return {(a.asname or a.name) for n in ast.walk(_tree())
            if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "tools" for a in n.names}


def _tool_names(node) -> set[str]:
    agent = getattr(node, "agent", node)
    names = {getattr(t, "__name__", None) or getattr(t, "name", "") for t in getattr(agent, "tools", None) or []}
    if not hasattr(agent, "instruction"):  # a function node: the Drive routines it calls
        names |= _calls_in(node.name) & _imported_tools()
    return names


def _capabilities(tool_names) -> set[str]:
    return {cap for cap, pattern in CAPABILITIES.items()
            for name in tool_names if re.search(pattern, name or "", re.I)}


def _route_capabilities(ws, route, avoid=frozenset()) -> set[str]:
    tools = set().union(*(_tool_names(n) for n in _reachable(ws, _route_target(ws, route), avoid)))
    return _capabilities(tools)


def _confirmation_nodes(ws) -> set[str]:
    """Function nodes that pause for the user (yield RequestInput)."""
    return {frm.name for frm, _, _ in _edges(ws) if _yields_request_input(frm.name)} | \
           {to.name for _, to, _ in _edges(ws) if _yields_request_input(to.name)}


# --- The router_agent (AGENTS.md 1.31: sort requests into categories) ------------------

def test_router_agent_has_every_required_category(ws):
    schema = ws.router_agent.output_schema
    assert schema is not None, "router_agent needs an output_schema so the router gets a category"
    categories = set(typing.get_args(schema.model_fields["category"].annotation))
    assert REQUIRED_CATEGORIES <= categories, f"missing categories: {sorted(REQUIRED_CATEGORIES - categories)}"
    assert "UNKNOWN" in categories, "requests that aren't Drive commands need an UNKNOWN category"


def test_router_agent_returns_a_confidence_and_the_full_request(ws):
    fields = ws.router_agent.output_schema.model_fields
    assert "confidence" in fields and "request" in fields


def test_request_field_describes_workspace_details_not_calendar_ones(ws):
    """The model fills `request` from this description; a calendar description makes it drop file details."""
    description = (ws.router_agent.output_schema.model_fields["request"].description or "").lower()
    assert "folder" in description or "file" in description or "document" in description, \
        "describe what a workspace request must keep: file type, topic, source document, target folder"
    assert "attendees" not in description and "recurrence" not in description, \
        "the `request` description is still the calendar one"


def test_router_agent_instruction_names_every_category(ws):
    instruction = ws.router_agent.instruction
    missing = [c for c in REQUIRED_CATEGORIES if c not in instruction]
    assert not missing, f"router_agent's instruction never explains {missing}"


def test_router_agent_instruction_covers_every_agents_md_case(ws):
    instruction = ws.router_agent.instruction.lower()
    for word in ("doc", "slide", "pdf", "infographic", "folder", "move"):
        assert word in instruction, f"router_agent's instruction never mentions {word!r} (AGENTS.md 1.32)"


def test_every_llm_agent_has_a_model_and_an_instruction(ws):
    missing = []
    for node in ws.workspace_agent.graph.nodes:
        agent = getattr(node, "agent", node)
        if hasattr(agent, "instruction") and hasattr(agent, "model"):
            if not agent.model:
                missing.append(f"{agent.name}: no model")
            if not agent.instruction:
                missing.append(f"{agent.name}: no instruction")
    assert not missing, "; ".join(missing)


# --- The router ---------------------------------------------------------------------

@pytest.mark.parametrize("category", sorted(REQUIRED_CATEGORIES))
def test_router_sends_each_category_on_with_the_request(ws, category):
    route, output = _route(ws, category, 0.95, REQUESTS[category])
    assert route == category
    assert output == REQUESTS[category], "the next node must get the restated request"


def test_router_accepts_the_schema_object_as_well_as_a_dict(ws):
    command = ws.router_agent.output_schema(category="DOCUMENT_MOVEMENT", confidence=0.9, request=MOVE)
    event = _router_fn(ws)(command, FakeContext())
    assert (event.actions.route, event.output) == ("DOCUMENT_MOVEMENT", MOVE)


def test_router_low_confidence_asks_instead_of_acting(ws):
    """A guess must never delete a folder or create files."""
    route, _ = _route(ws, "FOLDER_DELETION", 0.2, REQUESTS["FOLDER_DELETION"])
    assert route == "UNKNOWN"


def test_router_off_topic_is_unknown(ws):
    assert _route(ws, "UNKNOWN", 0.99, CALENDAR_REQUEST)[0] == "UNKNOWN"


def test_router_empty_input_is_unknown(ws):
    assert _router_fn(ws)({}, FakeContext()).actions.route == "UNKNOWN"


# --- The graph ------------------------------------------------------------------------

def test_graph_starts_with_router_agent_then_the_router(ws):
    starts = {to.name for frm, to, _ in _edges(ws) if frm.name in {"__START__", "START"}}
    assert starts == {ws.router_agent.name}
    _router_name(ws)


@pytest.mark.parametrize("category", sorted(REQUIRED_CATEGORIES))
def test_each_category_has_a_route(ws, category):
    _route_target(ws, category)


def test_unknown_route_replies_and_ends(ws):
    reply = _route_target(ws, "UNKNOWN")
    assert not _targets(ws, reply.name), "the UNKNOWN reply should end the turn"
    agent = getattr(reply, "agent", reply)
    assert not _tool_names(agent), "the UNKNOWN reply must not hold Drive tools"


# --- Tools each route can reach (AGENTS.md 1.32) -------------------------------------------

@pytest.mark.parametrize("category", sorted(REQUIRED_CATEGORIES))
def test_route_can_reach_the_tools_its_command_needs(ws, category):
    missing = ROUTE_NEEDS[category] - _route_capabilities(ws, category)
    assert not missing, (
        f"{category} can't reach a tool for {sorted(missing)} "
        f"(tool names are matched by CAPABILITIES at the top of this file)")


@pytest.mark.parametrize("category", sorted(REQUIRED_CATEGORIES))
def test_route_cannot_make_other_kinds_of_changes(ws, category):
    extra = _route_capabilities(ws, category) & ROUTE_FORBIDS[category]
    assert not extra, f"{category} should not be able to reach {sorted(extra)}"


def test_document_creation_searches_folders_before_asking(ws):
    """'use similarity search to match them into a drive folder': the search runs before the
    confirmation, so the user is asked about a matched folder (and creating one comes after)."""
    before_asking = _route_capabilities(ws, "DOCUMENT_CREATION", avoid=_confirmation_nodes(ws))
    assert "folder_search" in before_asking, "search the Drive folders before asking the user where to save"


def test_drive_tools_turn_errors_into_results(ws):
    """LlmAgents holding Drive tools pass tool errors back to the model instead of failing the turn."""
    from agents.tool_errors import report_tool_error
    wrong = [n.name for n in ws.workspace_agent.graph.nodes
             if hasattr(getattr(n, "agent", n), "instruction") and getattr(getattr(n, "agent", n), "tools", None)
             and getattr(getattr(n, "agent", n), "on_tool_error_callback", None) is not report_tool_error]
    assert not wrong, f"set on_tool_error_callback=report_tool_error on {wrong}"


# --- Human confirmation before writing (AGENTS.md: "ask for answer key or human confirmation") -----

@pytest.mark.parametrize("category", ["DOCUMENT_CREATION"])
def test_creation_asks_the_user_before_writing_to_drive(ws, category):
    """Every path from the route to a node that writes to Drive passes through a RequestInput node."""
    confirmations = _confirmation_nodes(ws)
    assert confirmations, "no node yields RequestInput: nothing asks for the answer key / confirmation"
    unguarded = _capabilities(set().union(*(
        _tool_names(n) for n in _reachable(ws, _route_target(ws, category), avoid=confirmations))))
    writes = unguarded & set(WRITE_CAPABILITIES)
    assert not writes, f"{category} can reach {sorted(writes)} without asking the user first"


def test_folder_deletion_asks_the_user_before_deleting(ws):
    """Not spelled out in AGENTS.md, but deleting a Drive folder can't be undone."""
    unguarded = _capabilities(set().union(*(
        _tool_names(n) for n in _reachable(ws, _route_target(ws, "FOLDER_DELETION"), avoid=_confirmation_nodes(ws)))))
    assert "delete_folder" not in unguarded, "FOLDER_DELETION reaches the delete tool without asking the user"


def test_confirmation_asks_about_an_answer_key(ws):
    sources = [ast.get_source_segment(_source(), _function(_tree(), name)) or "" for name in _confirmation_nodes(ws)]
    assert any("answer key" in s.lower() for s in sources), \
        "AGENTS.md: the confirmation should offer an answer key (e.g. for the related rates quiz)"


# --- Live: the real router_agent (opt-in) ----------------------------------------------------

LIVE = pytest.mark.skipif(os.environ.get("RUN_LIVE_GEMINI") != "1",
                          reason="set RUN_LIVE_GEMINI=1 (and GEMINI_API_KEY) to call Gemini")


async def _classify(ws, text):
    from google.adk import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    app, user, session_id = "workspace_agent_test", "test", uuid.uuid4().hex
    sessions = InMemorySessionService()
    await sessions.create_session(app_name=app, user_id=user, session_id=session_id)
    runner = Runner(agent=ws.router_agent, app_name=app, session_service=sessions)
    message = types.Content(role="user", parts=[types.Part(text=text)])
    async for _ in runner.run_async(user_id=user, session_id=session_id, new_message=message):
        pass
    session = await sessions.get_session(app_name=app, user_id=user, session_id=session_id)
    return session.state.get(ws.router_agent.output_key)


@pytest.fixture(scope="module")
def live_ws(ws):
    from dotenv import load_dotenv
    load_dotenv(APP_ROOT / ".env")
    if os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]
    if not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("no GEMINI_API_KEY / GOOGLE_API_KEY")
    assert ws.router_agent.output_key, "router_agent needs an output_key to read its decision back"
    return ws


@LIVE
@pytest.mark.parametrize("text, category", [
    (QUIZ, "DOCUMENT_CREATION"),
    (SLIDES, "DOCUMENT_CREATION"),
    (PDF, "DOCUMENT_CREATION"),
    (MOVE, "DOCUMENT_MOVEMENT"),
    (REQUESTS["FOLDER_CREATION"], "FOLDER_CREATION"),
    (REQUESTS["FOLDER_DELETION"], "FOLDER_DELETION"),
])
def test_live_router_agent_sorts_each_request(live_ws, text, category):
    command = asyncio.run(_classify(live_ws, text))
    assert command, "router_agent returned nothing"
    assert command["category"] == category, f"{text!r} -> {command}"


@LIVE
def test_live_move_keeps_the_folder_name(live_ws):
    command = asyncio.run(_classify(live_ws, MOVE))
    assert "atlantic salmon research project" in command["request"].lower()


@LIVE
def test_live_quiz_keeps_the_topic(live_ws):
    command = asyncio.run(_classify(live_ws, QUIZ))
    assert "related rates" in command["request"].lower()


@LIVE
def test_live_calendar_request_is_unknown(live_ws):
    command = asyncio.run(_classify(live_ws, CALENDAR_REQUEST))
    assert command["category"] == "UNKNOWN"
