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
import logging
import os
import tempfile
import threading
import time
import traceback
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()  # Local development only — Render injects real environment variables.

import openai as _openai
from markitdown import MarkItDown
from markitdown._exceptions import (
    FileConversionException,
    MissingDependencyException,
    UnsupportedFormatException,
)

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

    Built exactly once per worker process. Safe to call from the small
    gunicorn gthread pool via a module-level lock. Keeps startup import
    cost and RSS footprint as low as the Free Tier can afford.
    """
    global _builder_cache
    with _builder_lock:
        if _builder_cache is not None:
            return _builder_cache
        if not API_KEY:
            raise ConversionError(
                "The app is not configured: set the API_KEY environment "
                "variable (see .env.example) to enable LLM-backed conversion."
            )
        client = _openai.OpenAI(
            base_url=LLM_BASE_URL,
            api_key=API_KEY,
            timeout=115.0,
            max_retries=1,
        )
        converter = MarkItDown(
            llm_client=client,
            llm_model=LLM_MODEL,
            llm_prompt=VISION_PROMPT,
            enable_builtins=True,
            enable_plugins=False,  # No 3rd-party plugin indexing for security/speed
        )
        _builder_cache = (client, converter)
        logger.info("LLM pipeline ready: model=%s endpoint=%s", LLM_MODEL, LLM_BASE_URL)
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


def _llm_vision(file_path: str, mime: str, prompt: str) -> str:
    """Direct multimodal vision call (base64 data-URI → chat.completions)."""
    client, _ = _get_builder()
    with open(file_path, "rb") as fp:
        b64 = base64.b64encode(fp.read()).decode("ascii")
    data_uri = f"data:{mime};base64,{b64}"
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
    )
    content = response.choices[0].message.content
    if not content or not content.strip():
        raise ConversionError("The vision model returned no useful text for this image.")
    return content.strip()


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


def convert_file(path: str, filename: str) -> str:
    """Route a file to the correct converter and return Markdown text."""
    ext = Path(filename).suffix.lower()

    # Format families that MarkItDown 0.1.7 does not route itself:
    if ext in {".webp", ".svg"}:
        if ext == ".webp":
            return _llm_vision(path, IMAGE_MIME[ext], VISION_PROMPT)
        return _convert_svg(path)
    if ext in AUDIO_MIME:
        return _convert_audio(path)

    # Everything else goes to the native MarkItDown pipeline.
    try:
        _, converter = _get_builder()
        result = converter.convert(path)
        body = (result.markdown if result else "").strip()
    except UnsupportedFormatException:
        raise ConversionError(
            "Unsupported file type — the converter sniffed the content and found "
            "no handler for it. Submit a supported format under the 25 MB limit."
        ) from None
    except MissingDependencyException:
        raise ConversionError(
            "A converter dependency is missing on this deployment. Install the "
            "full MarkItDown extras (markitdown[all]) and redeploy."
        ) from None
    except FileConversionException:
        raise ConversionError(
            "The file matched a known format but could not be parsed — it may be "
            "corrupted or encrypted."
        ) from None
    except Exception:  # noqa: BLE001
        logger.error("Unhandled conversion error:\n%s", traceback.format_exc())
        raise ConversionError(
            "An unexpected error occurred while converting the file."
        ) from None

    if not body:
        raise ConversionError("The converter produced an empty result.")
    return body
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
    return jsonify(ok=False, error="Method not allowed."), 405


@app.errorhandler(500)
def server_error(_err):
    return jsonify(ok=False, error="Internal server error."), 500


# ---------------------------------------------------------------------------
# Security headers on every response
# ---------------------------------------------------------------------------
@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    return resp


if __name__ == "__main__":
    _port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=_port, debug=False, threaded=True)