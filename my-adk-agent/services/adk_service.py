import streamlit as st
import asyncio
import logging
import time
import os
import threading
import requests
from dataclasses import dataclass, field
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.genai import types as genai_types

import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)
    
from agents.agent import root_agent
from config.settings import APP_NAME_FOR_ADK, INITIAL_STATE


# File types each add_col upload mode accepts, mapped to their official API MIME type.
# The chat UI passes these extension lists to its widgets, so the two always agree.
SCREENSHOT_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
# PDFs go to Gemini as bytes; text/* documents are decoded and sent as text.
DOCUMENT_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
}
RECORDING_MIME_TYPES = {".wav": "audio/wav", ".mp3": "audio/mp3"}

# Google Docs, Sheets and Slides have no file of their own to download: Drive
# exports them to a format Gemini reads. Sheets export only the first sheet.
DRIVE_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "application/pdf",
    "application/vnd.google-apps.presentation": "application/pdf",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
# Everything the Google Drive picker lets the user choose.
DRIVE_MIME_TYPES = sorted({
    *SCREENSHOT_MIME_TYPES.values(), *DOCUMENT_MIME_TYPES.values(), *DRIVE_EXPORT_MIME_TYPES,
})
DRIVE_API_URL = "https://www.googleapis.com/drive/v3/files"

# Gemini rejects requests whose inline data is larger than ~20 MB in total.
MAX_INLINE_BYTES = 20 * 1024 * 1024


def _read_attachment(attachment) -> tuple[str, bytes]:
    """
    Returns (file name, bytes) for one attachment. Accepts a Streamlit UploadedFile
    (from st.file_uploader / st.audio_input), a (name, bytes) tuple, or a file path.
    """
    if isinstance(attachment, tuple):
        name, data = attachment
        return name, data
    if hasattr(attachment, "getvalue"):
        return attachment.name, attachment.getvalue()
    path = Path(attachment)
    return path.name, path.read_bytes()


def _read_drive_file(file) -> tuple[str, str, bytes]:
    """
    Returns (file name, MIME type, bytes) for one file picked with
    streamlit_google_picker, downloading it with the picker's access token.
    """
    size = int(file.size or 0)
    if size > MAX_INLINE_BYTES:
        raise ValueError(
            f"{file.name} is {size / 1024 / 1024:.1f} MB; the limit is "
            f"{MAX_INLINE_BYTES // 1024 // 1024} MB per message."
        )

    export_mime_type = DRIVE_EXPORT_MIME_TYPES.get(file.type)
    if export_mime_type:
        mime_type = export_mime_type
        url, params = f"{DRIVE_API_URL}/{file.id}/export", {"mimeType": export_mime_type}
    else:
        # Drive sometimes reports text files as application/octet-stream, so
        # fall back to the extension, the same way uploaded files are checked.
        mime_type = file.type if file.type in DRIVE_MIME_TYPES else _mime_type_for(
            file.name, {**SCREENSHOT_MIME_TYPES, **DOCUMENT_MIME_TYPES}, "Google Drive file")
        url, params = f"{DRIVE_API_URL}/{file.id}", {"alt": "media"}

    try:
        response = requests.get(
            url, params=params, headers={"Authorization": f"Bearer {file.token}"}, timeout=60,
        )
        response.raise_for_status()
    except requests.RequestException:
        logging.exception("Failed to download Google Drive file %s", file.id)
        raise ValueError(f"{file.name}: couldn't download it from Google Drive. Pick it again and retry.")
    return file.name, mime_type, response.content


def _document_part(label: str, name: str, mime_type: str, data: bytes) -> genai_types.Part:
    """PDFs and images go to Gemini as bytes; text/* is decoded and labeled so the
    agent can tell it apart from the user's own prompt."""
    if mime_type.startswith("text/"):
        text = data.decode("utf-8", errors="replace")
        return genai_types.Part(text=f"[{label}: {name}]\n{text}")
    return genai_types.Part.from_bytes(data=data, mime_type=mime_type)


def _mime_type_for(name: str, allowed: dict[str, str], kind: str) -> str:
    ext = Path(name).suffix.lower()
    mime_type = allowed.get(ext)
    if mime_type is None:
        raise ValueError(f"{name}: unsupported {kind} format. Use {', '.join(sorted(allowed))}.")
    return mime_type


def content(prompt: str | None = None,
            speech_to_text: str | None = None,
            screenshots=None,
            documents=None,
            recording=None,
            google_drive: list | None = None) -> genai_types.Content:
    """
    Builds one user types.Content for run_adk_sync() from whichever inputs are given.

    Arguments (all optional; only the ones provided become Parts, in this order):
        prompt:         the typed chat text.
        speech_to_text: a transcript of something the user said.
        screenshots:    list of images (.jpg/.jpeg/.png/.webp).
        documents:      list of documents (.pdf as bytes; .txt/.md/.csv as text).
        recording:      one audio clip (.wav from st.audio_input, or .mp3).
        google_drive:   list of files picked with streamlit_google_picker (images,
                        documents, and Google Docs/Sheets/Slides, which are exported).
    Each uploaded file can be a Streamlit UploadedFile, a (name, bytes) tuple, or a file path.
    Raises ValueError with a user-readable message if a file is unsupported, the
    attachments are too large, or there is nothing to send.
    """
    parts = []

    # Text-based inputs. Skip empty/whitespace-only strings.
    if prompt and prompt.strip():
        parts.append(genai_types.Part(text=prompt))
    if speech_to_text and speech_to_text.strip():
        parts.append(genai_types.Part(text=speech_to_text))

    #you pass in a list of documents and then shove them into a types.Part in a types.Content
    for document in documents or []:
        name, data = _read_attachment(document)
        mime_type = _mime_type_for(name, DOCUMENT_MIME_TYPES, "document")
        parts.append(_document_part("Document", name, mime_type, data))

    for file in google_drive or []:
        parts.append(_document_part("Google Drive file", *_read_drive_file(file)))

    for screenshot in screenshots or []:
        name, data = _read_attachment(screenshot)
        mime_type = _mime_type_for(name, SCREENSHOT_MIME_TYPES, "screenshot")
        parts.append(genai_types.Part.from_bytes(data=data, mime_type=mime_type))

    if recording is not None:
        name, data = _read_attachment(recording)
        mime_type = _mime_type_for(name, RECORDING_MIME_TYPES, "recording")
        parts.append(genai_types.Part.from_bytes(data=data, mime_type=mime_type))

    if not parts:
        raise ValueError("Nothing to send: add a prompt, screenshot, document, recording, or Google Drive file.")

    inline_bytes = sum(len(part.inline_data.data) for part in parts if part.inline_data)
    if inline_bytes > MAX_INLINE_BYTES:
        raise ValueError(
            f"Attachments are {inline_bytes / 1024 / 1024:.1f} MB; the limit is "
            f"{MAX_INLINE_BYTES // 1024 // 1024} MB per message. Remove a file and try again."
        )

    return genai_types.Content(role="user", parts=parts)


@st.cache_resource
def initialize_adk() -> Runner:
    """
    The ADK Runner and its in-memory session service. st.cache_resource builds
    them once per server process and shares them with every browser session,
    so a conversation's history survives page refreshes. Per-user state lives
    in the sessions inside it (see get_conversation), never in the cached object
    itself.
    """
    session_service = InMemorySessionService() # ADK's default in-memory session service for storing session data.
    return Runner( # The ADK Runner orchestrates the agent's execution.
        agent=root_agent,
        app_name=APP_NAME_FOR_ADK,
        session_service=session_service,
        auto_create_session=True
    )


@dataclass
class Conversation:
    """One signed-in user's chat: the ADK session holding the agent's context,
    the messages shown in the chat panel, and a counter bumped whenever the
    agent may have changed their calendar (it keys the calendar cache)."""
    session_id: str
    messages: list[dict] = field(default_factory=list)
    calendar_version: int = 0


@st.cache_resource
def _conversation_store() -> tuple[dict[str, Conversation], threading.Lock]:
    """Every user's Conversation, keyed by their Google account id. Cached like
    the runner, so it outlives each browser session (and therefore refreshes)."""
    return {}, threading.Lock()


def find_conversation(user_key: str) -> Conversation | None:
    """The user's conversation if they have one, without creating it."""
    conversations, _ = _conversation_store()
    return conversations.get(user_key)


def get_conversation(runner: Runner, user_key: str) -> Conversation:
    """
    The user's conversation, created on first use. Also recreates its ADK
    session if the session service lost it (e.g. after st.cache_resource.clear()).
    """
    conversations, lock = _conversation_store()
    with lock:
        conversation = conversations.get(user_key)
        if conversation is None:
            conversation = Conversation(session_id=f"chat_{int(time.time())}_{os.urandom(4).hex()}")
            conversations[user_key] = conversation

    # get_session/create_session are async - they must be awaited (via
    # asyncio.run here); a bare coroutine object is always truthy.
    session = asyncio.run(runner.session_service.get_session(
        app_name=APP_NAME_FOR_ADK, user_id=user_key, session_id=conversation.session_id,
    ))
    if not session:
        asyncio.run(runner.session_service.create_session(
            app_name=APP_NAME_FOR_ADK,
            user_id=user_key,
            session_id=conversation.session_id,
            state=INITIAL_STATE, # Initialize with predefined state.
        ))
    return conversation

async def run_adk_async(runner: Runner, user_key: str, session_id: str, content: genai_types.Content | str):
    """
    Asynchronously runs a single turn of the ADK agent conversation.
    `content` is normally built by content(); a plain string is wrapped into a text-only Content.
    """
    if isinstance(content, str):
        content = genai_types.Content(role='user', parts=[genai_types.Part(text=content)]) if content.strip() else None
    if not content or not content.parts:
        return "I didn't receive any message to respond to."

    try:
        session = await runner.session_service.get_session(app_name=APP_NAME_FOR_ADK, user_id=user_key, session_id=session_id)
    except Exception:
        logging.exception("Failed to look up ADK session %s", session_id)
        return "Something went wrong loading your session. Please refresh and try again."

    if not session:
        return "Error: ADK session not found."

    # Prepare the user's message in the format expected by ADK/Gemini.
    final_response_text = "[Agent encountered an issue]" # Default error message
    try:
        # Iterate through the asynchronous events generated by the ADK runner.
        # ADK can yield multiple events (e.g., tool calls, interim responses) before the final response.
        async for event in runner.run_async(user_id=user_key, session_id=session_id, new_message=content):
            if event.is_final_response(): # We are only interested in the final response from the agent.
                if event.content and event.content.parts:
                    # With include_thoughts=True, Gemini can return the model's
                    # reasoning as a separate Part with `thought=True` ahead of
                    # the actual answer - skip those, only join real answer text.
                    answer_parts = [
                        part.text
                        for part in event.content.parts
                        if getattr(part, "text", None) and not getattr(part, "thought", False)
                    ]
                    if answer_parts:
                        final_response_text = "".join(answer_parts)
                # No `break`: let the runner finish. Abandoning its generator
                # mid-stream makes OpenTelemetry log "Failed to detach context"
                # tracebacks when the generator is closed later.
    except Exception:
        # A tool call, model request, or anything else in the agent pipeline
        # can raise here - never let that crash the whole chat turn.
        logging.exception("ADK agent run failed for session %s", session_id)
        return "Sorry, I ran into an unexpected error handling that request. Please try again."

    return final_response_text or "[Agent returned an empty response]"

def run_adk_sync(runner: Runner, user_key: str, session_id: str, content: genai_types.Content | str):
    """
    Synchronous wrapper for running ADK, as Streamlit does not directly support async calls in the main thread.
    """
    try:
        # Runs the asynchronous function in a new event loop.
        return asyncio.run(run_adk_async(runner, user_key, session_id, content))
    except Exception:
        logging.exception("run_adk_sync failed for session %s", session_id)
        return "Sorry, I ran into an unexpected error handling that request. Please try again."

