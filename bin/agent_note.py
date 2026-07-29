#!/usr/bin/env python
"""
Headless note writer.

The transcription pipeline stops at `transcript.md`. Everything after that —
cleaning the transcript, writing the meeting notes, deciding what goes in the
action-item list — needs a language model. Until now that meant a human sat in
a chat window and said "整理這場會議".

This script removes that dependency: it shells out to a local coding-agent CLI
(Claude Code or OpenAI Codex) in non-interactive mode and lets *it* write the
markdown, then does the deterministic part (PDF/Word conversion, delivery
manifest, notification) in Python.

Split of responsibilities, deliberately:

    agent CLI  ->  .md and .json only   (judgement, language, structure)
    this file  ->  .pdf / .docx / delivery / notify   (mechanical, verifiable)

The agent is never asked to run a shell command. If it writes a bad file we
still convert what exists and report the gap, rather than silently shipping a
half-finished job.

    bin/agent_note.py <job_dir> [--backend claude|codex] [--deliver]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG, ROOT  # noqa: E402

SPEC_PATH = ROOT / "templates" / "NOTE_SPECS.md"
TASK_TEMPLATE = ROOT / "templates" / "AGENT_TASK.md"
PY = str((ROOT / ".venv" / "bin" / "python")) if (ROOT / ".venv" / "bin" / "python").exists() else sys.executable

FMT_NAME = {"md": "Markdown", "pdf": "PDF", "docx": "Word"}

# stem -> (label, make_pdf --kind, make_docx --kind)
DOC_KINDS = {
    "transcript_clean": ("整理過的逐字稿", "general", "transcript"),
    "note_general": ("會議記錄", "general", "general"),
    "note_client": ("會議記錄（客戶版）", "client", "client"),
    "note_self": ("會議記錄（內部覆盤）", "self", "self"),
    "note_partner": ("會議記錄（夥伴版）", "partner", "partner"),
    "note_interview": ("訪談紀要", "general", "general"),
}

NOTE_STEMS = {
    "general": ["note_general"],
    "client": ["note_client", "note_self", "note_partner"],
    "interview": ["note_interview"],
}


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def build_plan(meta: dict) -> dict:
    """Resolve meta.json into a concrete list of documents and formats.

    Mirrors the fallback rules in NOTE_SPECS.md: unknown meeting_type degrades
    to `general`, an empty format list degrades to md+pdf, and a job that asks
    for nothing at all still gets a meeting note (zero output is never right).
    """
    want_note = bool(meta.get("want_note", True))
    want_tr = bool(meta.get("want_transcript", False))
    if not want_note and not want_tr:
        want_note = True

    mtype = (meta.get("meeting_type") or "general").strip() or "general"
    if mtype not in NOTE_STEMS:
        mtype = "general"

    formats = [f for f in ("md", "pdf", "docx") if f in (meta.get("formats") or [])] or ["md", "pdf"]

    stems: list[str] = []
    if want_tr:
        stems.append("transcript_clean")
    if want_note:
        stems.extend(NOTE_STEMS[mtype])

    return {
        "want_note": want_note,
        "want_transcript": want_tr,
        "meeting_type": mtype,
        "formats": formats,
        "stems": stems,
    }


def build_prompt(job_dir: Path, meta: dict, result: dict, plan: dict) -> str:
    transcript = job_dir / "transcript.md"
    chars = len(transcript.read_text(errors="ignore")) if transcript.exists() else 0

    lines = []
    for stem in plan["stems"]:
        label = DOC_KINDS[stem][0]
        lines.append(f"- `{stem}.md` — {label}")
    if plan["want_note"]:
        lines.append("- `action_items.json` — 決議／待辦／風險／關鍵數字（結構化）")
    lines.append("- `INDEX.md` — 檔案清單與機密層級")

    num_speakers = result.get("num_speakers") or meta.get("num_speakers") or "?"
    declared = str(meta.get("num_speakers") or "").strip()
    warning = ""
    try:
        if declared and int(num_speakers) > max(int(declared) * 2, int(declared) + 3):
            warning = (
                f"> ⚠️ **聲紋分群不可靠**：使用者填 {declared} 位講者，系統卻切出 {num_speakers} 群"
                f"（線上會議錄音常見）。逐字稿的 speaker 標籤不可信，請改用發言內容、人稱與角色"
                f"重新判斷是誰在講，並在文件開頭註明「講者對應為推測」。"
            )
    except (TypeError, ValueError):
        pass

    dur = float(result.get("duration") or 0)
    subs = {
        "ROOT": str(ROOT),
        "JOB_DIR": str(job_dir),
        "SPEC_PATH": str(SPEC_PATH),
        "TRANSCRIPT_PATH": str(transcript),
        "META_PATH": str(job_dir / "meta.json"),
        "TITLE": result.get("title") or meta.get("title") or "未命名會議",
        "CLIENT": result.get("client") or meta.get("client") or "—",
        "DATE": result.get("date") or meta.get("date") or "",
        "DURATION": f"{int(dur)//3600:02d}:{(int(dur)%3600)//60:02d}:{int(dur)%60:02d}",
        "PARTICIPANTS": result.get("participants") or meta.get("participants") or "未提供",
        "NUM_SPEAKERS": str(num_speakers),
        "TRANSCRIPT_CHARS": f"{chars:,}",
        "SPEAKER_WARNING": warning,
        "DELIVERABLES": "\n".join(lines),
        # This entry point skips the review stage entirely, so there is nothing
        # confirmed and the source is always the raw transcript. review.py
        # fills both properly; here they are stated rather than left as
        # unsubstituted placeholders in the prompt.
        "SOURCE_DESC": "逐字稿（未經清稿）",
        "CONFIRMED": (
            "## 已確認事實\n\n"
            "（本次未經使用者確認流程，沒有任何已確認事實——"
            "講者姓名、專有名詞、金額日期一律依逐字稿推斷，"
            "並在文件開頭註明「講者對應為推測」。）\n"
        ),
    }

    text = TASK_TEMPLATE.read_text()
    for key, val in subs.items():
        text = text.replace("{{" + key + "}}", str(val))

    ctx = (meta.get("context") or "").strip()
    if ctx:
        text += f"\n## 使用者提供的背景／專有名詞\n\n{ctx}\n"
    return text


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #
def resolve_bin(backend: str, override: str | None) -> str:
    if override:
        path = os.path.expanduser(override)
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            raise SystemExit(f"[agent] configured agent.bin is not executable: {path}")
        return path
    found = shutil.which(backend)
    if not found:
        raise SystemExit(
            f"[agent] '{backend}' not found on PATH.\n"
            f"        install it, or set agent.bin in config.json to the full path.\n"
            f"        claude: npm i -g @anthropic-ai/claude-code\n"
            f"        codex:  npm i -g @openai/codex"
        )
    return found


def backend_cmd(backend: str, binary: str, prompt: str, model: str | None) -> list[str]:
    """Non-interactive invocation for each supported CLI.

    Both are pinned to read/write-only tooling: the agent writes markdown, it
    does not get a shell. `--add-dir` / `-C` point at the repo root so the spec
    file and the job folder are both in scope.
    """
    if backend == "claude":
        cmd = [
            binary, "-p", prompt,
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read,Write,Edit,Glob,Grep",
            "--add-dir", str(ROOT),
        ]
        if model:
            cmd += ["--model", model]
        return cmd

    if backend == "codex":
        cmd = [
            binary, "exec",
            "--sandbox", "workspace-write",
            "--skip-git-repo-check",
            "-C", str(ROOT),
        ]
        if model:
            cmd += ["-m", model]
        return cmd + [prompt]

    raise SystemExit(f"[agent] unknown backend '{backend}' (expected: claude, codex)")


def run_agent(cmd: list[str], log_path: Path, timeout: int) -> tuple[int, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with open(log_path, "a") as log:
        log.write(f"\n===== {time.strftime('%F %T')} :: {cmd[0]} =====\n")
        log.flush()
        try:
            proc = subprocess.run(
                cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            log.write(f"\n[agent] TIMEOUT after {timeout}s\n")
            rc = 124
    return rc, f"{time.time() - started:.1f}"


# --------------------------------------------------------------------------- #
# conversion + delivery
# --------------------------------------------------------------------------- #
def convert(job_dir: Path, stem: str, fmt: str, result: dict, meta: dict) -> tuple[bool, str]:
    src = job_dir / f"{stem}.md"
    out = job_dir / f"{stem}.{fmt}"
    script = ROOT / "bin" / ("make_pdf.py" if fmt == "pdf" else "make_docx.py")
    kind = DOC_KINDS[stem][1 if fmt == "pdf" else 2]
    dur = float(result.get("duration") or 0)
    cmd = [
        PY, str(script), str(src), "--out", str(out), "--kind", kind,
        "--title", str(result.get("title") or meta.get("title") or ""),
        "--client", str(result.get("client") or meta.get("client") or ""),
        "--date", str(result.get("date") or meta.get("date") or ""),
        "--participants", str(result.get("participants") or meta.get("participants") or ""),
        "--duration", f"{int(dur)//3600:02d}:{(int(dur)%3600)//60:02d}:{int(dur)%60:02d}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        return False, (proc.stderr or proc.stdout or "converter failed").strip()[:300]
    return True, str(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Write meeting documents with a local coding-agent CLI.")
    ap.add_argument("job_dir", help="archive/<job_id> (or 'latest')")
    ap.add_argument("--backend", choices=["claude", "codex"], default=None)
    ap.add_argument("--bin", default=None, help="full path to the CLI (overrides PATH lookup)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=None, help="seconds")
    ap.add_argument("--deliver", action="store_true", help="send the finished files via the configured notifier")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt and exit")
    args = ap.parse_args()

    acfg = CONFIG.get("agent", {})
    backend = args.backend or acfg.get("backend") or "claude"
    timeout = args.timeout or int(acfg.get("timeout_sec") or 3600)
    model = args.model or acfg.get("model")

    if args.job_dir == "latest":
        jobs = sorted((ROOT / "archive").glob("*/"), key=lambda p: p.stat().st_mtime)
        if not jobs:
            raise SystemExit("[agent] archive/ is empty")
        job_dir = jobs[-1].resolve()
    else:
        job_dir = Path(args.job_dir).expanduser().resolve()
    if not (job_dir / "transcript.md").exists():
        raise SystemExit(f"[agent] no transcript.md in {job_dir}")

    meta = read_json(job_dir / "meta.json")
    result = read_json(job_dir / "status.json").get("result") or {}
    plan = build_plan(meta)
    prompt = build_prompt(job_dir, meta, result, plan)

    if args.dry_run:
        print(prompt)
        return 0

    binary = resolve_bin(backend, args.bin or acfg.get("bin"))
    log_path = ROOT / "logs" / f"{job_dir.name}-agent.log"
    print(f"[agent] {backend} -> {job_dir.name}  ({', '.join(plan['stems'])}; {'/'.join(plan['formats'])})")
    print(f"[agent] log: {log_path}")

    rc, elapsed = run_agent(backend_cmd(backend, binary, prompt, model), log_path, timeout)
    print(f"[agent] {backend} exited {rc} after {elapsed}s")

    # Verify against the plan rather than trusting the exit code: a CLI can
    # exit 0 having written nothing, and can exit non-zero having written
    # everything. Only the files on disk decide.
    delivery: list[dict] = []
    missing: list[str] = []
    failed: list[str] = []
    for stem in plan["stems"]:
        md = job_dir / f"{stem}.md"
        if not md.exists():
            missing.append(f"{stem}.md")
            continue
        for fmt in plan["formats"]:
            if fmt == "md":
                delivery.append({"stem": stem, "fmt": "md", "path": str(md)})
                continue
            ok, info = convert(job_dir, stem, fmt, result, meta)
            if ok:
                delivery.append({"stem": stem, "fmt": fmt, "path": info})
            else:
                failed.append(f"{stem}.{fmt}: {info}")

    report = {
        "job_id": job_dir.name,
        "backend": backend,
        "model": model,
        "exit_code": rc,
        "elapsed_sec": float(elapsed),
        "plan": plan,
        "delivery": delivery,
        "missing": missing,
        "convert_failed": failed,
        "agent_report": read_json(job_dir / "agent_report.json"),
        "ok": not missing and not failed,
    }
    (job_dir / "delivery.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    for line in missing:
        print(f"[agent] MISSING  {line}")
    for line in failed:
        print(f"[agent] CONVERT  {line}")
    print(f"[agent] {len(delivery)} file(s) ready -> {job_dir / 'delivery.json'}")

    if args.deliver:
        notify(job_dir, report, result, meta)
    return 0 if report["ok"] else 1


def notify(job_dir: Path, report: dict, result: dict, meta: dict) -> None:
    ncfg = CONFIG.get("notify", {})
    binary = Path(os.path.expanduser(ncfg.get("bin", "")))
    if not ncfg.get("enabled", True) or not (binary.is_file() and os.access(binary, os.X_OK)):
        (ROOT / "logs" / "undelivered.log").write_text(json.dumps(report, ensure_ascii=False), )
        print("[agent] notifier unavailable; report parked in logs/undelivered.log")
        return

    title = result.get("title") or meta.get("title") or "未命名會議"
    fmts = "＋".join(FMT_NAME[f] for f in report["plan"]["formats"])
    ar = report.get("agent_report") or {}

    head = [f"📝 **{title}** 文件產出完成", ""]
    head.append(f"由 `{report['backend']}` 在本機背景撰寫 · 耗時 {report['elapsed_sec']:.0f}s · {fmts}")
    if ar.get("notes"):
        head.append(f"\n{ar['notes']}")
    if ar.get("uncertain"):
        head.append("\n**待確認：**")
        head += [f"- {u}" for u in ar["uncertain"][:5]]
    if report["missing"] or report["convert_failed"]:
        head.append("\n⚠️ 未完成：" + "、".join(report["missing"] + report["convert_failed"])[:300])
    head.append(f"\nJob: `{report['job_id']}`")

    msg = "\n".join(head) + "\n" + "\n".join(f"MEDIA:{d['path']}" for d in report["delivery"])
    proc = subprocess.run([str(binary), "send", "--to", ncfg.get("target", "telegram"), msg],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        with open(ROOT / "logs" / "undelivered.log", "a") as fh:
            fh.write(msg + "\n")
        print(f"[agent] delivery failed ({proc.stderr.strip()[:200]}); parked in logs/undelivered.log")
    else:
        print(f"[agent] delivered {len(report['delivery'])} file(s)")


if __name__ == "__main__":
    sys.exit(main())
