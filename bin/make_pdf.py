#!/usr/bin/env python
"""
Markdown meeting note -> branded HTML -> PDF (headless Chrome).

Chrome is used instead of LaTeX/weasyprint because it gives full CSS control
(and PingFang TC renders correctly out of the box on macOS).

  make_pdf.py note.md --out note.pdf --kind client \
      --title "..." --client "..." --date 2026-07-29
"""
from __future__ import annotations

import argparse
import html as _html
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG  # noqa: E402

# Any Chromium-family binary works; the first one present wins.
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


def find_chrome() -> str | None:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    return shutil.which("chromium") or shutil.which("google-chrome")


# launchd sets Homebrew on PATH; SSH and some CLI invocations do not.
# shutil.which("pandoc") alone then fails even though pandoc is installed.
PANDOC_CANDIDATES = (
    "/opt/homebrew/bin/pandoc",
    "/usr/local/bin/pandoc",
)


def find_pandoc() -> str | None:
    found = shutil.which("pandoc")
    if found:
        return found
    for c in PANDOC_CANDIDATES:
        if Path(c).is_file():
            return c
    return None


CHROME = find_chrome() or CHROME_CANDIDATES[0]

KINDS = {
    "general": ("會議記錄", "會議記錄 · 內容以現場討論為準 · 可分送與會者"),
    "client":  ("會議紀要", CONFIG["branding"]["client_footer"]),
    "self":    ("內部覆盤筆記", "內部專用 · 嚴禁外傳 · 含主觀評估與客戶情報"),
    "partner": ("夥伴補課摘要", "合作夥伴限閱 · 請勿轉發客戶"),
}

CSS = r"""
@page { size: A4; margin: 22mm 18mm 20mm; }
:root{
  --ink:#14181d; --ink-2:#3c4650; --ink-3:#7b858f;
  --accent:#2c4a6e; --rule:#e3e6ea; --wash:#f7f8fa;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  font-family:"PingFang TC","SF Pro Text",-apple-system,"Helvetica Neue",sans-serif;
  color:var(--ink); font-size:10.5pt; line-height:1.85;
  -webkit-font-smoothing:antialiased; letter-spacing:.005em;
}
.masthead{
  display:flex; justify-content:space-between; align-items:baseline;
  border-bottom:1px solid var(--ink); padding-bottom:9px; margin-bottom:34px;
}
.brand{font-size:11pt;font-weight:600;letter-spacing:.18em}
.kind{font-size:8.5pt;color:var(--ink-3);letter-spacing:.14em}

h1.doctitle{
  font-size:23pt; font-weight:600; line-height:1.28;
  letter-spacing:-.015em; margin:0 0 10px;
}
.deck{color:var(--ink-3); font-size:9pt; letter-spacing:.05em; margin-bottom:30px}

.facts{width:100%; border-collapse:collapse; margin:0 0 38px}
.facts td{padding:7px 0; border-bottom:1px solid var(--rule); vertical-align:top}
.facts td:first-child{
  width:82px; color:var(--ink-3); font-size:8.5pt; letter-spacing:.1em;
  white-space:nowrap; padding-right:16px;
}

h2{
  font-size:12.5pt; font-weight:600; letter-spacing:-.005em;
  margin:34px 0 12px; padding-top:14px; border-top:1px solid var(--rule);
  page-break-after:avoid;
}
h2:first-of-type{border-top:0;padding-top:0}
h3{font-size:10.5pt;font-weight:600;color:var(--accent);margin:20px 0 7px;page-break-after:avoid}
p{margin:0 0 11px}
ul,ol{margin:0 0 13px;padding-left:1.25em}
li{margin-bottom:5px;padding-left:.15em}
li::marker{color:var(--ink-3)}
strong{font-weight:600}
blockquote{
  margin:16px 0; padding:2px 0 2px 16px;
  border-left:2px solid var(--accent); color:var(--ink-2); font-style:normal;
}
code{background:var(--wash);padding:1px 5px;border-radius:3px;font-size:9pt}
hr{border:0;border-top:1px solid var(--rule);margin:26px 0}

table{width:100%;border-collapse:collapse;margin:14px 0 20px;font-size:9.5pt}
thead th{
  text-align:left; font-weight:600; font-size:8.5pt; letter-spacing:.08em;
  color:var(--ink-3); border-bottom:1px solid var(--ink); padding:8px 10px 7px;
}
tbody td{padding:9px 10px;border-bottom:1px solid var(--rule);vertical-align:top}
tbody tr:last-child td{border-bottom:1px solid var(--ink)}

.footnote{
  margin-top:40px; padding-top:11px; border-top:1px solid var(--rule);
  font-size:7.5pt; color:var(--ink-3); letter-spacing:.04em;
}
h2,h3,blockquote{page-break-inside:avoid}
/* Long tables may split across pages, but never mid-row, and the header
   repeats — otherwise a 12-row action-item table leaves half a page blank. */
table{page-break-inside:auto}
thead{display:table-header-group}
tr{page-break-inside:avoid}
"""

TEMPLATE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<style>{css}</style></head><body>
<div class="masthead"><div class="brand">{brand}</div>
<div class="kind">{kind_label}</div></div>
<h1 class="doctitle">{title}</h1>
<div class="deck">{deck}</div>
<table class="facts">{facts}</table>
{body}
<div class="footnote">{confidential}</div>
</body></html>"""


def md_to_html(md: str) -> str:
    """Prefer pandoc (best tables); fall back to python-markdown."""
    pandoc = find_pandoc()
    if pandoc:
        r = subprocess.run(
            [pandoc, "-f", "markdown+pipe_tables+hard_line_breaks",
             "-t", "html", "--no-highlight"],
            input=md, capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
    import markdown  # noqa: PLC0415
    return markdown.markdown(md, extensions=["tables", "nl2br"])


def strip_leading_h1(md: str) -> tuple[str, str | None]:
    lines = md.splitlines()
    title = None
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            title = ln[2:].strip()
            del lines[i]
            break
        if ln.strip():
            break
    return "\n".join(lines), title


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("markdown")
    ap.add_argument("--out", required=True)
    ap.add_argument("--kind", default="general", choices=list(KINDS))
    ap.add_argument("--title", default="")
    ap.add_argument("--client", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--participants", default="")
    ap.add_argument("--duration", default="")
    ap.add_argument("--keep-html", action="store_true")
    a = ap.parse_args()

    md = Path(a.markdown).read_text()
    md, h1 = strip_leading_h1(md)
    title = a.title or h1 or "會議紀錄"
    kind_label, confidential = KINDS[a.kind]

    rows = [("日期", a.date), ("客戶", a.client),
            ("與會", a.participants), ("時長", a.duration)]
    facts = "".join(
        f"<tr><td>{_html.escape(k)}</td><td>{_html.escape(v)}</td></tr>"
        for k, v in rows if v)

    doc = TEMPLATE.format(
        css=CSS, brand=_html.escape(CONFIG["branding"]["brand_name"]),
        kind_label=kind_label, title=_html.escape(title),
        deck=_html.escape(f"{a.client}　·　{a.date}".strip("　·　")),
        facts=facts, body=md_to_html(md), confidential=confidential)

    out = Path(a.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    hpath = out.with_suffix(".html")
    hpath.write_text(doc)

    if not Path(CHROME).exists():
        print(f"Chrome not found at {CHROME}; wrote HTML only: {hpath}",
              file=sys.stderr)
        sys.exit(2)

    # Chrome writes the PDF then sometimes fails to exit, so don't wait on it:
    # poll until the file stops growing, then kill.
    out.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as prof:
        proc = subprocess.Popen(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox",
             "--disable-extensions", "--disable-background-networking",
             f"--user-data-dir={prof}",
             "--no-pdf-header-footer", "--print-to-pdf-no-header",
             f"--print-to-pdf={out}", f"file://{hpath}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        stable, last = 0, -1
        for _ in range(600):                       # up to 60 s
            if proc.poll() is not None:
                break
            if out.exists():
                sz = out.stat().st_size
                stable = stable + 1 if sz == last and sz > 0 else 0
                last = sz
                if stable >= 5:                    # unchanged for ~0.5 s
                    break
            time.sleep(0.1)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    if not out.exists() or out.stat().st_size == 0:
        print((proc.stderr.read() if proc.stderr else "")[-1500:], file=sys.stderr)
        sys.exit(1)
    if not a.keep_html:
        hpath.unlink(missing_ok=True)
    print(str(out))


if __name__ == "__main__":
    main()
