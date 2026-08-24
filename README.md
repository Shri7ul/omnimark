<div align="center">

# ✨ OmniMark ✨

**A universal any-file → Markdown converter**

[![Live Demo](https://img.shields.io/badge/Live_Demo-shriful.tech%2Fomnimark-blue?style=for-the-badge&logo=vercel)](https://shriful.tech/omnimark)
[![Demo Video](https://img.shields.io/badge/Watch-Demo_Video-red?style=for-the-badge&logo=youtube)](https://lnkd.in/p/gauaa5UH)
[![Author](https://img.shields.io/badge/Author-Shriful_Islam_(InHumanZ)-green?style=for-the-badge&logo=github)](https://github.com/InHumanZ)

*Built with Flask, Microsoft MarkItDown & Pluggable Multimodal LLMs*

</div>

---

## 🚀 Drop a file. Get production-grade Markdown.

OmniMark is your all-in-one universal converter. Whether it's a messy PDF, a random image with text, a spreadsheet, or a voice memo—just drop it in, and OmniMark transforms it into clean, structured Markdown in seconds.

### 🌐 See it in action:
- **Live Demo:** [https://shriful.tech/omnimark](https://shriful.tech/omnimark)
- **Demo Video:** [Watch Demo](https://lnkd.in/p/gauaa5UH)

---

## 🔥 Mind-Blowing Features

- 📄 **Documents:** Instantly parse `PDF`, `DOCX`, `PPTX`, `XLSX`, `XLS`, `EPUB`
- 📊 **Structured Data & Web:** Extract clean tables from `HTML`, `CSV`, `JSON`, `JSONL`, `XML`
- 👁️ **Vision & OCR:** Magic chart analysis and text extraction from `JPG`, `PNG`, `WEBP`, `SVG` via LLM!
- 🎙️ **Audio (STT):** Convert speech-to-text directly from `MP3`, `WAV`, `M4A`, `OGG` using LLMs.
- 💻 **Code & Text:** Native support for `TXT`, `MD`, `PY`, `JS`, `LOG`
- 🎨 **Sleek UI:** A single dark "Obsidian-like" page with drag-&-drop, live progress, Markdown editor, and a live rendered preview!

---

## 🧱 Tech Stack

- **Flask 3.x** — Lightweight WSGI app, JSON API.
- **Microsoft MarkItDown 0.1.7** — Powerful document/table/code extraction.
- **OpenAI Python SDK** — Vision & Speech capabilities powered by OpenAI-compatible TokenRouter endpoints.
- **Gunicorn** — Concurrency without process-heavy memory.

*Tuned to run comfortably within **Render Free Tier (512 MB RAM / 0.1 CPU)**!*

<details>
<summary><b>🛠️ Run Locally & Deploy (Click to expand)</b></summary>

### 1. Prerequisites
- Python **3.10 – 3.13** (Windows, macOS, or Linux)
- Optional but recommended for audio STT: **ffmpeg** (for MP3/M4A/OGG → WAV)

### 2. Quick Start
```bash
# Clone & enter virtual environment
python -m venv .venv
# Windows: .venv\Scripts\activate | macOS/Linux: source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set the API key
cp .env.example .env
# Edit .env and set API_KEY=sk-your-tokenrouter-api-key

# Run the app
python app.py
```
Open **http://127.0.0.1:5000**, drop a file, and convert!

### ☁️ Deploy to Render (Free Tier)
1. Push this repo to GitHub.
2. In Render → **New** → **Web Service**, connect the repo, choose **Python** runtime.
3. Set Build command: `pip install -r requirements.txt`
4. Set Start command: `gunicorn --workers 2 --threads 2 --timeout 120 --worker-class gthread app:app`
5. Add `API_KEY` to Environment Variables. Deploy!

</details>

<details>
<summary><b>🔌 API Reference & Architecture (Click to expand)</b></summary>

### API
`POST /convert`
- `multipart/form-data` field `file` (one supported file, ≤ 25 MB).
- **200** → `{ ok, markdown, meta: { filename, extension, size_bytes, size_label, characters, words, model, seconds } }`

`GET /healthz` → `{ "status": "ok", "model": "..." }`

### Architecture & Memory Strategy
1. **Lazy, process-wide singletons** — `OpenAI` client + `MarkItDown` are built once per worker.
2. **Disk-backed uploads** — streamed to `tempfile` in 1 MB chunks; payload is never fully buffered in RAM.
3. **Security first** — Upload allow-list, random temp names, strict DOMPurify sanitization.
</details>

---
<div align="center">
  <p>Made with ❤️ by <b>Shriful Islam (InHumanZ)</b></p>
</div>