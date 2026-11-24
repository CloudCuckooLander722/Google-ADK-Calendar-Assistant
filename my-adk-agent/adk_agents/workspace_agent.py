import sys
from pathlib import Path
from typing import Literal, Optional, Union

from google.adk import Context, Event, Workflow
from google.adk.agents import LlmAgent
from google.adk.events import RequestInput
from google.adk.workflow import START, node
from google.genai import types
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.tool_errors import report_tool_error
from tools.workspace_tools import (
    _create_document,
    _create_slidedeck,
    _create_pdf,
    _create_infographic,
    list_drive_folders,
    create_drive_folder,
    find_drive_folder,
    delete_drive_folder,
    search_drive_files,
    read_drive_file,
    move_drive_file,
    rank_folders,
)


# PYDANTIC SCHEMAS FOR DETERMINISTIC CONFIGURATION
WorkspaceCategory = Literal[
    "DOCUMENT_CREATION",
    "FOLDER_CREATION",
    "FOLDER_DELETION",
    "DOCUMENT_MOVEMENT",
    "UNKNOWN"
]

class WorkspaceCommand(BaseModel):
    category: WorkspaceCategory
    confidence: float = Field(
        description="Probability score between 0.0 and 1.0 indicating confidence in this category."
    )
    request: str = Field(
        description=(
            "The full request restated as one self-contained instruction, with every detail the user "
            "gave or the conversation already settled: the file type (Doc, Slides, PDF, infographic), "
            "topic and contents, any source document to build it from, the target folder name if one "
            "was given, which existing file or folder it refers to, and whether an answer key is wanted."
        )
    )
    missing_info: Optional[str] = Field(
        default=None,
        description="What essential detail is missing (e.g. 'no report shared'), or null if nothing is missing.",
    )

class DocumentContent(BaseModel):
    """
    Organizes LLM data into a strict, Pydantic schema for reliability.
    content_agent only writes the draft; finding the Drive folder happens in find_folder.
    """

    title: str = Field(
                       description="Concise, descriptive title for the document without surrounding quotes or Markdown formatting."
                       )

    content: str = Field(
        description=(
            "Full body text of the document formatted in clean Markdown. "
            "Do not include the document title, the answer key, or the works cited section here."
        )
    )

    works_cited: Optional[str] = Field(
        default=None,
        description=(
            "Formatted citations or source URLs referenced in the content, one per line, "
            "or null if no external sources were used."
        )
    )

    file_type: Literal["DOC", "SLIDES", "PDF", "INFOGRAPHIC"] = Field(
        description="""The type of document to create, as specified by the user.
                    - "DOC": Google Docs document
                    - "SLIDES": Google Slides presentation
                    - "PDF": PDF document
                    - "INFOGRAPHIC": Infographic or visual representation of information""")

    folder_topic: str = Field(
        description=(
            "A short Drive folder name for the subject this document belongs to, e.g. 'Calculus I', "
            "'AP Chemistry', 'Atlantic Salmon Research Project'. Use the folder the user named, if any."
        )
    )

    answer_key: Optional[str] = Field(
        default=None,
        description=(
            "For quizzes, problem sets, and worksheets: worked answers to every question, in Markdown. "
            "Null for documents that have no questions."
        )
    )


class FolderDecision(BaseModel):
    """The user's answer to confirm_folder. The Runner puts it in ctx.resume_inputs["CONFIRM_FOLDER"]."""
    action: Literal["USE_SUGGESTED", "USE_OTHER", "CREATE_NEW", "NO_FOLDER", "CANCEL"] = Field(
        description=(
            "USE_SUGGESTED: save it in the suggested existing folder. USE_OTHER: save it in the folder named "
            "in folder_name (created if it doesn't exist). CREATE_NEW: create a new folder (folder_name, or the "
            "suggested name). NO_FOLDER: save it in My Drive. CANCEL: don't create anything."))
    folder_name: str = Field(default="", description="Folder for USE_OTHER / CREATE_NEW.")
    include_answer_key: bool = Field(default=False, description="Add the answer key at the end of the document.")


# ALSO AGENT DEFINITIONS

router_agent = LlmAgent(
    name="workspace_router_agent",
    model="gemini-2.5-flash",
    description="An agent that routes requests to the appropriate document management agent based on the user's intent.",
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    instruction=(
        "You sort Google Drive / Workspace requests for the workspace_agent. Read the request (and the "
        "conversation so far, to resolve words like 'this', 'it', or 'that report') and classify it into "
        "exactly one category, with a probability confidence (0.0 to 1.0):\n"
        "- 'DOCUMENT_CREATION': make a new file, whether written from scratch or generated from another "
        "document. This covers Google Docs, Slides decks, PDFs, and infographics "
        "(e.g., 'I want a quiz on curveball related rates problems that test my concepts', "
        "'Summarize this report into a slidedeck', 'Create a PDF for me or an infographic', "
        "'Make me a problem set for Calc I'). Placing the new file in a matching Drive folder, or "
        "creating that folder when none matches, is part of this category, not FOLDER_CREATION.\n"
        "- 'FOLDER_CREATION': the user only asks for a new Drive folder, with no file to make or move "
        "(e.g., 'Make a folder called Chem Labs').\n"
        "- 'FOLDER_DELETION': the user asks to delete or remove a Drive folder.\n"
        "- 'DOCUMENT_MOVEMENT': move an existing file into a Drive folder "
        "(e.g., 'Move this PDF to drive folder \"Atlantic Salmon Research Project\"'). Creating the "
        "folder when it doesn't exist is part of this category.\n"
        "- 'UNKNOWN': anything that maps to none of the above, including calendar or scheduling requests.\n"
        "If a request has several steps (e.g., make a quiz, then file it), classify it by its main "
        "action: making a file is DOCUMENT_CREATION, relocating an existing one is DOCUMENT_MOVEMENT.\n"
        "Fill `request` with a self-contained restatement that includes every detail: the file type "
        "(Doc, Slides, PDF, infographic), topic and contents, any source document it should be built "
        "from, the target folder name if one was given, and whether the user wants an answer key. "
        "Set `missing_info` only when something essential is genuinely absent (e.g., 'summarize this "
        "report' with no report shared or named, or 'move this PDF' with no file identified)."
    ),
    output_schema=WorkspaceCommand,
    output_key="workspace_command",
)

CONFIDENCE_THRESHOLD = 0.60
ROUTES = ("DOCUMENT_CREATION", "FOLDER_CREATION", "FOLDER_DELETION", "DOCUMENT_MOVEMENT")


def router(node_input: Union[WorkspaceCommand, dict], ctx: Context) -> Event:
    """Turns router_agent's category + confidence into a route; unsure requests ask for clarification."""
    if not node_input:
        return Event(route="UNKNOWN", output="")
    command = node_input if isinstance(node_input, WorkspaceCommand) else WorkspaceCommand.model_validate(node_input)
    ctx.state["last_workspace_command"] = command.model_dump()
    if command.category in ROUTES and command.confidence >= CONFIDENCE_THRESHOLD:
        return Event(route=command.category, output=command.request)
    return Event(route="UNKNOWN", output=command.missing_info or command.request)


def ask_for_clarification(node_input: Optional[str], ctx: Context) -> str:
    """UNKNOWN route: ends the turn with a question."""
    missing = (ctx.state.get("last_workspace_command") or {}).get("missing_info")
    if missing:
        return f"I need a bit more information before I can do that: {missing}."
    return ("I'm sorry, I couldn't tell what you'd like me to do in Google Drive. I can create a Doc, "
            "slide deck, PDF, or infographic, create or delete a folder, or move a file into a folder.")


def reply_cancelled(node_input: Optional[str] = None) -> str:
    return "Okay, I didn't create anything."


# --- DOCUMENT_CREATION: draft -> find folder -> ask the user -> create -----------------------------

content_agent = LlmAgent(
    name="content_agent",
    model="gemini-2.5-flash",
    description="Writes the content of a requested Doc, slide deck, PDF, or infographic.",
    generate_content_config=types.GenerateContentConfig(temperature=0.2),
    instruction=(
        "You write the content of the document the user asked for; another step saves it to Google Drive "
        "after the user confirms, so never claim it was saved.\n"
        "- Pick `file_type` from the request: DOC unless the user asked for slides, a PDF, or an infographic.\n"
        "- Write `content` in Markdown. Start each section with a '## ' heading: for SLIDES each heading "
        "becomes a slide (keep each to about 5 short bullet points); for INFOGRAPHIC each heading becomes "
        "a card (2-4 one-line points, short headings).\n"
        "- For quizzes, problem sets, and worksheets (e.g. 'a quiz on curveball related rates problems "
        "that test my concepts'), write questions that test concepts, not just plug-in steps, numbered, "
        "and put the worked answers in `answer_key`, never in `content`.\n"
        "- If the request is built from a source document (e.g. 'summarize this report', 'Document A'), find it "
        "with `search_drive_files` and read it with `read_drive_file`, then cover all of its important "
        "information. If you can't find or read it, say so in `content` instead of inventing it.\n"
        "- Set `folder_topic` to a short subject folder name (the folder the user named, if any).\n"
        "- List any sources you relied on in `works_cited`; never invent a URL."
    ),
    tools=[search_drive_files, read_drive_file],
    on_tool_error_callback=report_tool_error,
    output_schema=DocumentContent,
    output_key="document_draft",
)

def find_folder(node_input: Union[DocumentContent, dict], ctx: Context) -> dict:
    """Similarity search over the Drive folders the app can see, for the draft's folder_topic."""
    draft = node_input if isinstance(node_input, DocumentContent) else DocumentContent.model_validate(node_input)
    try:
        folders = list_drive_folders()
        search_error = None
    except ValueError as error:  # e.g. not signed in: still ask, the create step will report it
        folders, search_error = [], str(error)
    match, others = rank_folders(draft.folder_topic, folders)
    return {
        "draft": draft.model_dump(),
        "suggested_folder": match,
        "other_folders": others,
        "search_error": search_error,
    }


def _confirmation_message(proposal: dict) -> str:
    draft, folder = proposal["draft"], proposal["suggested_folder"]
    kind = {"DOC": "Google Doc", "SLIDES": "slide deck", "PDF": "PDF", "INFOGRAPHIC": "infographic"}[draft["file_type"]]
    lines = [f"I drafted \"{draft['title']}\" as a {kind}."]
    if folder:
        lines.append(f"Save it in your \"{folder['name']}\" folder?")
    else:
        lines.append(f"I didn't find a matching folder. Create a new \"{draft['folder_topic']}\" folder for it?")
    if proposal["other_folders"]:
        lines.append("Other folders: " + ", ".join(f"\"{f['name']}\"" for f in proposal["other_folders"]) + ".")
    if draft.get("answer_key"):
        lines.append("Should I include the answer key at the end?")
    else:
        lines.append("(No answer key: this document has no questions.)")
    return " ".join(lines)


@node(rerun_on_resume=True)
async def confirm_folder(ctx: Context, node_input: Optional[dict] = None):
    """
    Pauses for the user's permission before anything is written to Drive: which folder (the
    suggested one, another, a new one, or none) and whether to include the answer key.
    The Runner fills ctx.resume_inputs["CONFIRM_FOLDER"] from the user's reply and reruns this node.
    """
    proposal = node_input or ctx.state.get("folder_proposal") or {}
    ctx.state["folder_proposal"] = proposal
    answer = ctx.resume_inputs.get("CONFIRM_FOLDER")
    if answer is None:
        yield RequestInput(
            interrupt_id="CONFIRM_FOLDER",
            message=_confirmation_message(proposal),
            response_schema=FolderDecision,
            payload=proposal,
        )
        return

    # Fields the user left out can arrive as None; let the schema's defaults fill them.
    decision = FolderDecision.model_validate({k: v for k, v in dict(answer).items() if v is not None})
    if decision.action == "CANCEL":
        yield Event(route="CANCEL", output="")
    else:
        yield Event(route="CREATE", output={**proposal, "decision": decision.model_dump()})


def _resolve_folder(proposal: dict, decision: FolderDecision) -> Optional[dict]:
    """The Drive folder the user chose, creating it when it doesn't exist yet. None for My Drive."""
    if decision.action == "NO_FOLDER":
        return None
    if decision.action == "USE_SUGGESTED" and proposal.get("suggested_folder"):
        return proposal["suggested_folder"]
    name = decision.folder_name.strip() or proposal["draft"]["folder_topic"]
    if decision.action == "USE_OTHER":
        existing = [f for f in list_drive_folders() if f.get("name", "").strip().lower() == name.lower()]
        if existing:
            return existing[0]
    return create_drive_folder(name)


CREATORS = {
    "DOC": _create_document,
    "SLIDES": _create_slidedeck,
    "PDF": _create_pdf,
    "INFOGRAPHIC": _create_infographic,
}


def document_creation_agent(node_input: dict, ctx: Context) -> str:
    """
    Runs only after confirm_folder: puts the confirmed draft in the chosen folder.
    """
    draft = DocumentContent.model_validate(node_input["draft"])
    decision = FolderDecision.model_validate(node_input["decision"])

    content = draft.content
    if decision.include_answer_key and draft.answer_key:
        content += "\n\n## Answer Key\n\n" + draft.answer_key

    try:
        folder = _resolve_folder(node_input, decision)
        created = CREATORS[draft.file_type](
            content, draft.works_cited, title=draft.title, folder_id=folder["id"] if folder else None)
    except ValueError as error:
        return f"I couldn't create \"{draft.title}\": {error}"

    ctx.state["created_file"] = {**created, "folder": folder}
    where = f"in \"{folder['name']}\"" if folder else "in My Drive"
    return f"Created \"{created.get('name', draft.title)}\" {where}: {created.get('webViewLink', '')}".strip()


# --- FOLDER_CREATION ---------------------------------------------------------------------------

folder_creation_agent = LlmAgent(
    name="folder_creation_agent",
    model="gemini-2.5-flash",
    description="Creates a Google Drive folder, reusing one that already matches.",
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    instruction=(
        "Create the Drive folder the user asked for.\n"
        "- First call `find_drive_folder` with the name. If it returns a `match` with the same name "
        "(ignoring case), don't create a duplicate: tell the user it already exists and give its link.\n"
        "- Otherwise call `create_drive_folder` with the name exactly as the user gave it, and reply "
        "with the folder's name and link.\n"
        "- If a tool returns \"status\": \"error\", tell the user what failed in plain words."
    ),
    tools=[find_drive_folder, create_drive_folder],
    on_tool_error_callback=report_tool_error,
)


# --- FOLDER_DELETION: find -> ask the user -> trash -----------------------------------------------

class FolderToDelete(BaseModel):
    found: bool = Field(description="True if one folder clearly matches the request.")
    folder_id: Optional[str] = Field(default=None, description="The matching folder's id.")
    folder_name: Optional[str] = Field(default=None, description="The matching folder's name.")
    note: str = Field(default="", description="If not found or ambiguous: the candidates, or what's missing.")


folder_lookup_agent = LlmAgent(
    name="folder_lookup_agent",
    model="gemini-2.5-flash",
    description="Finds the Drive folder the user wants to delete; never deletes anything itself.",
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    instruction=(
        "Find the Drive folder the user wants to delete. Call `find_drive_folder` with its name.\n"
        "- If there is a `match`, set found=true with its id and name.\n"
        "- If there is no match, or the candidates are equally close, set found=false and list the "
        "candidates' names in `note` so the user can say which one."
    ),
    tools=[find_drive_folder],
    on_tool_error_callback=report_tool_error,
    output_schema=FolderToDelete,
    output_key="folder_to_delete",
)


class DeleteDecision(BaseModel):
    """The user's answer to confirm_folder_deletion (ctx.resume_inputs["CONFIRM_DELETE"])."""
    approve: bool = Field(description="True to move the folder and everything in it to the trash.")


@node(rerun_on_resume=True)
async def confirm_folder_deletion(ctx: Context, node_input: Optional[dict] = None):
    """Asks before trashing a folder: it takes every file inside with it."""
    target = node_input or ctx.state.get("folder_to_delete") or {}
    if not target.get("found") or not target.get("folder_id"):
        yield Event(route="NOT_FOUND", output=target.get("note") or "")
        return
    answer = ctx.resume_inputs.get("CONFIRM_DELETE")
    if answer is None:
        yield RequestInput(
            interrupt_id="CONFIRM_DELETE",
            message=(f"Delete the folder \"{target['folder_name']}\" and everything in it? "
                     "It goes to your Drive trash, where you can restore it for 30 days."),
            response_schema=DeleteDecision,
            payload=target,
        )
        return
    decision = DeleteDecision.model_validate({k: v for k, v in dict(answer).items() if v is not None})
    yield Event(route="DELETE" if decision.approve else "CANCEL", output=target)


def trash_folder(node_input: dict, ctx: Context) -> str:
    """Runs only after the user said yes in confirm_folder_deletion."""
    try:
        return delete_drive_folder(node_input["folder_id"])
    except ValueError as error:
        return f"I couldn't delete \"{node_input.get('folder_name')}\": {error}"


def reply_folder_not_found(node_input: Optional[str] = None) -> str:
    detail = f" {node_input}" if node_input else ""
    return f"I couldn't tell which folder to delete, so I didn't delete anything.{detail}"


def reply_kept(node_input=None) -> str:
    return "Okay, I kept the folder."


# --- DOCUMENT_MOVEMENT -----------------------------------------------------------------------------

document_movement_agent = LlmAgent(
    name="document_movement_agent",
    model="gemini-2.5-flash",
    description="Moves a Drive file into a folder, creating the folder if it doesn't exist.",
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    instruction=(
        "Move the file the user named into the Drive folder they named "
        "(e.g. 'Move this PDF to drive folder \"Atlantic Salmon Research Project\"').\n"
        "1. Find the file with `search_drive_files`. If several files match and it's not clear which one, "
        "list them and ask; don't guess. If none match, say so and stop.\n"
        "2. Find the folder with `find_drive_folder` (a similarity search). Use its `match`. If there is "
        "no match, create the folder with `create_drive_folder`, using the name the user gave.\n"
        "3. Call `move_drive_file` with the file's id and the folder's id.\n"
        "Reply with the file name, the folder name (saying if you created it), and the file's link. "
        "If a tool returns \"status\": \"error\", tell the user what failed in plain words."
    ),
    tools=[search_drive_files, find_drive_folder, create_drive_folder, move_drive_file],
    on_tool_error_callback=report_tool_error,
)


workspace_agent = Workflow(
    name="workspace_agent",
    description="An agent that can read and write to files in Google Drive or Google Workspace.",
    edges=[
        (START, router_agent, router),
        (
            router,
            {
                "DOCUMENT_CREATION": content_agent,
                "FOLDER_CREATION": folder_creation_agent,
                "FOLDER_DELETION": folder_lookup_agent,
                "DOCUMENT_MOVEMENT": document_movement_agent,
                "UNKNOWN": ask_for_clarification,
            },
        ),
        (content_agent, find_folder, confirm_folder),
        (confirm_folder, {"CREATE": document_creation_agent, "CANCEL": reply_cancelled}),
        (folder_lookup_agent, confirm_folder_deletion),
        (confirm_folder_deletion, {"DELETE": trash_folder, "CANCEL": reply_kept, "NOT_FOUND": reply_folder_not_found}),
    ],
)
