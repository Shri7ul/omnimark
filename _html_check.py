"""Render index.html via Jinja and sanity-check the new multi-mode markup/JS exist."""
import re
from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader("templates"))
html = env.get_template("index.html").render(model="qwen/qwen3.8-max-free", max_mb=25, configured=True)
with open("_rendered.html", "w", encoding="utf-8") as fh:
    fh.write(html)

checks = {
    "modeFile container": 'id="modeFile"' in html,
    "modeUrl container": 'id="modeUrl"' in html,
    "modeText container": 'id="modeText"' in html,
    "urlInput field": 'id="urlInput"' in html,
    "textInput field": 'id="textInput"' in html,
    "mode tabs present": all(f'id="tab{k}"' in html for k in ("File", "Url", "Text")),
    "mode buttons present": all(f'id="btnConvert{k}"' in html for k in ("", "Url", "Text")),
    "post /convert-url wired": "/convert-url" in html,
    "post /convert-text wired": "/convert-text" in html,
    "mode switch JS": "function switchMode" in html,
    "runRequest JS": "function runRequest" in html,
    "no leftover dropShell": "dropShell" not in html,
}

all_ok = True
for name, ok in checks.items():
    print(("[OK ]" if ok else "[FAIL]"), name)
    all_ok = all_ok and ok

# Basic JS balance: count braces roughly inside the single <script> block
m = re.search(r"<script>\s*(\(function \(\) \{.*?\}\)\(\));\s*</script>", html, re.S)
print("[OK ] script IIFE block found" if m else "[FAIL] script IIFE block found")
if m:
    body = m.group(1)
    print("[OK ] braces balanced" if body.count("{") == body.count("}") else "[FAIL] braces unbalanced")
    print("[OK ] parens balanced" if body.count("(") == body.count(")") else "[FAIL] parens unbalanced")

print("ALL_HTML_CHECKS_PASSED" if all_ok and m else "SOME_CHECKS_FAILED")