"""
Shared helpers for the end-to-end tests: run an ADK Workflow through a real
Runner with every LlmAgent answered by a ScriptedLlm instead of Gemini.

    models = install_scripts(monkeypatch, workflow, agent_name=[Reply({...}), Call("tool", ...), "text"])
    chat = Session(workflow)
    question = pending_question(chat.say("..."))        # (interrupt_id, args) or None
    chat.answer("INTERRUPT_ID", {...})                   # replies the way the UI must
"""
import asyncio
import json
import re
import uuid
from typing import Any, AsyncGenerator

from google.adk import Runner
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types


class Call:
    """A tool call the scripted model makes."""

    def __init__(self, tool_name: str, /, **args):  # positional-only: tools may take a `name` argument
        self.name, self.args = tool_name, args


class Reply:
    """Structured output (output_schema): sent via set_model_response when the agent has tools."""

    def __init__(self, data: dict):
        self.data = data


def _request_text(request: LlmRequest) -> str:
    """Everything the model was sent: text, plus tool results (as JSON)."""
    out = []
    for c in request.contents or []:
        for p in c.parts or []:
            if p.text:
                out.append(p.text)
            elif p.function_response:
                out.append(json.dumps(p.function_response.response, default=str))
    return "\n".join(out)


class ScriptedLlm(BaseLlm):
    """Answers each model call with the next step of its script, and records each request's text."""

    model: str = "scripted"
    agent: str = ""
    script: list[Any] = []
    requests: list[str] = []

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(_request_text(llm_request))
        if not self.script:
            raise AssertionError(f"{self.agent} was called more times than scripted; last request:\n{self.requests[-1]}")
        step = self.script.pop(0)
        if isinstance(step, Call):
            part = types.Part(function_call=types.FunctionCall(name=step.name, args=step.args))
        elif isinstance(step, Reply):
            if "set_model_response" in llm_request.tools_dict:
                part = types.Part(function_call=types.FunctionCall(name="set_model_response", args=step.data))
            else:
                part = types.Part(text=json.dumps(step.data))
        else:
            part = types.Part(text=str(step))
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def llm_nodes(workflow) -> dict:
    """Every LlmAgent the workflow will actually run, nested Workflows included.
    (Workflow keeps its own copies of the agents, so patching the module-level
    agents would not reach them.)"""
    found = {}
    for n in workflow.graph.nodes:
        if hasattr(n, "graph") and n.graph is not None:
            found.update(llm_nodes(n))
        elif hasattr(n, "instruction") and hasattr(n, "model"):
            found[n.name] = n
    return found


def install_scripts(monkeypatch, workflow, **scripts) -> dict:
    """Gives each LlmAgent in `workflow` a ScriptedLlm; returns them by agent name.
    An agent with no script fails the test if it is called. Removes the API keys,
    so nothing can reach Gemini."""
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    nodes = llm_nodes(workflow)
    unknown = set(scripts) - set(nodes)
    assert not unknown, f"no such agents in {workflow.name}: {unknown} (has {sorted(nodes)})"
    models = {}
    for name, agent in nodes.items():
        model = ScriptedLlm(agent=name, script=list(scripts.get(name, [])))
        monkeypatch.setattr(agent, "model", model)
        models[name] = model
    return models


class Session:
    """One conversation with a workflow through a real Runner."""

    def __init__(self, workflow):
        self.app, self.user, self.id = f"{workflow.name}_e2e", "tester", uuid.uuid4().hex
        self.service = InMemorySessionService()
        asyncio.run(self.service.create_session(app_name=self.app, user_id=self.user, session_id=self.id))
        self.runner = Runner(agent=workflow, app_name=self.app, session_service=self.service)
        self.events = []

    def _run(self, message: types.Content):
        async def go():
            out = []
            async for event in self.runner.run_async(user_id=self.user, session_id=self.id, new_message=message):
                out.append(event)
            return out
        new = asyncio.run(go())
        self.events += new
        return new

    def say(self, text: str):
        return self._run(types.Content(role="user", parts=[types.Part(text=text)]))

    def answer(self, interrupt_id: str, response: dict):
        """Reply to a RequestInput the way the UI must: a function_response with the interrupt's id."""
        part = types.Part(function_response=types.FunctionResponse(
            id=interrupt_id, name="adk_request_input", response=response))
        return self._run(types.Content(role="user", parts=[part]))

    @property
    def state(self) -> dict:
        session = asyncio.run(self.service.get_session(app_name=self.app, user_id=self.user, session_id=self.id))
        return session.state


def pending_question(events) -> tuple[str, dict] | None:
    """(interrupt id, args) of the RequestInput the run paused on, if any."""
    for event in reversed(events):
        for call in event.get_function_calls():
            if call.name == "adk_request_input":
                return call.id, call.args or {}
    return None


def final_text(events) -> str:
    for event in reversed(events):
        if event.content and event.content.parts:
            text = "".join(p.text for p in event.content.parts if p.text and not p.thought)
            if text.strip():
                return text
        if isinstance(getattr(event, "output", None), str) and event.output.strip():
            return event.output
    return ""


FOLDER = "application/vnd.google-apps.folder"


# --- Fake Drive + Slides --------------------------------------------------------------

class _Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeDrive:
    """Folders and files in a pretend Drive; records every create, move, and trash.
    Starting folders have ids folderN, starting files docN, anything created fileN."""

    def __init__(self, folders=("Calculus I", "AP Chemistry", "Atlantic Salmon Research Project"),
                 files=(("Salmon Survey Results.pdf", "application/pdf", ""),
                        ("Document A", "application/vnd.google-apps.document",
                         "Plan 1: review 10.1. Plan 2: review 10.2. Plan 3: practice test."))):
        self.items = [{"id": f"folder{i}", "name": name, "mimeType": FOLDER, "parents": [],
                       "webViewLink": f"https://drive.example/folder{i}"} for i, name in enumerate(folders)]
        self.items += [{"id": f"doc{i}", "name": name, "mimeType": mime, "parents": [],
                        "webViewLink": f"https://drive.example/doc{i}"} for i, (name, mime, _) in enumerate(files)]
        self.uploads = {f"doc{i}": text.encode() for i, (_, _, text) in enumerate(files)}  # file id -> bytes
        self.slide_requests = []
        self.deleted = []   # permanently deleted (temporary files)
        self.trashed = []   # moved to the trash

    def files(self):
        return _Files(self)

    def presentations(self):
        return _Presentations(self)

    def _item(self, file_id):
        return next(i for i in self.items if i["id"] == file_id)

    # What the tests look at
    def created(self, kind=None):
        """Items created during the test (not the starting ones), excluding deleted temporary files."""
        made = [i for i in self.items if i["id"].startswith("file") and i["id"] not in self.deleted]
        return [i for i in made if kind is None or (i["mimeType"] == FOLDER) == (kind == "folder")]

    def text(self, item) -> str:
        return self.uploads.get(item["id"], b"").decode("utf-8", "ignore")

    def folder_named(self, name):
        return next(i for i in self.items if i["mimeType"] == FOLDER and i["name"] == name)

    def file_named(self, name):
        return next(i for i in self.items if i["mimeType"] != FOLDER and i["name"] == name)


_PUBLIC = ("id", "name", "mimeType", "webViewLink")


class _Files:
    def __init__(self, drive: FakeDrive):
        self.drive = drive

    def list(self, q="", fields=None, orderBy=None, pageSize=100, pageToken=None):
        gone = set(self.drive.deleted) | set(self.drive.trashed)
        want_folders = f"mimeType='{FOLDER}'" in q
        items = [i for i in self.drive.items if i["id"] not in gone and (i["mimeType"] == FOLDER) == want_folders]
        contains = re.search(r"name contains '((?:[^'\\]|\\.)*)'", q)
        if contains:
            needle = contains.group(1).replace("\\'", "'").lower()
            items = [i for i in items if needle in i["name"].lower()]
        return _Request({"files": [dict(i) for i in items][:pageSize]})

    def get(self, fileId, fields=None):
        return _Request(dict(self.drive._item(fileId)))

    def get_media(self, fileId):
        return _Request(self.drive.uploads.get(fileId, b""))

    def create(self, body, media_body=None, fields=None):
        n = len(self.drive.items)
        item = {"id": f"file{n}", "name": body["name"],
                "mimeType": body.get("mimeType", media_body.mimetype() if media_body else None),
                "parents": body.get("parents", []), "webViewLink": f"https://drive.example/file{n}"}
        self.drive.items.append(item)
        if media_body:
            self.drive.uploads[item["id"]] = media_body._fd.getvalue()
        return _Request({k: item[k] for k in _PUBLIC})

    def update(self, fileId, body=None, addParents=None, removeParents=None, fields=None):
        item = self.drive._item(fileId)
        if (body or {}).get("trashed"):
            self.drive.trashed.append(fileId)
        if addParents is not None:
            removed = set(filter(None, (removeParents or "").split(",")))
            item["parents"] = [p for p in item["parents"] if p not in removed] + [addParents]
        return _Request({k: item[k] for k in _PUBLIC})

    def export(self, fileId, mimeType):
        if mimeType == "application/pdf":
            return _Request(b"%PDF-1.4 fake")
        return _Request(self.drive.uploads.get(fileId, b""))

    def delete(self, fileId):
        self.drive.deleted.append(fileId)
        return _Request("")


class _Presentations:
    def __init__(self, drive: FakeDrive):
        self.drive = drive

    def get(self, presentationId, fields=None):
        return _Request({"slides": [{"objectId": "blank"}]})

    def batchUpdate(self, presentationId, body):
        self.drive.slide_requests += body["requests"]
        return _Request({})
