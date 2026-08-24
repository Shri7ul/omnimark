"""Live-ish integration checks: /convert-url on a public page + /convert for a real PDF."""
import io
import os
import tempfile

from app import app

c = app.test_client()

# 1) Live URL fetch + HTML -> Markdown (MarkItDown HTML converter; no LLM call)
r = c.post("/convert-url", json={"url": "https://example.com/"})
j = r.get_json() or {}
print("url-live", r.status_code, "ok=", bool(j.get("ok")), repr((j.get("markdown") or "")[:90]))

# 2) End-to-end PDF via /convert (exercises MarkItDown PDF + fallback chain)
objects = []
objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
objects.append(
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
    b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
)
s = b"BT /F1 24 Tf 100 700 Td (Hello from PDF) Tj ET"
objects.append(b"<< /Length %d >>\nstream\n" % len(s) + s + b"\nendstream")
objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

out = b"%PDF-1.4\n"
offs = []
for o in objects:
    offs.append(len(out))
    out += b"%d 0 obj\n" % len(offs)
    out += o + b"\nendobj\n"
xref = len(out)
out += b"xref\n0 %d\n" % (len(offs) + 1) + b"0000000000 65535 f \n"
for off in offs:
    out += b"%010d 00000 n \n" % off
out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(offs) + 1, xref)

p = os.path.join(os.environ.get("TEMP", "."), "_omt_live.pdf")
with open(p, "wb") as fh:
    fh.write(out)

with open(p, "rb") as fh:
    r = c.post(
        "/convert",
        data={"file": (io.BytesIO(fh.read()), "doc.pdf")},
        content_type="multipart/form-data",
    )
j = r.get_json() or {}
print("file-pdf", r.status_code, "ok=", bool(j.get("ok")), repr((j.get("markdown") or "")[:80]))
os.remove(p)
print("done")