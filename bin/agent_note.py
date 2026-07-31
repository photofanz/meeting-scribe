#!/usr/bin/env python
"""
Headless note writer.

The transcription pipeline stops at `transcript.md`. Everything after that —
cleaning the transcript, writing the meeting notes, deciding what goes in the
action-item list — needs a language model. Until now that meant a human sat in
a chat window and said "整理這場會議".

This script removes that dependency: it drives either a local coding-agent CLI
(Claude Code / OpenAI Codex) or an OpenAI-compatible API backend (for example
LM Studio on another machine), then does the deterministic part (PDF/Word
conversion, delivery manifest, notification) in Python.

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
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG, ROOT, resolve_agent_config  # noqa: E402
from lmstudio_runtime import schedule_cleanup, touch_activity  # noqa: E402

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

CLI_BACKENDS = {"claude", "codex"}
API_BACKENDS = {"openai_compat"}
ALL_BACKENDS = CLI_BACKENDS | API_BACKENDS


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


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def loads_loose(text: str) -> dict | None:
    if not text or not text.strip():
        return None
    for candidate in (text, _FENCE.sub("", text)):
        try:
            val = json.loads(candidate)
            return val if isinstance(val, dict) else None
        except Exception:  # noqa: BLE001
            pass
    start, depth = text.find("{"), 0
    if start < 0:
        return None
    in_str = esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    val = json.loads(text[start:i + 1])
                    return val if isinstance(val, dict) else None
                except Exception:  # noqa: BLE001
                    return None
    return None


def build_api_writer_prompt(job_dir: Path, meta: dict, result: dict, plan: dict,
                            source_path: Path, source_desc: str, confirmed: str,
                            user_context: str = "", speaker_warning: str = "") -> str:
    spec = SPEC_PATH.read_text(errors="ignore")
    source_text = source_path.read_text(errors="ignore")
    meta_text = json.dumps(meta, ensure_ascii=False, indent=2)
    stems = [s for s in plan["stems"] if s != "transcript_clean"]
    files = [f"{s}.md" for s in stems] + ["action_items.json", "INDEX.md", "agent_report.json"]
    dur = float(result.get("duration") or 0)
    parts = [
        "你是 meeting-scribe 的會議文件撰寫者，正在 API 模式下執行。",
        "你沒有 Read / Write / Edit / shell 工具，也不能讀檔；所有需要的資料都已內嵌在這個 prompt。",
        "請根據規格與來源內容，直接回傳要落盤的檔案內容。",
        "",
        "## 會議資訊",
        f"- 標題：{result.get('title') or meta.get('title') or '未命名會議'}",
        f"- 對象：{result.get('client') or meta.get('client') or '—'}",
        f"- 日期：{result.get('date') or meta.get('date') or ''}",
        f"- 長度：{int(dur)//3600:02d}:{(int(dur)%3600)//60:02d}:{int(dur)%60:02d}",
        f"- 與會者：{result.get('participants') or meta.get('participants') or '未提供'}",
        f"- 聲紋群數：{result.get('num_speakers') or meta.get('num_speakers') or '?'}",
        f"- 來源：{source_desc}",
        "",
    ]
    if speaker_warning:
        parts += [speaker_warning, ""]
    if confirmed.strip():
        parts += [confirmed.strip(), ""]
    if user_context.strip():
        parts += [user_context.strip(), ""]

    parts += [
        "## 規格（全文內嵌）",
        "```markdown",
        spec,
        "```",
        "",
        "## meta.json（內嵌）",
        "```json",
        meta_text,
        "```",
        "",
        f"## {source_desc}（內嵌）",
        "```markdown",
        source_text,
        "```",
        "",
        "## 允許輸出的檔案",
        "只允許下列檔名，不能多也不能少：",
    ]
    parts += [f"- `{name}`" for name in files]
    parts += [
        "",
        "## 回傳格式",
        "只回傳一個 JSON 物件，不要加 Markdown code fence，不要加前言、結語或說明文字。格式如下：",
        "```json",
        "{",
        '  "files": {',
        '    "note_general.md": "markdown 內容",',
        '    "action_items.json": {"decisions": [], "actions": [], "risks": [], "numbers": []},',
        '    "INDEX.md": "markdown 內容",',
        '    "agent_report.json": {"files": ["實際寫出的檔名"], "corrections": 0, "uncertain": [], "notes": "一句話說明"}',
        "  }",
        "}",
        "```",
        "規則：",
        "- `.md` 的值必須是字串。",
        "- `.json` 的值必須是 JSON 物件或陣列，不要再包成字串。",
        "- `files` 內只能出現允許輸出的檔名。",
        "- `agent_report.json.files` 要與你實際輸出的檔名一致。",
        "- 不得補寫逐字稿裡沒有的內容；不確定就誠實寫待確認或未討論。",
    ]
    return "\n".join(parts)


def persist_api_writer_result(job_dir: Path, raw_text: str, stems: list[str]) -> tuple[bool, str]:
    allowed = {f"{s}.md" for s in stems if s != "transcript_clean"} | {
        "action_items.json", "INDEX.md", "agent_report.json"
    }
    data = loads_loose(raw_text)
    if not data or not isinstance(data.get("files"), dict):
        return False, "response was not a JSON object with files"
    files = data["files"]
    unknown = [name for name in files if name not in allowed]
    if unknown:
        return False, f"unexpected file(s): {', '.join(sorted(unknown))}"

    for name in allowed:
        if name not in files:
            return False, f"missing file in response: {name}"

    for name, content in files.items():
        out = job_dir / name
        if name.endswith(".md"):
            if not isinstance(content, str):
                return False, f"{name} must be a string"
            out.write_text(content)
        else:
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except Exception as exc:  # noqa: BLE001
                    return False, f"{name} invalid JSON string: {exc}"
            out.write_text(json.dumps(content, ensure_ascii=False, indent=2))
    return True, ""


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #
def backend_is_api(backend: str) -> bool:
    return backend in API_BACKENDS


def resolve_bin(backend: str, override: str | None) -> str:
    if backend_is_api(backend):
        return ""
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

    raise SystemExit(f"[agent] unknown backend '{backend}' (expected: claude, codex, openai_compat)")


def _extract_openai_text(payload: dict, endpoint: str) -> str:
    if endpoint == "responses":
        if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
            return payload["output_text"].strip()
        parts: list[str] = []
        for item in payload.get("output") or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                text = content.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts).strip()

    choices = payload.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def _openai_compat_request(prompt: str, model: str | None, acfg: dict) -> tuple[str, str, dict, str]:
    api = (acfg.get("api") or {}) if isinstance(acfg, dict) else {}
    base_url = str(api.get("base_url") or "http://127.0.0.1:1234/v1").rstrip("/")
    endpoint = str(api.get("endpoint") or "chat_completions").strip() or "chat_completions"
    temp_val = api.get("temperature")
    temperature = float(0.1 if temp_val is None else temp_val)
    max_tokens = int(api.get("max_output_tokens") or 16384)
    model_name = model or str(api.get("model") or "").strip() or "openai/gpt-oss-120b"

    if endpoint == "responses":
        url = f"{base_url}/responses"
        body = {
            "model": model_name,
            "input": prompt,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
    else:
        endpoint = "chat_completions"
        url = f"{base_url}/chat/completions"
        body = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    return endpoint, url, body, str(api.get("api_key") or "lm-studio")


def _run_openai_compat(prompt: str, model: str | None, log_path: Path,
                       timeout: int, acfg: dict) -> tuple[int, str, str]:
    endpoint, url, body, api_key = _openai_compat_request(prompt, model, acfg)
    touch_activity(acfg, event="request")
    started = time.time()
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as log:
        log.write(f"\n===== {time.strftime('%F %T')} :: openai_compat =====\n")
        log.write(f"[agent] endpoint={endpoint} url={url} model={body.get('model')} timeout={timeout}s\n")
        log.flush()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                code = getattr(resp, "status", 200)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            log.write(f"[agent] HTTPError {exc.code}: {raw[:1200]}\n")
            return exc.code, f"{time.time() - started:.1f}", ""
        except Exception as exc:  # noqa: BLE001
            log.write(f"[agent] request failed: {type(exc).__name__}: {exc}\n")
            return 1, f"{time.time() - started:.1f}", ""

        try:
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            log.write(f"[agent] invalid JSON response: {exc}\n{raw[:1200]}\n")
            return 1, f"{time.time() - started:.1f}", ""

        text = _extract_openai_text(data, endpoint)
        if not text:
            log.write(f"[agent] empty text response\n{raw[:1200]}\n")
            return 1, f"{time.time() - started:.1f}", ""

        log.write(f"[agent] received {len(text)} chars\n")
        return code, f"{time.time() - started:.1f}", text


def invoke_backend(backend: str, binary: str, prompt: str, model: str | None,
                   log_path: Path, timeout: int, acfg: dict | None = None) -> tuple[int, str, str]:
    if backend_is_api(backend):
        return _run_openai_compat(prompt, model, log_path, timeout, acfg or {})
    rc, elapsed = run_agent(backend_cmd(backend, binary, prompt, model), log_path, timeout)
    return rc, elapsed, ""


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
    ap = argparse.ArgumentParser(description="Write meeting documents with a local coding-agent CLI or OpenAI-compatible API.")
    ap.add_argument("job_dir", help="archive/<job_id> (or 'latest')")
    ap.add_argument("--backend", choices=sorted(ALL_BACKENDS), default=None)
    ap.add_argument("--bin", default=None, help="full path to the CLI (overrides PATH lookup)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=None, help="seconds")
    ap.add_argument("--deliver", action="store_true", help="send the finished files via the configured notifier")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt and exit")
    args = ap.parse_args()

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
    acfg = resolve_agent_config(meta)
    backend = args.backend or acfg.get("backend") or "claude"
    timeout = args.timeout or int(acfg.get("timeout_sec") or 3600)
    model = args.model or acfg.get("model")
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

    if backend_is_api(backend):
        prompt = build_api_writer_prompt(
            job_dir,
            meta,
            result,
            plan,
            job_dir / "transcript.md",
            "逐字稿（未經清稿）",
            (
                "## 已確認事實\n\n"
                "（本次未經使用者確認流程，沒有任何已確認事實——"
                "講者姓名、專有名詞、金額日期一律依逐字稿推斷，"
                "並在文件開頭註明「講者對應為推測」。）\n"
            ),
            (f"## 使用者提供的背景／專有名詞\n\n{(meta.get('context') or '').strip()}\n"
             if (meta.get("context") or "").strip() else ""),
            "",
        )
    rc, elapsed, reply_text = invoke_backend(backend, binary, prompt, model, log_path, timeout, acfg)
    print(f"[agent] {backend} exited {rc} after {elapsed}s")
    if backend_is_api(backend):
        ok, why = persist_api_writer_result(job_dir, reply_text, plan["stems"])
        if not ok:
            print(f"[agent] API writer output invalid: {why}")
            rc = rc or 1

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
    if backend_is_api(backend) and report["ok"]:
        schedule_cleanup(acfg, job_id=job_dir.name, stage="write")
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
