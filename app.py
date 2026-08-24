"""
OmniMark — Universal Any-File to Markdown Converter
===================================================
Production-grade Flask backend built on Microsoft MarkItDown, extended with
a pluggable, OpenAI-compatible multimodal LLM layer (TokenRouter) that powers
vision/OCR (JPG, PNG, WEBP, SVG) and speech-to-text (MP3, WAV, M4A, OGG).

Memory-tuned for Render Free Tier (512 MB RAM / 0.1 CPU):
  * Lightweight gunicorn gthread workers (2 workers x 2 threads).
  * Lazily-initialized, process-wide OpenAI + MarkItDown singletons.
  * Uploads streamed to disk via NamedTemporaryFile (never fully in RAM).
  * 25 MB upload ceiling enforced at the WSGI and application layers.
"""

import base64
import io
import ipaddress
import logging
import os
import re
import tempfile
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()  # Local development only — Render injects real environment variables.

try:
    import openai as _openai
except ImportError:  # pragma: no cover — app boots without it; vision routes explain
    _openai = None

try:
    from markitdown import MarkItDown
except ImportError:  # pragma: no cover — app boots; conversion routes report the missing dep
    MarkItDown = None

# ---------------------------------------------------------------------------
# Configuration — every knob is overridable via environment variables
# ---------------------------------------------------------------------------
MAX_UPLOAD_MB = max(1, int(os.getenv("MAX_UPLOAD_MB", "25")))
MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024

API_KEY = os.getenv("API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.tokenrouter.com/v1").strip().rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen/qwen3.8-max-free").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("omnimark")

# Defensive extension allow-list (server-side; mirrors the client UI).
SUPPORTED = {
    "documents": {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".epub"},
    "web/data": {".html", ".htm", ".xml", ".csv", ".json", ".jsonl"},
    "images": {".jpg", ".jpeg", ".png", ".webp", ".svg"},
    "audio": {".mp3", ".wav", ".m4a", ".ogg"},
    "code/text": {".txt", ".text", ".md", ".markdown", ".py", ".js", ".log"},
}
ALLOWED_EXTENSIONS = set().union(*SUPPORTED.values())

IMAGE_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".svg": "image/svg+xml",
}
AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/x-wav",
    ".m4a": "audio/mp4", ".ogg": "audio/ogg",
}

# Prompt used for every direct vision "chat.completions" call.
VISION_PROMPT = (
    "Write a detailed Markdown document describing this image exactly. "
    "Transcribe EVERY piece of visible text verbatim (OCR). Analyze charts, plots, "
    "diagrams, and UI screenshots; describe data points, axes, colors, layout, and "
    "interface elements with precision. Structure the output with Markdown headings, "
    "bullet lists, and tables where appropriate. Do not invent text that is not "
    "actually present in the image."
)

# Prompt used when an SVG document is fed to the model as raw XML text.
SVG_PROMPT = (
    "You are an SVG semantic analyzer. Below is the raw XML of an SVG file "
    "(icons, logos, charts, or diagrams). Produce a Markdown document that: "
    "1) describes the graphic and its visual layout; 2) transcribes every <text> "
    "element verbatim including axis labels, titles and legends; 3) reconstructs "
    "any chart, table, or quantitative data as Markdown tables/lists. Only include "
    "information that is actually present in the markup — never fabricate values. "
    "Ignore namespace declarations, <metadata>, IDs, and styling attributes."
)
# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["JSON_SORT_KEYS"] = False
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY") or os.urandom(32)

_builder_lock = threading.Lock()
_builder_cache = None  # (openai_client, markitdown) — built once per process.


class ConversionError(RuntimeError):
    """Raised for expected, user-facing conversion failures."""


def _get_builder():
    """Return the shared (OpenAI client, MarkItDown) pair, building lazily.

    The OpenAI client is optional: plain document/table/code conversion works
    without it. Image/audio/vision paths raise a clear error instead of a crash
    when no API key is configured. The MarkItDown instance is built once per
    worker and shared across the gthread pool to bound RSS on the 512 MB tier.
    """
    global _builder_cache
    with _builder_lock:
        if _builder_cache is not None:
            return _builder_cache
        if MarkItDown is None:
            raise ConversionError(
                "MarkItDown is not installed on this deployment. "
                "Run 'pip install -r requirements.txt' and redeploy."
            )
        client = None
        if API_KEY and _openai is not None:
            client = _openai.OpenAI(
                base_url=LLM_BASE_URL,
                api_key=API_KEY,
                timeout=115.0,
                max_retries=0,
            )
        converter = MarkItDown(
            llm_client=client,
            llm_model=LLM_MODEL,
            llm_prompt=VISION_PROMPT,
            enable_builtins=True,
            enable_plugins=False,  # No 3rd-party plugin indexing for security/speed
        )
        _builder_cache = (client, converter)
        logger.info("MarkItDown ready: model=%s llm_enabled=%s", LLM_MODEL, bool(client))
        return _builder_cache


# ---------------------------------------------------------------------------
# Low-level multimodal helpers
# ---------------------------------------------------------------------------
def _read_text(file_path: str, limit: int = 120_000) -> str:
    """Read a file as text (best-effort decode), truncated to `limit` chars."""
    raw = Path(file_path).read_bytes()
    text = raw.decode("utf-8", errors="replace")
    if len(text) > limit:
        text = text[:limit] + "\n\n/* …truncated… */"
    return text


def _wrap(code: str, lang: str = "") -> str:
    return f"```{lang}\n{code}\n```"


def _sniff_image_mime(file_path: str):
    """Best-effort MIME detection from magic bytes so the client sends an
    accurate Content-Type (a common cause of HTTP 400 rejections)."""
    try:
        with open(file_path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head.lstrip().startswith((b"<?xml", b"<svg")):
        return "image/svg+xml"
    return None


def _vision_messages(prompt: str, data_uri: str):
    """Standard OpenAI ChatCompletions vision message list."""
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ],
    }]


def _llm_vision(file_path: str, mime: str, prompt: str) -> str:
    """Send the image as base64 ChatCompletions vision content.

    Uses a ``data:<sniffed-mime>;base64,...`` URI so providers do not reject the
    payload with a 400. Retries once with an OCR-focused prompt, then raises a
    descriptive ConversionError rather than letting the request crash the app.
    """
    client, _ = _get_builder()
    if client is None:
        raise ConversionError(
            "No API key is configured; image vision/OCR is disabled. "
            "Set the API_KEY environment variable to enable it."
        )
    with open(file_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    data_uri = f"data:{mime};base64,{b64}"

    attempts = [
        _vision_messages(prompt, data_uri),
        _vision_messages(
            "Transcribe ALL visible text verbatim (OCR), then describe this image in "
            "clean Markdown with headings, lists and tables where relevant.",
            data_uri,
        ),
    ]
    last_error = ""
    for messages in attempts:
        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                max_tokens=4096,
                timeout=50.0,
            )
            content = (response.choices[0].message.content or "").strip()
            if content:
                return content
            last_error = "the model returned an empty response"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Vision request failed: %s", last_error)
            
    # Attempt graceful OCR fallback
    try:
        import pytesseract
        from PIL import Image
        text = pytesseract.image_to_string(Image.open(file_path)).strip()
        if text:
            return f"### OCR Extracted Text\n\n{text}"
    except ImportError:
        pass
    except Exception as e:
        logger.warning("Local OCR fallback failed: %s", e)

    raise ConversionError(
        "The vision model could not transcribe this image"
        + (" (node returned no text)" if not last_error else f" — {last_error}")
    )


def _convert_svg(file_path: str) -> str:
    """SVG → Markdown: feed the raw XML to the LLM; fall back to a fenced block."""
    text = _read_text(file_path)
    if not text.strip():
        return "*(empty SVG file)*"
    try:
        client, _ = _get_builder()
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": SVG_PROMPT + "\n\n" + text}],
            temperature=0.2,
            max_tokens=4096,
        )
        out = (response.choices[0].message.content or "").strip()
        if out:
            return out
    except ConversionError:
        raise
    except Exception:
        logger.warning("LLM path failed for SVG; falling back to raw block.", exc_info=True)
    return _wrap(text, lang="xml")
def _local_transcribe(file_path: str, fmt: str) -> str:
    """Local STT via pydub + SpeechRecognition (Google Web Speech API)."""
    import speech_recognition as sr
    from pydub import AudioSegment

    if fmt in {"ogg", "mp3", "m4a"}:
        segment = AudioSegment.from_file(file_path, format=fmt)
        wav = io.BytesIO()
        segment.export(wav, format="wav")
        wav.seek(0)
        source = wav
    else:  # wav passes straight through
        source = file_path

    recognizer = sr.Recognizer()
    with sr.AudioFile(source) as src:
        audio = recognizer.record(src)
    return recognizer.recognize_google(audio).strip()


def _llm_transcribe(file_path: str, ext: str) -> str:
    """Speech-to-text via an OpenAI-compatible /audio/transcriptions endpoint."""
    client, _ = _get_builder()
    if client is None:
        raise ConversionError(
            "No API key is configured; LLM audio transcription is disabled."
        )
    mime = AUDIO_MIME.get(ext, "application/octet-stream")
    name = "audio" + ext
    with open(file_path, "rb") as fh:
        response = client.audio.transcriptions.create(
            model=LLM_MODEL, file=(name, fh, mime)
        )
    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise ConversionError("The STT endpoint returned no transcript.")
    return text


def _convert_audio(file_path: str) -> str:
    """Best-effort audio → Markdown transcript.

    Order of attempts: (1) MarkItDown native (metadata + local STT),
    (2) local pydub + recognize_google, (3) LLM /audio/transcriptions.
    """
    ext = Path(file_path).suffix.lower()
    errors = []

    # (1) MarkItDown native — includes EXIF metadata + its own STT path.
    if ext in AUDIO_MIME and ext != ".ogg":
        try:
            _, converter = _get_builder()
            result = converter.convert(file_path)
            body = (result.markdown if result else "").strip()
            if body and "Audio Transcript" in body:
                return body
        except Exception as exc:  # noqa: BLE001
            errors.append(f"markitdown: {exc}")
            logger.warning("MarkItDown audio path failed (%s); trying fallbacks.", ext)

    # (2) Local transcription (requires ffmpeg + internet to Google).
    try:
        text = _local_transcribe(file_path, ext.lstrip("."))
        if text:
            return "### Audio Transcript\n\n" + text
    except Exception as exc:  # noqa: BLE001
        errors.append(f"local: {exc}")

    # (3) LLM /audio/transcriptions endpoint.
    try:
        text = _llm_transcribe(file_path, ext)
        if text:
            return "### Audio Transcript\n\n" + text
    except Exception as exc:  # noqa: BLE001
        errors.append(f"llm: {exc}")

    raise ConversionError(
        "Could not transcribe this audio file. Tried local STT and the LLM "
        "endpoint — " + ("; ".join(errors) or "no backend available") + ". "
        "Install ffmpeg locally and confirm the endpoint exposes a working "
        "audio-transcription route."
    )


def _extract_pdf_text(path: str) -> str:
    """Blind PDF text extraction fallback (used when MarkItDown yields nothing).

    Tries pypdf first (lightweight, adequate for text-based PDFs), then
    pdfminer.six. Raises ConversionError with a clear message if neither works,
    e.g. for scanned/image-only PDFs that would need OCR.
    """
    errors = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
        pages = [p for p in pages if p]
        if pages:
            return "\n\n".join(pages)
        errors.append("pypdf returned no text")
    except ImportError:
        errors.append("pypdf not installed")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pypdf error: {exc}")
        logger.warning("pypdf extraction failed: %s", exc)

    try:
        from pdfminer.high_level import extract_text as pdfminer_extract
        text = (pdfminer_extract(path) or "").strip()
        if text:
            return text
        errors.append("pdfminer returned no text")
    except ImportError:
        errors.append("pdfminer not installed")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pdfminer error: {exc}")
        logger.warning("pdfminer extraction failed: %s", exc)

    raise ConversionError(
        "Could not extract text from this PDF (" + "; ".join(errors) + "). "
        "It may be a scanned/image-only document; OCR is not available without "
        "a configured vision model."
    )


def _raw_text_fallback(path: str) -> str:
    """Best-effort decode of a plain text file to UTF-8-ish text."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return ""
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace")


_PIPE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")


def _clean_fragmented_tables(markdown: str) -> str:
    """Repair MarkItDown's fractured ASCII tables.

    Many multi-column documents/CVs come out as stray ``| | |`` rows. This
    repair keeps real multi-column tables intact but rewrites empty, one-cell,
    and single-character fragments as clean bullet lines, and drops stray
    separators — producing readable Markdown headings/lists instead of broken
    table syntax.
    """
    cleaned = []
    for raw in markdown.split("\n"):
        line = raw.rstrip()
        if not _PIPE_LINE_RE.match(line):
            cleaned.append(line)
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        cells = [c for c in cells if c]
        if not cells:
            continue
        # Drop separator rows like |---|---:---|
        if all(re.fullmatch(r"[-:+=_]+", c) for c in cells):
            continue
        if len(cells) == 1:
            # A lone cell (e.g. a heading fragment leaked from a table) → bullet.
            cleaned.append(f"- {cells[0]}")
        elif len(cells) >= 3 and all(len(c) <= 2 for c in cells):
            # Character-by-character fragmentation like | T | h | i | n | → merge.
            cleaned.append("- " + " ".join(cells))
        else:
            cleaned.append("| " + " | ".join(cells) + " |")
    result = "\n".join(cleaned)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def convert_file(path: str, filename: str) -> str:
    """Route a file to the best converter and return Markdown text (or raise
    ConversionError with a user-facing message — never crash the worker)."""
    ext = Path(filename).suffix.lower()

    if ext == ".svg":
        return _convert_svg(path)
    if ext in IMAGE_MIME:  # jpg / jpeg / png / webp → vision/OCR endpoint
        mime = _sniff_image_mime(path) or IMAGE_MIME[ext]
        return _llm_vision(path, mime, VISION_PROMPT)
    if ext in AUDIO_MIME:
        return _convert_audio(path)

    body = ""
    conversion_error = ""
    try:
        _, converter = _get_builder()
        result = converter.convert(path)
        body = (result.markdown if result else "").strip()
        if not body and ext == ".pdf":
            # MarkItDown frequently yields empty output for PDFs.
            body = _extract_pdf_text(path)
        elif not body and ext not in {".json", ".jsonl", ".csv"}:
            body = _raw_text_fallback(path)
    except Exception as exc:  # noqa: BLE001
        conversion_error = f"{type(exc).__name__}: {exc}"
        logger.warning("MarkItDown failed for %s (%s); trying fallbacks.", filename, conversion_error)
        if ext == ".pdf":
            body = _extract_pdf_text(path)
        elif ext not in {".json", ".jsonl", ".csv"}:
            body = _raw_text_fallback(path)

    if not body:
        raise ConversionError(
            conversion_error or "The converter produced an empty result for this file."
        )
    return _clean_fragmented_tables(body)
# ---------------------------------------------------------------------------
# URL & text pipeline helpers
# ---------------------------------------------------------------------------
def _json_error(message: str, status: int):
    return jsonify(ok=False, error=message), status


def _valid_url_host(url: str) -> bool:
    """Best-effort SSRF guard: must be http(s) and not a private/loopback IP."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().strip("[]")
        if not host or parsed.scheme not in ("http", "https"):
            return False
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
                return False
        except ValueError:
            pass  # a hostname, not an IP literal — follow-up redirects are still bounded
        return True
    except Exception:  # noqa: BLE001
        return False


def _fetch_url_to_file(url: str, dest: str) -> int:
    """Stream a remote URL into `dest`, capped at MAX_CONTENT_LENGTH."""
    import requests  # lazy import keeps cold start light

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; OmniMark/1.0; Markdown converter)",
        "Accept": "text/html,text/markdown,text/plain,application/pdf;q=0.9,*/*;q=0.1",
    }
    size = 0
    with requests.get(url, headers=headers, timeout=30, stream=True) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as out:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_CONTENT_LENGTH:
                    raise ConversionError(
                        "The remote page is larger than the "
                        f"{MAX_UPLOAD_MB} MB processing limit."
                    )
                out.write(chunk)
    return size


def _decode_bytes(data: bytes) -> str:
    """Decode fetched bytes trying common encodings."""
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    return data.decode("utf-8", errors="replace")


def _html_to_markdown(html: str) -> str:
    """Convert raw HTML (e.g. downloaded article) to clean Markdown.

    Tries MarkItDown's HTML converter first, then falls back to a
    BeautifulSoup-based extraction so a broken dependency never crashes.
    """
    try:
        _, converter = _get_builder()
        result = converter.convert_stream(
            io.BytesIO(html.encode("utf-8")),
            file_extension=".html",
        )
        body = (result.markdown if result else "").strip()
        if body:
            return body
    except Exception as exc:  # noqa: BLE001
        logger.warning("MarkItDown HTML conversion failed; using bs4 fallback: %s", exc)

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe"]):
            tag.decompose()
        text = soup.get_text("\n")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return "\n\n".join(lines)
    except Exception:  # noqa: BLE001
        return html


def _clean_pasted_text(text: str) -> str:
    """Normalize pasted/raw text into tidy Markdown source."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    # Collapse runs of blank lines (allow at most 1 empty line between blocks).
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template(
        "index.html",
        model=LLM_MODEL,
        max_mb=MAX_UPLOAD_MB,
        configured=bool(API_KEY),
    )


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "model": LLM_MODEL})


def _stream_to_temp(f, ext: str) -> tuple[str, int]:
    """Persist the uploaded stream to a temp file; return (path, bytes)."""
    fd, tmp_path = tempfile.mkstemp(prefix="omnimark_", suffix=ext)
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = f.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_CONTENT_LENGTH:
                    raise ValueError(f"Upload exceeds the {MAX_UPLOAD_MB} MB limit.")
                out.write(chunk)
        return tmp_path, size
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


@app.post("/convert")
def convert():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify(ok=False, error="No file was uploaded."), 400

    # Strip any directory components a hostile client might smuggle in.
    original = Path(uploaded.filename).name
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify(
            ok=False,
            error=f"Unsupported file type '{ext or '(none)'}'. Allowed: "
            f"{', '.join(sorted(ALLOWED_EXTENSIONS))}.",
        ), 400

    if uploaded.content_length and uploaded.content_length > MAX_CONTENT_LENGTH:
        return jsonify(
            ok=False, error=f"Upload exceeds the {MAX_UPLOAD_MB} MB limit."
        ), 413

    tmp_path = None
    started = time.perf_counter()
    try:
        tmp_path, size = _stream_to_temp(uploaded, ext)
        markdown = convert_file(tmp_path, original)
        elapsed = round(time.perf_counter() - started, 2)
        return jsonify(
            ok=True,
            markdown=markdown,
            meta={
                "filename": original,
                "extension": ext.lstrip("."),
                "size_bytes": size,
                "size_label": _human_size(size),
                "characters": len(markdown),
                "words": len(markdown.split()),
                "model": LLM_MODEL,
                "seconds": elapsed,
            },
        )
    except ConversionError as exc:
        logger.warning("Conversion failed (%s): %s", original, exc)
        return jsonify(ok=False, error=str(exc)), 422
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 413
    except Exception as err:  # noqa: BLE001
        # Duck-type provider errors so this works across OpenAI SDK versions.
        _never = type("_Never", (Exception,), {})
        _a = lambda name: getattr(_openai, name, _never)  # noqa: E731
        if isinstance(err, (_a("AuthenticationError"), _a("PermissionDeniedError"))):
            status, message = 502, "The LLM provider rejected the API key. Check API_KEY."
        elif isinstance(err, _a("RateLimitError")):
            status, message = 429, "LLM rate limit exceeded — please retry shortly."
        elif isinstance(err, _a("APIConnectionError")):
            status, message = 502, "Could not reach the LLM endpoint. Check connectivity."
        elif isinstance(err, _a("APITimeoutError")):
            status, message = 504, "The LLM request timed out. Please retry."
        else:
            logger.error("Unhandled route error:\n%s", traceback.format_exc())
            status, message = 500, "The server hit an unexpected error during conversion."
        return jsonify(ok=False, error=message), status
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                logger.warning("Could not remove temp file: %s", tmp_path)


@app.post("/convert-url")
def convert_url_route():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return _json_error("No URL provided.", 400)
    if not url.lower().startswith(("http://", "https://")):
        return _json_error("URL must begin with http:// or https://.", 400)
    if not _valid_url_host(url):
        return _json_error(
            "This URL cannot be fetched by the server (only public http(s) hosts are allowed).",
            400,
        )

    tmp_path = None
    started = time.perf_counter()
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="omnimark_url_", suffix=".html")
        os.close(fd)
        size = _fetch_url_to_file(url, tmp_path)
        data = Path(tmp_path).read_bytes()
        if data.lstrip().startswith(b"%PDF"):
            # Some URLs point directly at PDF files → use the PDF pipeline.
            markdown = convert_file(tmp_path, "download.pdf")
        else:
            text = _decode_bytes(data)
            markdown = _html_to_markdown(text)
        clean = _clean_fragmented_tables(markdown)
    except ConversionError as exc:
        return _json_error(str(exc), 422)
    except Exception as exc:  # noqa: BLE001
        cls = type(exc).__name__
        msg = str(exc)
        if "Timeout" in cls or "ConnectionError" in cls:
            return _json_error("The URL request timed out or the host is unreachable.", 504)
        if cls == "HTTPError" or "404" in msg or "403" in msg or "401" in msg:
            return _json_error(f"The remote server refused the request: {msg}", 502)
        logger.error("URL conversion error (%s):\n%s", url, traceback.format_exc())
        return _json_error(f"Could not convert that URL: {msg}", 502)
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                logger.warning("Could not remove temp file: %s", tmp_path)

    if not clean:
        return _json_error("No readable content was found at that URL.", 422)
    return jsonify(
        ok=True,
        markdown=clean,
        meta={
            "filename": "webpage.md",
            "source": url,
            "size_label": _human_size(size),
            "characters": len(clean),
            "words": len(clean.split()),
            "model": LLM_MODEL,
            "seconds": round(time.perf_counter() - started, 2),
        },
    )


@app.post("/convert-text")
def convert_text():
    payload = request.get_json(silent=True) or {}
    content = (payload.get("content") or "").strip()
    if not content:
        return _json_error("No text content provided.", 400)
    if len(content) > MAX_CONTENT_LENGTH:
        return _json_error(f"Text exceeds the {MAX_UPLOAD_MB} MB limit.", 413)

    started = time.perf_counter()
    cleaned = _clean_pasted_text(content)
    if not cleaned:
        return _json_error("Nothing to convert after cleaning.", 422)
    return jsonify(
        ok=True,
        markdown=cleaned,
        meta={
            "filename": "pasted-text.md",
            "source": "direct text",
            "characters": len(cleaned),
            "words": len(cleaned.split()),
            "model": LLM_MODEL,
            "seconds": round(time.perf_counter() - started, 2),
        },
    )


def _human_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{round(num / 1024, 1)} KB"
    return f"{round(num / (1024 * 1024), 1)} MB"


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(413)
def payload_too_large(_err):
    return jsonify(
        ok=False, error=f"Upload exceeds the {MAX_UPLOAD_MB} MB limit."
    ), 413


@app.errorhandler(404)
def not_found(_err):
    return jsonify(ok=False, error="Not found."), 404


@app.errorhandler(405)
def bad_method(_err):
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return resp
    return jsonify(ok=False, error="Method not allowed."), 405


@app.errorhandler(500)
def server_error(_err):
    return jsonify(ok=False, error="Internal server error."), 500


# ---------------------------------------------------------------------------
# Security & CORS headers on every response
# ---------------------------------------------------------------------------
@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return resp

@app.after_request
def security_headers(resp):
    resp.headers.setdefault("Access-Control-Allow-Origin", "*")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    return resp


if __name__ == "__main__":
    _port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=_port, debug=False, threaded=True)