"""Backend smoke test for OmniMark (no external LLM calls - forces empty API key)."""
import io
import os
import struct
import zlib
import sys

os.environ["API_KEY"] = ""

from app import app, _clean_fragmented_tables  # noqa: E402

c = app.test_client()

failures = []


def check(name, cond, extra=""):
    print(f"[{ 'OK ' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        failures.append(name)


# 1) healthz
r = c.get("/healthz")
check("healthz", r.status_code == 200 and r.get_json()["status"] == "ok", r.status_code)

# 2) home page renders
r = c.get("/")
check("index-render", r.status_code == 200 and b"OmniMark" in r.data, f"{r.status_code} len={len(r.data)}")

# 3) convert-text
r = c.post("/convert-text", json={"content": "Line one\r\n\r\n\r\nLine two\n  \nLine three"})
j = r.get_json()
check("convert-text", r.status_code == 200 and j.get("ok"), f"{r.status_code} {j}")
check("convert-text-blanks", "Line two\n\nLine three" in (j or {}).get("markdown", ""), repr((j or {}).get("markdown")))

# 3b) convert-text empty
r = c.post("/convert-text", json={"content": "   "})
check("convert-text-empty", r.status_code == 400, r.status_code)

# 4) convert-url validation
r = c.post("/convert-url", json={"url": "file:///etc/passwd"})
check("url-invalid-scheme", r.status_code == 400, r.status_code)
r = c.post("/convert-url", json={"url": "http://127.0.0.1/secret"})
check("url-private-ip", r.status_code == 400, r.status_code)
r = c.post("/convert-url", json={})
check("url-empty", r.status_code == 400, r.status_code)

# 5) file upload (.md) - markitdown handles it locally, no key needed
r = c.post(
    "/convert",
    data={"file": (io.BytesIO(b"# Title\n\nSome **bold** text."), "sample.md")},
    content_type="multipart/form-data",
)
j = r.get_json()
check("file-md", r.status_code == 200 and j.get("ok"), f"{r.status_code} {j}")

# 6) file upload (.txt)
r = c.post(
    "/convert",
    data={"file": (io.BytesIO(b"plain text hello\nsecond line"), "notes.txt")},
    content_type="multipart/form-data",
)
j = r.get_json()
check("file-txt", r.status_code == 200 and j.get("ok"), f"{r.status_code} {j}")

# 7) tiny PNG without API key -> clean 422 (must NOT crash / 500)
def _ch(tag, data):
    blob = tag + data
    return struct.pack(">I", len(data)) + blob + struct.pack(">I", zlib.crc32(blob))


png_bytes = (
    b"\x89PNG\r\n\x1a\n"
    + _ch(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    + _ch(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
    + _ch(b"IEND", b"")
)
r = c.post(
    "/convert",
    data={"file": (io.BytesIO(png_bytes), "tiny.png")},
    content_type="multipart/form-data",
)
j = r.get_json() or {}
# Should be a clean application-level error (422) and NEVER a 500 crash.
check("file-png-no-key", r.status_code == 422, f"{r.status_code} {j.get('error','')[:70]}")

# 8) fragmented-table cleaner
md = "| col A  | col B  |\n|---|---|\n|  |  |\n| x  | y  |\n|  |   |"
out = _clean_fragmented_tables(md)
check("cleaner-drops-fragments", "| |" not in out and out.count("| x | y |") == 1, repr(out))
check("cleaner-keeps-header", "| col A" in out, repr(out))

# 9) raw text fallback
import tempfile as _tf
from app import _raw_text_fallback, _extract_pdf_text  # noqa: E402

p = _tf.mktemp(suffix=".txt")
with open(p, "wb") as fh:
    fh.write("héllo wörld".encode("utf-8"))
check("raw-fallback-utf8", _raw_text_fallback(p) == "héllo wörld", repr(_raw_text_fallback(p)))
os.remove(p)

# 10) minimal PDF extraction fallback
pdf_objects = []
pdf_objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
pdf_objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
pdf_objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>")
pdf_stream = b"BT /F1 24 Tf 100 700 Td (Hello from pdf) Tj ET"
pdf_objects.append(b"<< /Length %d >>\nstream\n" % len(pdf_stream) + pdf_stream + b"\nendstream")
pdf_objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

out = b"%PDF-1.4\n"
offsets = []
for obj in pdf_objects:
    offsets.append(len(out))
    out += b"%d 0 obj\n" % (len(offsets))
    out += obj + b"\nendobj\n"
xref_pos = len(out)
out += b"xref\n0 %d\n" % (len(offsets) + 1)
out += b"0000000000 65535 f \n"
for off in offsets:
    out += b"%010d 00000 n \n" % off
out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(offsets) + 1, xref_pos)

pdf_path = _tf.mktemp(suffix=".pdf")
with open(pdf_path, "wb") as fh:
    fh.write(out)
try:
    txt = _extract_pdf_text(pdf_path)
    check("pdf-extract-fallback", "Hello from pdf" in txt, repr(txt))
except Exception as exc:  # noqa: BLE001
    check("pdf-extract-fallback", False, f"{type(exc).__name__}: {exc}")
finally:
    os.remove(pdf_path)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL SMOKE TESTS PASSED")