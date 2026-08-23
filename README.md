# OmniMark

**A universal any-file → Markdown converter**, built with **Flask** + **Microsoft
MarkItDown** and a pluggable **multimodal LLM** (an OpenAI-compatible
[TokenRouter](https://tokenrouter.com) endpoint) that powers vision/OCR and
speech-to-text.

Tuned to run comfortably within **Render Free Tier (512 MB RAM / 0.1 CPU)**.

---

## ✨ What it does

Drop a file and get clean, production-grade Markdown in seconds:

| Category | Formats |
| --- | --- |
| Documents | `PDF`, `DOCX`, `PPTX`, `XLSX`, `XLS`, `EPUB` |
| Structured data & web | `HTML`, `CSV`, `JSON`, `JSONL`, `XML` (clean tables) |
| Vision & OCR | `JPG`, `PNG`, `WEBP`, `SVG` (chart analysis + OCR via LLM) |
| Audio (STT) | `MP3`, `WAV`, `M4A`, `OGG` (speech-to-text via LLM) |
| Code & text | `TXT`, `MD`, `PY`, `JS`, `LOG` |

The UI is a single dark "Obsidian" page with drag-&-drop, live size + type
validation, an upload-progress meter, a **Markdown editor with line numbers &
word/char counters**, and a **live rendered preview** (Marked.js + DOMPurify +
Highlight.js). Copy-to-clipboard and one-click `.md` download are built in.

---

## 🧱 Tech stack & architecture

- **Flask 3.x** — lightweight WSGI app, JSON API.
- **Microsoft MarkItDown 0.1.7** — document/table/code extraction.
- **OpenAI Python SDK** — `chat.completions` (image modality) for **WEBP** and
  `audio.transcriptions` for **OGG**/**STT fallback**. The base URL is the
  OpenAI-compatible TokenRouter `/v1` endpoint.
- **gunicorn** (`gthread`, 2 workers × 2 threads) — concurrency without
  process-heavy memory.

### Memory strategy (512 MB)
1. **Lazy, process-wide singletons** — `OpenAI` client + `MarkItDown` are built
   once per worker behind a lock, so both gthreads share the loaded libraries.
2. **Disk-backed uploads** — a `tempfile.mkstemp()` file is written in 1 MB
   chunks while streaming; the payload is never buffered fully in RAM, and the
   temp file is deleted inside a `finally` block.
3. **25 MB upload ceiling** enforced twice (Flask config + route check).

---

## 📁 Project layout

    .
    ├── app.py              # Flask routes, error handlers, temp lifecycle, pipeline
    ├── requirements.txt     # Pinned dependencies
    ├── Procfile             # Render start command (gunicorn)
    ├── render.yaml          # Optional declarative Render deployment
    ├── .env.example         # Environment blueprint (never commit the real .env)
    ├── .gitignore           # Secrets & cache ignore rules
    ├── templates/
    │   └── index.html       # Single-page UI (Tailwind, Marked, DOMPurify, hljs)
    └── README.md

---

## 🚀 Run locally

### 1. Prerequisites
- Python **3.10 – 3.13** (Windows, macOS, or Linux)
- Optional but recommended for audio STT: **ffmpeg** (for MP3/M4A/OGG → WAV)

### 2. Virtual environment (recommended)

    # Windows
    python -m venv .venv
    .venv\Scripts\activate

    # macOS / Linux
    python3 -m venv .venv
    source .venv/bin/activate

### 3. Install dependencies

    pip install -r requirements.txt

### 4. Set the API key

Copy the blueprint and paste your TokenRouter key:

    # Windows
    copy .env.example .env

    # macOS / Linux
    cp .env.example .env

Then edit `.env` and set a real value:

    API_KEY=sk-your-tokenrouter-api-key

> `.env` is already git-ignored. The app also reads real environment variables,
> so on Render you don't need a `.env` file at all.

### 5. Launch

    # Option A — quick dev server
    python app.py                # → http://127.0.0.1:5000

    # Option B — gunicorn (same as production)
    gunicorn --workers 2 --threads 2 --timeout 120 --worker-class gthread app:app

Open **http://127.0.0.1:5000**, drop a file, and press **Convert to Markdown**.

> Press **Ctrl+Enter** while a file is selected to start conversion without
> clicking. Health probe: `GET /healthz`.
---

## ☁️ Deploy to Render (Free Tier)

### Dashboard (recommended)
1. Push this repo to GitHub (or another Git host).
2. In Render → **New** → **Web Service**, connect the repo, choose **Python** runtime.
3. Set:
   - **Build command**: `pip install -r requirements.txt`
   - **Start command** (matches `Procfile`): `gunicorn --workers 2 --threads 2 --timeout 120 --worker-class gthread app:app`
4. Environment → **Add Variable**:
   - `API_KEY` = your TokenRouter key (mandatory)
   - `LLM_BASE_URL` = `https://api.tokenrouter.com/v1` (optional)
   - `LLM_MODEL` = `qwen/qwen3.8-max-free` (optional)
   - `MAX_UPLOAD_MB` = `25` (optional)
5. **Deploy**. When the first request is made, Render activates an instance and
   installs dependencies. It may take a few minutes before responses are warm.

### Option via `render.yaml`
If you push with `render.yaml` in the repo root, Render offers a **Deploy this
service** flow that reads it. The `API_KEY` variable is `sync: false`, so you
still set it once on the service's **Environment** tab.

### Post-deploy note on memory
With 2 gunicorn workers plus MarkItDown's heavier optional libs (pandas etc.),
a Free instance sits close to the 512 MB budget. If Render reports OOM errors,
the fastest fix is to drop to a single worker:

    gunicorn --workers 1 --threads 2 --timeout 120 --worker-class gthread app:app

(Update both the `render.yaml` start command and the Dashboard overrides.)

---

## ⚙️ Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_KEY` | *(none — required)* | TokenRouter API key for LLM vision/OCR/STT |
| `LLM_BASE_URL` | `https://api.tokenrouter.com/v1` | OpenAI-compatible base URL |
| `LLM_MODEL` | `qwen/qwen3.8-max-free` | Model served by TokenRouter |
| `MAX_UPLOAD_MB` | `25` | Upload ceiling (kept ≤25 for the Free Tier) |
| `PORT` | `5000` | Web port (Render sets this automatically) |
| `FLASK_SECRET_KEY` | random | Optional (no session auth is used) |

---

## 🎙 Audio transcription requirements
The backend tries, in order:

1. **MarkItDown native** — adds EXIF metadata, then transcodes MP3/M4A→WAV via
   **pydub** (needs **ffmpeg**) and transcribes with **SpeechRecognition**
   (Google Web Speech — needs outbound HTTPS).
2. **Local STT** (pydub + `recognize_google`).
3. **LLM `/audio/transcriptions`** on your TokenRouter endpoint (if the provider
   exposes it), using the OpenAI-compatible client.

If your Render image lacks ffmpeg, audio may fall through to step 3 or return a
clear, actionable error. WAV files with clear speech generally work without
extra tooling.

---

## 🔌 API

`POST /convert`
- `multipart/form-data` field `file` (one supported file, ≤ 25 MB).
- **200** → `{ ok, markdown, meta: { filename, extension, size_bytes,
  size_label, characters, words, model, seconds } }`
- **400** → missing/unsupported file · **413** → too large
- **422** → conversion failed (unsupported content, corrupt file, STT error)
- **502/504** → LLM connectivity/upstream · **429** → LLM rate limit

`GET /healthz` → `{ "status": "ok", "model": "..." }`

`GET /` → serves the single-page UI.

---

## 🔐 Security notes

- The upload allow-list + `mkstemp()` random names prevent filename/path
  injection.
- MarkItDown runs only on the temp file we created — it never fetches remote
  URLs or wildcards from client input.
- DOMPurify sanitizes every Markdown→HTML render before it is injected into the
  preview pane.
- Security headers (`nosniff`, `DENY` framing, etc.) are set on every response.
- **Rotate your key if it has ever been shared.** The `.env` in this workspace
  contains a live key and is already ignored by Git — treat it as a secret.

---

## 🧯 Troubleshooting

- **`ModuleNotFoundError: markitdown`** → re-run `pip install -r requirements.txt`
  and make sure the virtual environment is active.
- **Import fails on optional extras** → install `markitdown[all]==0.1.7`.
- **`413` at 24.9 MB** → that is the 25 MB ceiling working as intended.
- **Slow first render** → MarkItDown + pandas are loaded lazily into memory on
  the first request; subsequent requests are fast.
- **`502` / rate limit** → check `API_KEY`, provider quota, and outbound network.