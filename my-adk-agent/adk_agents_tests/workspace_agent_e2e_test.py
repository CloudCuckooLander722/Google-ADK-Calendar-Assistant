"""
End-to-end runs of adk_agents/workspace_agent.py through a real ADK Runner,
with no Gemini and no Google account (see adk_scripted.py).

Every LlmAgent gets a scripted model; Google Drive and Slides are replaced by
FakeDrive, which holds a few existing folders and records every file and folder
created. Covers AGENTS.md 1.32's DOCUMENT_CREATION cases:

    draft (content_agent) -> similarity search over Drive folders (find_folder)
    -> ask the user: which folder, and include the answer key? (confirm_folder)
    -> create the Doc / deck / PDF / infographic there (document_creation_agent)

and checks that nothing is written to Drive before the user answers.

    cd my-adk-agent && ../.venv/bin/python -m pytest adk_agents_tests/workspace_agent_e2e_test.py -v
"""
import json
import os
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("ENVIRONMENT", "development")
if not os.environ.get("GOOGLE_CREDS_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["GOOGLE_CREDS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from adk_scripted import Call, FakeDrive, Reply, Session, final_text, install_scripts, pending_question

import adk_agents.workspace_agent as ws
import tools.workspace_tools as workspace_tools

@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(workspace_tools, "get_drive_service", lambda: fake)
    monkeypatch.setattr(workspace_tools, "get_slides_service", lambda: fake)
    return fake


@pytest.fixture
def script(monkeypatch):
    return lambda **scripts: install_scripts(monkeypatch, ws.workspace_agent, **scripts)


# --- Requests and drafts ----------------------------------------------------------------

QUIZ = "I want a quiz on curveball related rates problems that test my concepts"
QUIZ_DRAFT = {
    "title": "Curveball Related Rates Quiz",
    "content": "## Questions\n1. A ladder slides down a wall at 2 ft/s...\n2. A cone drains at 3 cm^3/s...",
    "works_cited": None,
    "file_type": "DOC",
    "folder_topic": "Calculus I",
    "answer_key": "1. 1.5 ft/s\n2. -0.06 cm/s",
}


def _router(category, request, confidence=0.95):
    return [Reply({"category": category, "confidence": confidence, "request": request})]


def _start_quiz(script, drafts=(QUIZ_DRAFT,)):
    models = script(workspace_router_agent=_router("DOCUMENT_CREATION", QUIZ),
                    content_agent=[Reply(d) for d in drafts])
    chat = Session(ws.workspace_agent)
    return models, chat, pending_question(chat.say(QUIZ))


# --- DOCUMENT_CREATION -------------------------------------------------------------------

def test_quiz_asks_before_writing_anything(script, drive):
    models, chat, question = _start_quiz(script)

    assert question and question[0] == "CONFIRM_FOLDER", "should stop and ask where to save it"
    assert drive.created() == [], "nothing may be written to Drive before the user answers"
    assert QUIZ in models["content_agent"].requests[0], "content_agent gets the restated request"
    message = question[1].get("message", "") or json.dumps(question[1])
    assert "Calculus I" in json.dumps(question[1]), "suggests the matching folder"
    assert "answer key" in message.lower(), "asks about the answer key (AGENTS.md)"


def test_quiz_saved_in_the_matched_folder_with_answer_key(script, drive):
    _, chat, _ = _start_quiz(script)
    events = chat.answer("CONFIRM_FOLDER", {"action": "USE_SUGGESTED", "include_answer_key": True})

    assert pending_question(events) is None
    [doc] = drive.created()
    assert doc["mimeType"] == "application/vnd.google-apps.document"
    assert doc["name"] == "Curveball Related Rates Quiz"
    assert doc["parents"] == [drive.folder_named("Calculus I")["id"]], "saved in the matched folder"
    assert "## Answer Key" in drive.text(doc) and "1.5 ft/s" in drive.text(doc)
    assert "Created" in final_text(events) and "Calculus I" in final_text(events)
    assert chat.state["created_file"]["id"] == doc["id"]


def test_answer_key_left_out_when_declined(script, drive):
    _, chat, _ = _start_quiz(script)
    chat.answer("CONFIRM_FOLDER", {"action": "USE_SUGGESTED", "include_answer_key": False})
    [doc] = drive.created()
    assert "Answer Key" not in drive.text(doc) and "1.5 ft/s" not in drive.text(doc)


def test_no_matching_folder_creates_one(script, drive):
    draft = dict(QUIZ_DRAFT, folder_topic="Organic Chemistry Labs")
    _, chat, question = _start_quiz(script, drafts=(draft,))
    assert "didn't find a matching folder" in question[1].get("message", "")

    chat.answer("CONFIRM_FOLDER", {"action": "CREATE_NEW"})
    [folder] = drive.created("folder")
    [doc] = drive.created("file")
    assert folder["name"] == "Organic Chemistry Labs"
    assert doc["parents"] == [folder["id"]], "the document goes in the new folder"


def test_user_can_pick_another_existing_folder(script, drive):
    _, chat, _ = _start_quiz(script)
    chat.answer("CONFIRM_FOLDER", {"action": "USE_OTHER", "folder_name": "ap chemistry"})
    assert drive.created("folder") == [], "an existing folder is reused, not created again"
    [doc] = drive.created("file")
    assert doc["parents"] == [drive.folder_named("AP Chemistry")["id"]]


def test_user_can_name_a_folder_that_does_not_exist_yet(script, drive):
    _, chat, _ = _start_quiz(script)
    chat.answer("CONFIRM_FOLDER", {"action": "USE_OTHER", "folder_name": "Midterm Review"})
    [folder] = drive.created("folder")
    assert folder["name"] == "Midterm Review"
    assert drive.created("file")[0]["parents"] == [folder["id"]]


def test_no_folder_saves_in_my_drive(script, drive):
    _, chat, _ = _start_quiz(script)
    chat.answer("CONFIRM_FOLDER", {"action": "NO_FOLDER"})
    [doc] = drive.created()
    assert doc["parents"] == []


def test_cancel_creates_nothing(script, drive):
    _, chat, _ = _start_quiz(script)
    events = chat.answer("CONFIRM_FOLDER", {"action": "CANCEL"})
    assert drive.created() == []
    assert "didn't create anything" in final_text(events)


@pytest.mark.parametrize("file_type, request_text, mime", [
    ("SLIDES", "Summarize this report into a slidedeck.", "application/vnd.google-apps.presentation"),
    ("PDF", "Create a PDF for me on related rates", "application/pdf"),
    ("INFOGRAPHIC", "Create an infographic on related rates", "image/png"),
])
def test_other_file_types(script, drive, file_type, request_text, mime):
    draft = dict(QUIZ_DRAFT, file_type=file_type, answer_key=None,
                 content="## Ladder\n- 2 ft/s\n## Cone\n- 3 cm^3/s")
    script(workspace_router_agent=_router("DOCUMENT_CREATION", request_text), content_agent=[Reply(draft)])
    chat = Session(ws.workspace_agent)
    question = pending_question(chat.say(request_text))
    assert question and question[0] == "CONFIRM_FOLDER"
    assert "no answer key" in question[1].get("message", "").lower()

    chat.answer("CONFIRM_FOLDER", {"action": "USE_SUGGESTED"})
    [made] = drive.created()
    assert made["mimeType"] == mime
    assert made["parents"] == [drive.folder_named("Calculus I")["id"]]
    if file_type == "SLIDES":
        assert any("createSlide" in r for r in drive.slide_requests)


def test_drive_error_is_reported_not_crashed(script, drive, monkeypatch):
    _, chat, _ = _start_quiz(script)
    monkeypatch.setattr(workspace_tools, "get_drive_service", lambda: None)  # signed out mid-way
    events = chat.answer("CONFIRM_FOLDER", {"action": "USE_SUGGESTED"})
    assert "couldn't create" in final_text(events) and "Not signed in" in final_text(events)


# --- Other routes --------------------------------------------------------------------------

def test_calendar_request_gets_a_clarifying_reply(script, drive):
    script(workspace_router_agent=_router("UNKNOWN", "Schedule an appointment with Dr. Chong"))
    events = Session(ws.workspace_agent).say("Schedule an appointment with Dr. Chong")
    assert pending_question(events) is None
    assert "Google Drive" in final_text(events)
    assert drive.created() == []


# --- Reading a source document ("Summarize this report into a slidedeck") ------------------------

def test_slides_from_a_report_reads_the_document_first(script, drive):
    report = "Summarize Document A into a slidedeck"
    draft = dict(QUIZ_DRAFT, file_type="SLIDES", answer_key=None, title="Document A Summary",
                 content="## Plans\n- review 10.1\n- review 10.2\n- practice test")
    models = script(
        workspace_router_agent=_router("DOCUMENT_CREATION", report),
        content_agent=[Call("search_drive_files", name="Document A"),
                       Call("read_drive_file", file_id="doc1"),
                       Reply(draft)],
    )
    chat = Session(ws.workspace_agent)
    question = pending_question(chat.say(report))
    assert question and question[0] == "CONFIRM_FOLDER"
    assert "Plan 2: review 10.2" in models["content_agent"].requests[-1], "the model saw Document A's text"
    chat.answer("CONFIRM_FOLDER", {"action": "USE_SUGGESTED"})
    [deck] = drive.created()
    assert deck["mimeType"] == "application/vnd.google-apps.presentation"


# --- FOLDER_CREATION -------------------------------------------------------------------------------

def test_folder_creation_creates_the_folder(script, drive):
    script(
        workspace_router_agent=_router("FOLDER_CREATION", "Make a folder called Chem Labs"),
        folder_creation_agent=[Call("find_drive_folder", name="Chem Labs"),
                               Call("create_drive_folder", name="Chem Labs"),
                               "Created the folder Chem Labs: https://drive.example/file5"],
    )
    events = Session(ws.workspace_agent).say("Make a folder called Chem Labs")
    assert [f["name"] for f in drive.created("folder")] == ["Chem Labs"]
    assert "Chem Labs" in final_text(events)


def test_find_drive_folder_does_not_match_a_different_subject(drive):
    """The folder_creation_agent's check: 'Chem Labs' is not 'AP Chemistry', so it gets created."""
    assert workspace_tools.find_drive_folder("Chem Labs")["match"] is None
    assert workspace_tools.find_drive_folder("calc 1")["match"]["name"] == "Calculus I"


# --- FOLDER_DELETION -------------------------------------------------------------------------------

def _start_deletion(script, found=True):
    lookup = ({"found": True, "folder_id": "folder1", "folder_name": "AP Chemistry", "note": ""} if found
              else {"found": False, "note": "Did you mean AP Chemistry or Calculus I?"})
    script(
        workspace_router_agent=_router("FOLDER_DELETION", "Delete my AP Chem folder"),
        folder_lookup_agent=[Call("find_drive_folder", name="AP Chem"), Reply(lookup)],
    )
    chat = Session(ws.workspace_agent)
    return chat, chat.say("Delete my AP Chem folder")


def test_folder_deletion_asks_then_trashes(script, drive):
    chat, events = _start_deletion(script)
    question = pending_question(events)
    assert question and question[0] == "CONFIRM_DELETE"
    assert "AP Chemistry" in question[1].get("message", "") and "trash" in question[1].get("message", "")
    assert drive.trashed == [], "nothing is deleted before the user says yes"

    events = chat.answer("CONFIRM_DELETE", {"approve": True})
    assert drive.trashed == ["folder1"]
    assert "trash" in final_text(events)


def test_folder_deletion_declined_keeps_the_folder(script, drive):
    chat, _ = _start_deletion(script)
    events = chat.answer("CONFIRM_DELETE", {"approve": False})
    assert drive.trashed == []
    assert "kept the folder" in final_text(events)


def test_folder_deletion_without_a_clear_match_deletes_nothing(script, drive):
    _, events = _start_deletion(script, found=False)
    assert pending_question(events) is None
    assert drive.trashed == []
    assert "AP Chemistry or Calculus I" in final_text(events)


# --- DOCUMENT_MOVEMENT -----------------------------------------------------------------------------

MOVE = 'Move this PDF to drive folder "Atlantic Salmon Research Project"'


def test_move_pdf_into_the_matched_folder(script, drive):
    script(
        workspace_router_agent=_router("DOCUMENT_MOVEMENT", MOVE),
        document_movement_agent=[Call("search_drive_files", name="Salmon"),
                                 Call("find_drive_folder", name="Atlantic Salmon Research Project"),
                                 Call("move_drive_file", file_id="doc0", folder_id="folder2"),
                                 "Moved Salmon Survey Results.pdf to Atlantic Salmon Research Project."],
    )
    events = Session(ws.workspace_agent).say(MOVE)
    pdf = drive.file_named("Salmon Survey Results.pdf")
    assert pdf["parents"] == [drive.folder_named("Atlantic Salmon Research Project")["id"]]
    assert drive.created("folder") == []
    assert "Moved" in final_text(events)


def test_move_creates_the_folder_when_missing(script, drive):
    script(
        workspace_router_agent=_router("DOCUMENT_MOVEMENT", 'Move the salmon PDF to "Fisheries 2026"'),
        document_movement_agent=[Call("search_drive_files", name="Salmon"),
                                 Call("find_drive_folder", name="Fisheries 2026"),
                                 Call("create_drive_folder", name="Fisheries 2026"),
                                 Call("move_drive_file", file_id="doc0", folder_id="file5"),
                                 "Created Fisheries 2026 and moved the PDF there."],
    )
    Session(ws.workspace_agent).say('Move the salmon PDF to "Fisheries 2026"')
    [folder] = drive.created("folder")
    assert folder["name"] == "Fisheries 2026"
    assert drive.file_named("Salmon Survey Results.pdf")["parents"] == [folder["id"]]
