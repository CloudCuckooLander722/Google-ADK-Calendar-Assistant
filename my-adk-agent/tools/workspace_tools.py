import io
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload


MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)

from services.api_services import get_drive_access_token, get_drive_service, get_slides_service


# Every routine returns the same shape, so the agent can report and file it:
#   {"id", "name", "mimeType", "webViewLink"}
# folder_id puts the file in that Drive folder; None leaves it in My Drive.
# The app's OAuth scope is drive.file, which covers files the app creates itself.

GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
PDF = "application/pdf"
PNG = "image/png"
_FILE_FIELDS = "id, name, mimeType, webViewLink"


# --- Shared helpers ------------------------------------------------------------------

def _drive():
    service = get_drive_service()
    if service is None:
        raise ValueError("Not signed in to Google Drive. Please log in again.")
    return service


def _slides():
    service = get_slides_service()
    if service is None:
        raise ValueError("Not signed in to Google Slides. Please log in again.")
    return service


def _title(title: Optional[str], fallback: str) -> str:
    title = (title or "").strip().strip('"').strip()
    return title or fallback


def _with_works_cited(content: str, works_cited: Optional[str]) -> str:
    """The Markdown body with a 'Works Cited' section appended, if there are any sources."""
    body = (content or "").strip()
    cited = (works_cited or "").strip()
    if cited and cited not in ("[]", "None"):
        body += "\n\n## Works Cited\n\n" + cited
    return body


def _upload(name: str, data: bytes, source_mime: str, target_mime: Optional[str], folder_id: Optional[str]) -> Dict:
    """Uploads bytes to Drive; with target_mime set, Drive converts them (e.g. Markdown -> Google Doc)."""
    metadata = {"name": name}
    if target_mime:
        metadata["mimeType"] = target_mime
    if folder_id:
        metadata["parents"] = [folder_id]
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=source_mime, resumable=False)
    return _drive().files().create(body=metadata, media_body=media, fields=_FILE_FIELDS).execute()


def _plain(text: str) -> str:
    """Markdown inline formatting stripped, for places that show plain text (slides, images)."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)            # images
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)  # links -> text (url)
    text = re.sub(r"(\*\*|__|\*|_|`)", "", text)
    return text.strip()


def _sections(markdown: str) -> List[Tuple[str, List[str]]]:
    """Splits Markdown into (heading, lines) sections at #/##/### headings and '---' rules.
    Bullet and numbered-list markers are removed from each line."""
    sections: List[Tuple[str, List[str]]] = []
    heading, lines = "", []
    for raw in (markdown or "").splitlines():
        line = raw.strip()
        match = re.match(r"^#{1,3}\s+(.*)", line)
        if match or re.fullmatch(r"-{3,}|\*{3,}", line):
            if heading or lines:
                sections.append((heading, lines))
            heading, lines = (_plain(match.group(1)) if match else ""), []
            continue
        if not line:
            continue
        line = re.sub(r"^([-*+]|\d+[.)])\s+", "", line)
        lines.append(_plain(line))
    if heading or lines:
        sections.append((heading, lines))
    # A leading heading with nothing under it is the document's own title (e.g. "# Related Rates"),
    # which the title slide / banner already shows.
    if len(sections) > 1 and not sections[0][1]:
        sections = sections[1:]
    return sections


# --- Folders -------------------------------------------------------------------------------

FOLDER = "application/vnd.google-apps.folder"


def list_drive_folders(max_folders: int = 500) -> List[Dict]:
    """
    Lists the Drive folders this app can see, newest first.

    With the drive.file scope that is the folders the app created plus any the
    user picked in the Drive picker, not every folder in their Drive.

    Returns:
        [{"id", "name", "webViewLink"}, ...]
    """
    drive = _drive()
    folders, page_token = [], None
    try:
        while len(folders) < max_folders:
            result = drive.files().list(
                q=f"mimeType='{FOLDER}' and trashed=false",
                fields="nextPageToken, files(id, name, webViewLink)",
                orderBy="modifiedTime desc",
                pageSize=min(100, max_folders - len(folders)),
                pageToken=page_token,
            ).execute()
            folders += result.get("files", [])
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        return folders
    except HttpError as error:
        raise ValueError(f"Failed to list Drive folders: {error}")


def create_drive_folder(name: str, parent_id: Optional[str] = None) -> Dict:
    """
    Creates a Drive folder.

    Args:
        name: Folder name.
        parent_id: Folder to create it in, or None for My Drive.

    Returns:
        {"id", "name", "mimeType", "webViewLink"} of the new folder.
    """
    name = _title(name, "")
    if not name:
        raise ValueError("Folder name is required.")
    metadata = {"name": name, "mimeType": FOLDER}
    if parent_id:
        metadata["parents"] = [parent_id]
    try:
        return _drive().files().create(body=metadata, fields=_FILE_FIELDS).execute()
    except HttpError as error:
        raise ValueError(f"Failed to create Drive folder '{name}': {error}")


FOLDER_MATCH_THRESHOLD = 0.6
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}


def _words(text: str) -> List[str]:
    return [_ROMAN.get(w, w) for w in re.findall(r"\w+", (text or "").lower())]


def _same_word(a: str, b: str) -> bool:
    """Equal, or one abbreviates the other ('calc' / 'calculus', 'chem' / 'chemistry')."""
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= 4 and long.startswith(short)


def folder_similarity(topic: str, folder_name: str) -> float:
    """How alike a folder name is to a topic, 0-1: the share of words that match
    (Dice coefficient), so 'Calc I' ~ 'Calculus I' while 'Calculus II', or
    'Organic Chemistry Labs' vs 'AP Chemistry', stay below FOLDER_MATCH_THRESHOLD."""
    a, b = _words(topic), _words(folder_name)
    if not a or not b:
        return 0.0
    unmatched, matches = list(b), 0
    for word in a:
        hit = next((w for w in unmatched if _same_word(word, w)), None)
        if hit is not None:
            unmatched.remove(hit)
            matches += 1
    return 2 * matches / (len(a) + len(b))


def rank_folders(topic: str, folders: List[Dict]) -> Tuple[Optional[Dict], List[Dict]]:
    """(best match if it clears FOLDER_MATCH_THRESHOLD, up to 3 runners-up). Ties go to character similarity."""
    import difflib

    def score(folder):
        name = folder.get("name", "")
        return (folder_similarity(topic, name), difflib.SequenceMatcher(None, topic.lower(), name.lower()).ratio())

    ranked = sorted(folders, key=score, reverse=True)
    if ranked and folder_similarity(topic, ranked[0].get("name", "")) >= FOLDER_MATCH_THRESHOLD:
        return ranked[0], ranked[1:4]
    return None, ranked[:3]


def find_drive_folder(name: str) -> Dict:
    """
    Similarity search for a Drive folder by name (e.g. 'Atlantic Salmon Research'
    finds 'Atlantic Salmon Research Project'; 'Calc I' finds 'Calculus I').

    Args:
        name: The folder name or subject to look for.

    Returns:
        {"match": {"id", "name", "webViewLink"} or None when nothing is close enough,
         "candidates": up to 3 other close folders}
    """
    match, candidates = rank_folders(name, list_drive_folders())
    return {"match": match, "candidates": candidates}


def delete_drive_folder(folder_id: str) -> str:
    """
    Moves a Drive folder (and everything in it) to the trash. It can be restored
    from the trash for 30 days.

    Args:
        folder_id: The folder's id, from find_drive_folder or list_drive_folders.
    """
    if not (folder_id or "").strip():
        raise ValueError("folder_id is required.")
    try:
        folder = _drive().files().update(fileId=folder_id, body={"trashed": True}, fields="id, name").execute()
        return f"Moved folder '{folder.get('name', folder_id)}' to the trash."
    except HttpError as error:
        raise ValueError(f"Failed to delete folder {folder_id}: {error}")


# --- Files: find, read, move -----------------------------------------------------------------

def search_drive_files(name: str, max_results: int = 10) -> List[Dict]:
    """
    Finds Drive files (not folders) whose name contains the given text.

    With the drive.file scope this covers files the app created and files the
    user picked in the Drive picker or attached in the chat.

    Args:
        name: Text the file name contains (e.g. 'Document A', 'salmon report').
        max_results: How many files to return at most.

    Returns:
        [{"id", "name", "mimeType", "webViewLink", "parents"}, ...], most recently modified first.
    """
    query = name.replace("\\", "\\\\").replace("'", "\\'")
    try:
        result = _drive().files().list(
            q=f"name contains '{query}' and mimeType != '{FOLDER}' and trashed=false",
            fields="files(id, name, mimeType, webViewLink, parents)",
            orderBy="modifiedTime desc",
            pageSize=max(1, min(max_results, 50)),
        ).execute()
        return result.get("files", [])
    except HttpError as error:
        raise ValueError(f"Failed to search Drive files for '{name}': {error}")


_TEXT_EXPORTS = {
    GOOGLE_DOC: "text/plain",
    GOOGLE_SLIDES: "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
MAX_READ_CHARS = 100_000


def read_drive_file(file_id: str) -> Dict:
    """
    Reads a Drive file's text so its content can be summarized or turned into
    something else (e.g. 'Summarize this report into a slidedeck').
    Google Docs, Slides and Sheets are exported as text; text, Markdown and CSV
    files are downloaded. PDFs and images can't be read here: ask the user to
    attach them in the chat instead.

    Args:
        file_id: The file's id, from search_drive_files.

    Returns:
        {"name", "mimeType", "text", "truncated"}
    """
    if not (file_id or "").strip():
        raise ValueError("file_id is required.")
    drive = _drive()
    try:
        meta = drive.files().get(fileId=file_id, fields="id, name, mimeType").execute()
        mime = meta.get("mimeType", "")
        if mime in _TEXT_EXPORTS:
            data = drive.files().export(fileId=file_id, mimeType=_TEXT_EXPORTS[mime]).execute()
        elif mime.startswith("text/") or mime in ("application/json",):
            data = drive.files().get_media(fileId=file_id).execute()
        else:
            raise ValueError(
                f"'{meta.get('name')}' is a {mime or 'binary'} file, which can't be read as text here. "
                "Ask the user to attach it in the chat.")
    except HttpError as error:
        raise ValueError(f"Failed to read file {file_id}: {error}")
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    return {"name": meta.get("name"), "mimeType": mime, "text": text[:MAX_READ_CHARS],
            "truncated": len(text) > MAX_READ_CHARS}


def move_drive_file(file_id: str, folder_id: str) -> Dict:
    """
    Moves a Drive file into a folder (out of the folders it was in).

    Args:
        file_id: The file's id, from search_drive_files.
        folder_id: The destination folder's id, from find_drive_folder or create_drive_folder.

    Returns:
        {"id", "name", "mimeType", "webViewLink"} of the moved file.
    """
    if not (file_id or "").strip() or not (folder_id or "").strip():
        raise ValueError("file_id and folder_id are required.")
    drive = _drive()
    try:
        current = drive.files().get(fileId=file_id, fields="parents").execute()
        return drive.files().update(
            fileId=file_id,
            addParents=folder_id,
            removeParents=",".join(current.get("parents", [])),
            fields=_FILE_FIELDS,
        ).execute()
    except HttpError as error:
        raise ValueError(f"Failed to move file {file_id} to folder {folder_id}: {error}")


# --- Google Doc --------------------------------------------------------------------------

def _create_document(
    content: str,
    works_cited: Optional[str] = None,
    title: Optional[str] = None,
    folder_id: Optional[str] = None,
) -> Dict:
    """
    Creates a Google Doc from Markdown content (headings, lists, bold, links and
    tables keep their formatting: Drive converts the Markdown on upload), with a
    Works Cited section at the end when sources are given.

    Args:
        content: Body of the document in Markdown, without the title.
        works_cited: Citations / source URLs, one per line, or None.
        title: Document name in Drive.
        folder_id: Drive folder to create it in, or None for My Drive.

    Returns:
        {"id", "name", "mimeType", "webViewLink"} of the new Doc.
    """
    if not (content or "").strip():
        raise ValueError("Document content is required.")
    name = _title(title, "Untitled document")
    body = _with_works_cited(content, works_cited)
    try:
        return _upload(name, body.encode("utf-8"), "text/markdown", GOOGLE_DOC, folder_id)
    except HttpError as error:
        raise ValueError(f"Failed to create Google Doc '{name}': {error}")


# --- Google Slides -------------------------------------------------------------------------

MAX_LINES_PER_SLIDE = 7


def _slide_pages(title: str, content: str, works_cited: Optional[str]) -> List[Tuple[str, List[str]]]:
    """(slide title, bullet lines) for every slide after the title slide.
    A section longer than MAX_LINES_PER_SLIDE continues on a '(cont.)' slide."""
    pages = []
    for heading, lines in _sections(content) or [(title, [])]:
        heading = heading or title
        chunks = [lines[i:i + MAX_LINES_PER_SLIDE] for i in range(0, len(lines), MAX_LINES_PER_SLIDE)] or [[]]
        for n, chunk in enumerate(chunks):
            pages.append((heading if n == 0 else f"{heading} (cont.)", chunk))
    cited = [_plain(line) for line in (works_cited or "").splitlines() if line.strip()]
    if cited and cited != ["[]"]:
        for i in range(0, len(cited), MAX_LINES_PER_SLIDE):
            pages.append(("Works Cited" if i == 0 else "Works Cited (cont.)", cited[i:i + MAX_LINES_PER_SLIDE]))
    return pages


def _slide_requests(slide_title: str, lines: List[str], layout: str) -> List[Dict]:
    """batchUpdate requests that add one slide and fill its title and body placeholders."""
    slide_id, title_id, body_id = (f"s_{uuid.uuid4().hex[:12]}" for _ in range(3))
    body_type = "SUBTITLE" if layout == "TITLE" else "BODY"
    requests = [{
        "createSlide": {
            "objectId": slide_id,
            "slideLayoutReference": {"predefinedLayout": layout},
            "placeholderIdMappings": [
                {"layoutPlaceholder": {"type": "CENTERED_TITLE" if layout == "TITLE" else "TITLE"}, "objectId": title_id},
                {"layoutPlaceholder": {"type": body_type}, "objectId": body_id},
            ],
        }
    }, {"insertText": {"objectId": title_id, "text": slide_title or " "}}]
    if lines:
        requests.append({"insertText": {"objectId": body_id, "text": "\n".join(lines)}})
        if layout != "TITLE":
            requests.append({"createParagraphBullets": {
                "objectId": body_id,
                "textRange": {"type": "ALL"},
                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
            }})
    return requests


def _create_slidedeck(
    content: str,
    works_cited: Optional[str] = None,
    title: Optional[str] = None,
    folder_id: Optional[str] = None,
) -> Dict:
    """
    Creates a Google Slides deck from Markdown content: a title slide, then one
    slide per #/## heading (or '---' break) with that section's lines as
    bullets, then Works Cited slides when sources are given.

    Args:
        content: The deck's content in Markdown; each heading starts a slide.
        works_cited: Citations / source URLs, one per line, or None.
        title: Deck name in Drive, also used on the title slide.
        folder_id: Drive folder to put it in, or None for My Drive.

    Returns:
        {"id", "name", "mimeType", "webViewLink"} of the new presentation.
    """
    if not (content or "").strip():
        raise ValueError("Slide deck content is required.")
    name = _title(title, "Untitled presentation")
    try:
        # Create it through Drive so it lands in the folder, then fill it through Slides.
        metadata = {"name": name, "mimeType": GOOGLE_SLIDES}
        if folder_id:
            metadata["parents"] = [folder_id]
        created = _drive().files().create(body=metadata, fields=_FILE_FIELDS).execute()
        presentation_id = created["id"]

        slides = _slides()
        blank = slides.presentations().get(presentationId=presentation_id, fields="slides.objectId").execute()
        requests = [{"deleteObject": {"objectId": s["objectId"]}} for s in blank.get("slides", [])]
        requests += _slide_requests(name, [], "TITLE")
        for slide_title, lines in _slide_pages(name, content, works_cited):
            requests += _slide_requests(slide_title, lines, "TITLE_AND_BODY")
        slides.presentations().batchUpdate(presentationId=presentation_id, body={"requests": requests}).execute()
        return created
    except HttpError as error:
        raise ValueError(f"Failed to create slide deck '{name}': {error}")


# --- PDF -------------------------------------------------------------------------------------

def _create_pdf(
    content: str,
    works_cited: Optional[str] = None,
    title: Optional[str] = None,
    folder_id: Optional[str] = None,
) -> Dict:
    """
    Creates a PDF from Markdown content. Drive builds it: the Markdown becomes a
    temporary Google Doc, which is exported as PDF and then deleted, so the
    PDF keeps the Doc's formatting and needs no PDF library here.

    Args:
        content: Body of the PDF in Markdown, without the title.
        works_cited: Citations / source URLs, one per line, or None.
        title: File name in Drive (".pdf" is added if missing).
        folder_id: Drive folder to put it in, or None for My Drive.

    Returns:
        {"id", "name", "mimeType", "webViewLink"} of the new PDF.
    """
    if not (content or "").strip():
        raise ValueError("PDF content is required.")
    name = _title(title, "Untitled document")
    pdf_name = name if name.lower().endswith(".pdf") else f"{name}.pdf"
    drive = _drive()
    temp_doc = None
    try:
        temp_doc = _upload(f"{name} (PDF draft)", _with_works_cited(content, works_cited).encode("utf-8"),
                           "text/markdown", GOOGLE_DOC, None)
        pdf_bytes = drive.files().export(fileId=temp_doc["id"], mimeType=PDF).execute()
        return _upload(pdf_name, pdf_bytes, PDF, None, folder_id)
    except HttpError as error:
        raise ValueError(f"Failed to create PDF '{pdf_name}': {error}")
    finally:
        if temp_doc:
            try:
                drive.files().delete(fileId=temp_doc["id"]).execute()
            except HttpError:
                logging.warning("Could not delete the temporary Doc %s used for %s", temp_doc.get("id"), pdf_name)


# --- Infographic -------------------------------------------------------------------------------

INFOGRAPHIC_WIDTH = 1080
_PALETTE = ["#2563EB", "#059669", "#D97706", "#DC2626", "#7C3AED", "#0891B2"]

# Plain-text stand-ins for characters Pillow's built-in font can't draw.
_FALLBACKS = {
    "⁰": "^0", "¹": "^1", "²": "^2", "³": "^3", "⁴": "^4", "⁵": "^5", "⁶": "^6", "⁷": "^7", "⁸": "^8", "⁹": "^9",
    "₀": "_0", "₁": "_1", "₂": "_2", "₃": "_3", "₄": "_4",
    "“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "…": "...", "•": "-",
    "×": "x", "÷": "/", "≤": "<=", "≥": ">=", "≠": "!=", "≈": "~", "±": "+/-", "√": "sqrt", "∞": "inf",
    "π": "pi ", "θ": "theta ", "Δ": "delta ", "Σ": "sum", "∫": "integral", "°": " deg", "→": "->",
}


def _drawable(text: str, font) -> str:
    """Replaces characters the font has no glyph for (they would render as empty boxes)."""
    import unicodedata
    from PIL import Image, ImageDraw

    def glyph(ch: str) -> bytes:
        canvas = Image.new("L", (int(font.size * 2), int(font.size * 2)))
        ImageDraw.Draw(canvas).text((0, 0), ch, font=font, fill=255)
        return canvas.tobytes()

    missing = glyph("￿")  # what the font draws for a character it doesn't have
    out = []
    for ch in text:
        if ch.isascii() or glyph(ch) != missing:
            out.append(ch)
        elif ch in _FALLBACKS:
            out.append(_FALLBACKS[ch])
        else:
            ascii_form = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
            out.append(ascii_form or "?")
    return "".join(out)


def _render_infographic(title: str, content: str, works_cited: Optional[str]) -> bytes:
    """Draws a tall PNG: a title banner, one coloured card per section, and a sources footer."""
    from PIL import Image, ImageDraw, ImageFont

    def font(size):
        return ImageFont.load_default(size=size)

    title_font, heading_font, body_font, small_font = font(56), font(38), font(28), font(22)
    margin, pad, gap, width = 60, 32, 36, INFOGRAPHIC_WIDTH
    inner = width - 2 * margin - 2 * pad
    indent = 30  # bullet column
    title = _drawable(title, title_font)
    content = _drawable(content, body_font)
    works_cited = _drawable(works_cited or "", small_font)

    def wrap(text, f, max_width):
        """Greedy word wrap measured in pixels."""
        words, lines, line = text.split(), [], ""
        for word in words:
            trial = f"{line} {word}".strip()
            if f.getlength(trial) <= max_width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
        return lines

    def height(f):
        top, bottom = f.getbbox("Ag")[1], f.getbbox("Ag")[3]
        return bottom - top + 12

    sections = [(h, ls) for h, ls in _sections(content) if ls] or [("", [content])]
    title_lines = wrap(title, title_font, width - 2 * margin)
    cards = []
    for i, (heading, lines) in enumerate(sections):
        head = wrap(heading, heading_font, inner) if heading else []
        # (starts a bullet point?, text): wrapped lines hang under the first one.
        body = [(n == 0, w) for line in lines for n, w in enumerate(wrap(line, body_font, inner - indent) or [""])]
        cards.append((_PALETTE[i % len(_PALETTE)], head, body,
                      2 * pad + len(head) * height(heading_font) + (16 if head and body else 0)
                      + len(body) * height(body_font)))
    sources = [_plain(s) for s in (works_cited or "").splitlines() if s.strip() and s.strip() != "[]"]
    source_lines = [w for s in sources for w in wrap(s, small_font, width - 2 * margin)]

    banner = 2 * margin + len(title_lines) * height(title_font)
    footer = (margin + height(small_font) * (len(source_lines) + 1)) if source_lines else margin
    total = banner + gap + sum(c[3] + gap for c in cards) + footer

    image = Image.new("RGB", (width, total), "#F8FAFC")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width, banner], fill="#0F172A")
    y = margin
    for line in title_lines:
        draw.text((margin, y), line, font=title_font, fill="#FFFFFF")
        y += height(title_font)

    y = banner + gap
    for colour, head, body, card_height in cards:
        draw.rounded_rectangle([margin, y, width - margin, y + card_height], radius=24, fill="#FFFFFF", outline="#E2E8F0", width=2)
        draw.rounded_rectangle([margin, y, margin + 14, y + card_height], radius=7, fill=colour)
        ty = y + pad
        for line in head:
            draw.text((margin + pad, ty), line, font=heading_font, fill=colour)
            ty += height(heading_font)
        if head and body:
            ty += 16
        for starts_point, line in body:
            if starts_point:
                cy = ty + height(body_font) // 2 - 2
                draw.ellipse([margin + pad + 4, cy - 5, margin + pad + 14, cy + 5], fill=colour)
            draw.text((margin + pad + indent, ty), line, font=body_font, fill="#1E293B")
            ty += height(body_font)
        y += card_height + gap

    if source_lines:
        draw.text((margin, y), "Sources", font=small_font, fill="#475569")
        y += height(small_font)
        for line in source_lines:
            draw.text((margin, y), line, font=small_font, fill="#64748B")
            y += height(small_font)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _create_infographic(
    content: str,
    works_cited: Optional[str] = None,
    title: Optional[str] = None,
    folder_id: Optional[str] = None,
) -> Dict:
    """
    Creates an infographic PNG from Markdown content and uploads it to Drive:
    a title banner, one coloured card per #/## section with its lines as
    bullets, and the sources at the bottom. Keep sections short (a heading and
    2-5 one-line points) so the image stays readable.

    Args:
        content: The infographic's content in Markdown; each heading becomes a card.
        works_cited: Citations / source URLs, one per line, or None.
        title: Shown in the banner, and the file name in Drive (".png" is added).
        folder_id: Drive folder to put it in, or None for My Drive.

    Returns:
        {"id", "name", "mimeType", "webViewLink"} of the new image.
    """
    if not (content or "").strip():
        raise ValueError("Infographic content is required.")
    name = _title(title, "Infographic")
    png_name = name if name.lower().endswith(".png") else f"{name}.png"
    try:
        return _upload(png_name, _render_infographic(name, content, works_cited), PNG, None, folder_id)
    except HttpError as error:
        raise ValueError(f"Failed to create infographic '{png_name}': {error}")
