"""Handles uploaded files: saves them to disk and pulls out readable text
so it can be fed to the model as context, the same way web search results
are (see websearch.py's _web_context_block for the same pattern).

Images are saved and served back for display either way, but only
actually *understood* by the model if a vision-capable model is selected
- see app.py's is_vision_model(). Without one, the model just gets told
an image was attached and its filename, not its contents.
"""
import base64
import os
import re
import uuid

import docx
from pypdf import PdfReader

UPLOAD_DIR = os.path.join("static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_TEXT_CHARS = 6000  # per file - keeps a small local model's context sane
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB

TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".csv",
    ".html", ".htm", ".css", ".c", ".cpp", ".h", ".hpp", ".java", ".cs",
    ".go", ".rs", ".rb", ".php", ".sh", ".yaml", ".yml", ".xml", ".sql",
    ".log", ".ini", ".cfg", ".toml",
    # Added after a .bat upload came back "unsupported": the list only
    # ever held the extensions somebody happened to think of, and every
    # one it missed told the user their file could not be read when it
    # was plain text all along.
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".bash", ".zsh", ".fish",
    ".env", ".conf", ".properties", ".gradle", ".tsv", ".rst", ".tex",
    ".svg", ".vue", ".svelte", ".astro", ".scss", ".sass", ".less",
    ".kt", ".kts", ".swift", ".scala", ".dart", ".lua", ".pl", ".pm",
    ".r", ".jl", ".ex", ".exs", ".erl", ".hs", ".clj", ".asm", ".s",
    ".m", ".mm", ".f90", ".vb", ".gitignore", ".dockerignore",
    ".editorconfig", ".patch", ".diff", ".srt", ".vtt", ".graphql",
    ".proto", ".tf", ".hcl", ".nix", ".mjs", ".cjs", ".mts", ".cts",
}

# Files with no extension at all that are text by convention.
TEXT_FILENAMES = {
    "makefile", "dockerfile", "license", "licence", "readme", "changelog",
    "authors", "contributing", "notice", "procfile", "gemfile", "rakefile",
    "vagrantfile", "jenkinsfile", "codeowners", ".gitignore", ".env",
}

# How much of an unknown file to sniff before deciding it is text.
SNIFF_BYTES = 8192


def _looks_like_text(path):
    """Whether an unrecognised file is really just text.

    The extension list can only ever hold what somebody remembered.
    This is the fallback that makes the answer depend on the file rather
    than on its name: decode a sample as UTF-8 and reject it if it holds
    NUL bytes or a lot of control characters, which is what separates a
    document from a compiled binary or an archive.
    """
    try:
        with open(path, "rb") as fh:
            sample = fh.read(SNIFF_BYTES)
    except OSError:
        return False
    if not sample:
        return False
    if b"\x00" in sample:
        return False
    try:
        decoded = sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    # Tabs, newlines and carriage returns are ordinary in text; other
    # control characters are not.
    control = sum(1 for ch in decoded
                  if ord(ch) < 32 and ch not in "\t\n\r")
    return control <= len(decoded) * 0.02
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name):
    name = os.path.basename(name or "file")
    name = _SAFE_NAME_RE.sub("_", name)
    return name[-120:] or "file"


def _extract_text(path, ext, name=""):
    """-> extracted text, an error message starting with "[", or None if
    the file is genuinely not readable as text."""
    try:
        if ext == ".pdf":
            reader = PdfReader(path)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        elif ext == ".docx":
            document = docx.Document(path)
            text = "\n".join(p.text for p in document.paragraphs)
        elif ext in TEXT_EXTENSIONS or name in TEXT_FILENAMES:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        elif _looks_like_text(path):
            # Unrecognised extension, but the bytes say text. Reading it
            # is strictly better than telling somebody their file cannot
            # be read when it plainly can.
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            return None
    except Exception as e:
        return f"[Could not read this file: {e}]"

    text = text.strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n...[truncated]"
    return text


def save_and_extract(file_storage):
    """Saves an uploaded Werkzeug FileStorage to disk and returns
    {filename, url, kind, text, size, error}. `kind` is "text", "image",
    or "unsupported" (still saved, just nothing could be extracted)."""
    original_name = file_storage.filename or "file"
    ext = os.path.splitext(original_name)[1].lower()
    stored_name = f"{uuid.uuid4().hex[:8]}_{_safe_filename(original_name)}"
    path = os.path.join(UPLOAD_DIR, stored_name)

    file_storage.save(path)
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        os.remove(path)
        return {
            "filename": original_name,
            "url": None,
            "kind": "unsupported",
            "text": None,
            "size": size,
            "error": "File too large (20MB limit).",
        }

    if ext in IMAGE_EXTENSIONS:
        kind, text = "image", None
    else:
        text = _extract_text(path, ext,
                             os.path.basename(original_name).lower())
        kind = "text" if text is not None else "unsupported"

    url_dir = UPLOAD_DIR.replace(os.sep, "/")
    return {
        "filename": original_name,
        "url": f"/{url_dir}/{stored_name}",
        "kind": kind,
        "text": text,
        "size": size,
    }


def encode_image_base64(url):
    """Reads a previously-saved upload back off disk (from its /static/...
    URL) and returns it base64-encoded, the form Ollama's `images` field
    expects. -> None if the file can't be found or read."""
    if not url or not url.startswith("/" + UPLOAD_DIR.replace(os.sep, "/") + "/"):
        return None
    path = os.path.join(UPLOAD_DIR, os.path.basename(url))
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return None
