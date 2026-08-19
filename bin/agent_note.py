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


def scale_contract_block(source_chars: int) -> str:
    """The one instruction every writer path needs: write at the meeting's scale.

    AGENT_TASK.md is six prohibitions and no floor, so the safest reading of it
    is a two-line-per-topic stub — which is what a small model actually returns.
    The segmented-writing block carried the counterweight ("內容量要與會議實際
    討論相稱"), but that block is tool-loop-only, so the CLI and single-envelope
    API paths were relying on the model being strong enough to not need telling.
    The mechanics of appending differ per path; this requirement does not, so it
    lives on its own and is injected everywhere.
    """
    return "\n".join([
        "## 內容量（硬規格）",
        "",
        f"來源約 {source_chars:,} 字。會議記錄的份量要與會議實際討論相稱——"
        "把一場會壓成幾行摘要視同失敗。",
        "",
        "- 逐字稿裡討論到的議題，有幾個就寫幾個；不要為了篇幅合併、跳過，或只留一個標題。",
        "- 每個議題都要寫到規格要求的深度（討論內容、誰提的、結論與依據），不要一句話帶過。",
        "- 寫到後段覺得篇幅變長時，該做的是繼續寫完，不是把剩下的議題濃縮成摘要。",
        "- 這條與「禁則」不衝突：禁則管的是不要編造，這條管的是不要偷懶。"
        "逐字稿沒講的仍然一律不准寫。",
    ])


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
                f"重新判斷是誰在講。**這件事不要寫進文件**——把「講者對應為推測」寫進"
                f"`agent_report.json` 的 `uncertain`，文件裡判不出來的句子不指派給特定人"
                f"（寫「會中有人提出」）。"
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
        # Segmented writing is a tool-loop capability (review.py fills this in).
        # The CLI agents reached from here have no append mode, so it stays blank
        # rather than leaking an unsubstituted placeholder into the prompt.
        "WRITE_STRATEGY": "",
        # How much to write does not depend on how the file gets written, so
        # unlike WRITE_STRATEGY this one is filled on every path.
        "SCALE_CONTRACT": scale_contract_block(chars),
        # This entry point skips the review stage entirely, so there is nothing
        # confirmed and the source is always the raw transcript. review.py
        # fills both properly; here they are stated rather than left as
        # unsubstituted placeholders in the prompt.
        "SOURCE_DESC": "逐字稿（未經清稿）",
        "CONFIRMED": (
            "## 已確認事實\n\n"
            "（本次未經使用者確認流程，沒有任何已確認事實——"
            "講者姓名、專有名詞、金額日期一律依逐字稿推斷。"
            "**不要在文件裡註明這件事**：把「講者對應為推測」寫進 `agent_report.json` 的"
            "`uncertain`，判不出來的句子不指派給特定人。）\n"
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
        scale_contract_block(len(source_text)),
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
        # This path has no append mode: a reply that runs out of room mid-object
        # parses as nothing at all and the whole job fails. Say where to give way
        # first, so the length requirement above never costs the entire output.
        "- 整個 JSON 必須完整閉合。真的寫不下時，先縮減 `action_items.json` 的附屬欄位，"
        "最後才動議題討論——但絕不可回傳斷在一半的 JSON。",
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


# Transport-level failure detail for the most recent API backend call.
# _run_openai_compat() can only return an empty string on failure, which callers
# used to mistake for "the model replied with the wrong shape". Callers should
# consult last_api_error() before running any schema validation.
_LAST_API_ERROR: dict | None = None


def last_api_error() -> dict | None:
    """Detail of the last API transport failure, or None if the call succeeded."""
    return _LAST_API_ERROR


def _set_api_error(kind: str, detail: str, *, code: int = 0, url: str = "",
                   model: str = "") -> None:
    global _LAST_API_ERROR
    _LAST_API_ERROR = {
        "kind": kind,          # http_error | unreachable | not_json | empty
        "detail": detail.strip(),
        "code": code,
        "url": url,
        "model": model,
    }


def describe_api_error(err: dict | None) -> str:
    """Human-readable one-liner for an entry produced by _set_api_error()."""
    if not err:
        return ""
    kind = err.get("kind")
    model = err.get("model") or "?"
    detail = (err.get("detail") or "").strip()
    if len(detail) > 600:
        detail = detail[:600] + "…"
    if kind == "http_error":
        return (f"推論服務回應 HTTP {err.get('code')}（model={model}）："
                f"{detail or '無錯誤內容'}")
    if kind == "unreachable":
        return f"無法連線到推論服務 {err.get('url') or '?'}：{detail}"
    if kind == "not_json":
        return f"推論服務回傳的內容不是合法 JSON（model={model}）：{detail}"
    if kind == "empty":
        return f"推論服務回傳空白內容（model={model}）：{detail}"
    return detail or "未知的 API 失敗"


def _extract_api_error_message(raw: str) -> str:
    """Pull the human message out of an OpenAI-style error body when possible."""
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return raw.strip()
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"]).strip()
        if isinstance(err, str) and err.strip():
            return err.strip()
        if data.get("message"):
            return str(data["message"]).strip()
    return raw.strip()


def _run_openai_compat(prompt: str, model: str | None, log_path: Path,
                       timeout: int, acfg: dict) -> tuple[int, str, str]:
    global _LAST_API_ERROR
    _LAST_API_ERROR = None
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
            _set_api_error("http_error", _extract_api_error_message(raw),
                           code=exc.code, url=url, model=str(body.get("model") or ""))
            return exc.code, f"{time.time() - started:.1f}", ""
        except Exception as exc:  # noqa: BLE001
            log.write(f"[agent] request failed: {type(exc).__name__}: {exc}\n")
            _set_api_error("unreachable", f"{type(exc).__name__}: {exc}",
                           url=url, model=str(body.get("model") or ""))
            return 1, f"{time.time() - started:.1f}", ""

        try:
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            log.write(f"[agent] invalid JSON response: {exc}\n{raw[:1200]}\n")
            _set_api_error("not_json", f"{exc}｜開頭內容：{raw[:300]}",
                           code=code, url=url, model=str(body.get("model") or ""))
            return 1, f"{time.time() - started:.1f}", ""

        text = _extract_openai_text(data, endpoint)
        if not text:
            log.write(f"[agent] empty text response\n{raw[:1200]}\n")
            _set_api_error("empty", _extract_api_error_message(raw) or raw[:300],
                           code=code, url=url, model=str(body.get("model") or ""))
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
# post-write cleanup
# --------------------------------------------------------------------------- #

# Stems whose prose is allowed to carry transcript timestamps. transcript_clean
# IS a transcript; interview notes quote verbatim and the spec asks for marks so
# a researcher can re-listen. Everything else is a document you hand to people.
TIMESTAMP_OK_STEMS = {"transcript_clean", "note_interview"}

# Stems allowed to describe how the pipeline works. Only the transcript: its
# spec genuinely asks for a "講者對應為推測" header, because that file is a
# working document, not a deliverable. Interview notes are handed to people
# like every other note, so they keep their timestamps but lose disclaimers.
DISCLAIMER_OK_STEMS = {"transcript_clean"}

# `[00:12:34]` / `[12:34]`, optionally wrapped in backticks or parens, and runs
# of them joined by a dash or comma (`[00:02:53]–[00:03:04]`). Anchored on the
# bracket so ordinary markdown links and footnotes are untouched.
_TS = r"[（(]?`?\[\d{1,2}:\d{2}(?::\d{2})?\]`?[)）]?"
_TS_RUN = re.compile(rf"{_TS}(?:\s*[–—~至,、，/-]\s*{_TS})*")
_FENCE = re.compile(r"^\s*(```|~~~)")


def strip_inline_timestamps(text: str) -> tuple[str, int]:
    """Remove transcript timestamps from note prose.

    The spec forbids them, but a prompt rule is a request; this is the
    guarantee. Fenced code blocks are skipped — nothing in a note legitimately
    needs `[00:12:34]` outside one, and inside one it may be sample data.
    """
    out: list[str] = []
    removed = 0
    in_fence = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or "[" not in line:
            out.append(line)
            continue
        cleaned, n = _TS_RUN.subn("", line)
        if n:
            removed += n
            # Tidy the holes the removal leaves: doubled spaces, a space that
            # now sits before punctuation, and brackets emptied of content.
            cleaned = re.sub(r"[（(]\s*[)）]", "", cleaned)
            cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
            cleaned = re.sub(r"[ \t]+([。，、；：！？）」』】])", r"\1", cleaned)
            cleaned = cleaned.rstrip()
        out.append(cleaned)
    return "\n".join(out), removed


# Vocabulary that only shows up when the writer is explaining our pipeline to
# the reader. Split in two because the risk is not symmetric.
#
# _PROCESS_ALWAYS: phrases nobody says in a meeting. They exist to disclaim the
# document itself, so they are internal wherever they appear — including inside
# 「」, since the prompts used to literally ask for 註明「講者對應為推測」.
#
# _PROCESS_UNQUOTED: real technical vocabulary. A consultant demoing this very
# tool could say "聲紋分群" out loud, and a verbatim quote of that is content,
# not a disclaimer. These only count when they appear outside 「」/『』/"" —
# i.e. when the note is speaking in its own voice about how it was made.
_PROCESS_ALWAYS = (
    "講者對應為推測", "講者對應係推測", "講者對應仍為推測",
    "逐句歸屬", "未經使用者確認",
)
_PROCESS_UNQUOTED = (
    "聲紋分群", "聲紋群", "講者標籤", "speaker 標籤", "speaker標籤",
    "依內容推斷", "依內容推測",
)
_QUOTED = re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”|\"[^\"]*\"")
_BLOCKQUOTE = re.compile(r"^\s{0,3}>")


def _is_process_disclaimer(block: str) -> bool:
    if any(term in block for term in _PROCESS_ALWAYS):
        return True
    bare = _QUOTED.sub("", block)
    return any(term in bare for term in _PROCESS_UNQUOTED)


def strip_process_disclaimers(text: str) -> tuple[str, int]:
    """Remove blockquotes that explain the pipeline instead of the meeting.

    A note is handed to attendees and clients. How the diarisation clustered,
    how speaker attribution was inferred, what the system could not resolve —
    that belongs in agent_report.json's `uncertain`, which the user sees in the
    completion notice, not in the document. The spec says so; this makes it so.

    Scope is deliberately narrow to avoid eating content: only blockquote
    blocks (`>`-prefixed runs), never body prose, never fenced code. A quote of
    something an attendee said survives — see _PROCESS_UNQUOTED. Returns the
    text and how many blocks went.
    """
    lines = text.split("\n")
    out: list[str] = []
    removed = 0
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence or not _BLOCKQUOTE.match(line):
            out.append(line)
            i += 1
            continue
        j = i
        while j < len(lines) and _BLOCKQUOTE.match(lines[j]):
            j += 1
        block = "\n".join(lines[i:j])
        if _is_process_disclaimer(block):
            removed += 1
        else:
            out.extend(lines[i:j])
        i = j

    if not removed:
        return text, 0

    # Tidy the hole: a removed block leaves behind the blank lines that framed
    # it — as a doubled gap mid-document, a blank first line when the
    # disclaimer sat under the title, or a trailing gap when it sat last.
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")
    if text.endswith("\n"):
        cleaned += "\n"
    return cleaned, removed


# Writing instructions that live in the spec's skeleton headings and get copied
# into the output verbatim (`## 一、本次會議重點（3 句以內）`). Deliberately
# shape-based, not a keyword list: only counts/limits phrased as a constraint,
# so a heading that legitimately carries a parenthetical — `### 3. 報價調整
# （Jim 提出）`, `## 二、議題討論（續）` — is untouched.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_UNIT = r"(?:句|字|條|點|項|個|則)"
_HEADING_INSTRUCTION = re.compile(
    r"[（(]\s*(?:"
    rf"\d+\s*{_UNIT}?\s*以[內内下]"
    rf"|(?:最多|至多|不超過|不多於|至少|最少|不少於)\s*\d+\s*{_UNIT}?"
    rf"|\d+\s*[-–~至]\s*\d+\s*{_UNIT}"
    r")\s*[)）]"
)


def strip_heading_instructions(text: str) -> tuple[str, int]:
    """Drop spec-writing instructions the writer copied into headings.

    The skeleton in NOTE_SPECS.md doubles as both the required structure and
    the place to note a length limit, so `（3 句以內）` rides along into the
    delivered document. The spec now says not to; this makes it not happen.
    Headings only — the same parenthetical inside body prose ("控制在 3 句以內")
    would be the writer quoting a real instruction someone gave in the meeting.
    """
    out: list[str] = []
    removed = 0
    in_fence = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or not _HEADING.match(line):
            out.append(line)
            continue
        cleaned, n = _HEADING_INSTRUCTION.subn("", line)
        if n:
            removed += n
            cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).rstrip()
        out.append(cleaned)
    return "\n".join(out), removed


def scrub_note(job_dir: Path, stem: str) -> int:
    """Rewrite <stem>.md in place without timestamps, disclaimers or heading
    instructions.

    Returns the total number of things removed. The two exemptions are
    separate concepts: transcript_clean is a transcript and keeps both;
    note_interview keeps its timestamps but is still a deliverable, so its
    disclaimers go.
    """
    md = job_dir / f"{stem}.md"
    try:
        original = md.read_text()
    except OSError:
        return 0
    text, removed = original, 0
    if stem not in TIMESTAMP_OK_STEMS:
        text, n = strip_inline_timestamps(text)
        removed += n
    if stem not in DISCLAIMER_OK_STEMS:
        text, n = strip_process_disclaimers(text)
        removed += n
    # No exemption: a heading is a heading in every document we hand over, and
    # the transcript's headings carry no counts to strip.
    text, n = strip_heading_instructions(text)
    removed += n
    if removed:
        md.write_text(text)
    return removed


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
                "講者姓名、專有名詞、金額日期一律依逐字稿推斷。"
                "**不要在文件裡註明這件事**：把「講者對應為推測」寫進 `agent_report.json` 的"
                "`uncertain`，判不出來的句子不指派給特定人。）\n"
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
        # Before conversion, so the PDF and the Word file inherit the clean md.
        dropped = scrub_note(job_dir, stem)
        if dropped:
            print(f"[agent] SCRUB    {stem}.md: removed {dropped} timestamp(s)/disclaimer(s)")
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
