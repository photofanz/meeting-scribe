#!/usr/bin/env python
"""
Markdown meeting note -> Word (.docx) via pandoc.

Mirrors make_pdf.py's CLI and header block so the three formats
(md / pdf / docx) carry the same masthead, facts table and footer.

  make_docx.py note.md --out note.docx --kind general \
      --title "..." --client "..." --date 2026-07-29

Word is the "hand it over and let them edit" format, so fonts are chosen
for cross-platform Office (Microsoft JhengHei ships with Word on both
macOS and Windows) rather than macOS-only PingFang TC.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG  # noqa: E402

REF = Path(__file__).resolve().parent.parent / "templates" / "reference.docx"

KINDS = {
    "general":    ("會議記錄", "會議記錄 · 內容以現場討論為準 · 可分送與會者"),
    "client":     ("會議紀要", CONFIG["branding"]["client_footer"]),
    "self":       ("內部覆盤筆記", "內部專用 · 嚴禁外傳 · 含主觀評估與客戶情報"),
    "partner":    ("夥伴補課摘要", "合作夥伴限閱 · 請勿轉發客戶"),
    "transcript": ("逐字稿（已校訂）", "已校訂逐字稿 · 內容以錄音為準 · 引用前請回查音檔"),
}

CJK = "Microsoft JhengHei"
LATIN = "Calibri"


def build_reference(dst: Path) -> Path:
    """Create a reference.docx with CJK-safe fonts, once, and cache it."""
    if dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = subprocess.run(["pandoc", "--print-default-data-file", "reference.docx"],
                         capture_output=True, check=True).stdout
    tmp = dst.with_suffix(".raw.docx")
    tmp.write_bytes(raw)

    font_re = re.compile(rb'<w:rFonts[^/>]*/>')

    def patch_fonts(xml: bytes) -> bytes:
        repl = (f'<w:rFonts w:ascii="{LATIN}" w:hAnsi="{LATIN}" '
                f'w:eastAsia="{CJK}" w:cs="{CJK}"/>').encode()
        return font_re.sub(repl, xml)

    with zipfile.ZipFile(tmp) as zin, \
         zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename in ("word/styles.xml", "word/theme/theme1.xml"):
                data = patch_fonts(data)
            zout.writestr(item, data)
    tmp.unlink(missing_ok=True)
    return dst


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


def normalize_for_pandoc(md: str) -> str:
    """Keep generated markdown away from Pandoc's YAML front-matter parser.

    Our transcripts and notes legitimately use `---` as horizontal rules inside
    the body. Pandoc treats a bare `---` near the top of a document as the start
    of a YAML metadata block and aborts on the next non-YAML line. Rewriting
    thematic breaks to the equivalent `* * *` keeps the visual output identical
    while removing the ambiguity.
    """
    return re.sub(r"(?m)^-{3,}\s*$", "* * *", md)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("markdown")
    ap.add_argument("--out", required=True)
    ap.add_argument("--kind", default="general", choices=list(KINDS))
    ap.add_argument("--title", default="")
    ap.add_argument("--client", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--participants", default="")
    ap.add_argument("--duration", default="")
    a = ap.parse_args()

    if not shutil.which("pandoc"):
        print("pandoc not found — cannot build .docx", file=sys.stderr)
        sys.exit(2)

    md = Path(a.markdown).read_text()
    md, h1 = strip_leading_h1(md)
    md = normalize_for_pandoc(md)
    title = a.title or h1 or "會議紀錄"
    kind_label, confidential = KINDS[a.kind]

    rows = [("日期", a.date), ("客戶／對象", a.client),
            ("與會", a.participants), ("時長", a.duration)]
    facts = [f"| {k} | {v} |" for k, v in rows if v]

    head = [f"# {title}", "", f"**{kind_label}**"]
    if facts:
        head += ["", "| | |", "|---|---|", *facts]
    # `* * *` not `---`: a bare `---` after a blank line makes pandoc try to
    # parse a YAML metadata block and abort.
    head += ["", "* * *", ""]

    doc = "\n".join(head) + md.lstrip("\n") + \
          f"\n\n* * *\n\n*{confidential}*\n"

    out = Path(a.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    ref = build_reference(REF)

    r = subprocess.run(
        ["pandoc", "-f", "markdown+pipe_tables+hard_line_breaks",
         "-t", "docx", f"--reference-doc={ref}", "-o", str(out)],
        input=doc, capture_output=True, text=True)
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        print(r.stderr[-1500:], file=sys.stderr)
        sys.exit(1)
    print(str(out))


if __name__ == "__main__":
    main()
