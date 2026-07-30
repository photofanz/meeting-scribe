#!/usr/bin/env python
"""
Outbound notifications — the one place that knows how this machine reaches you.

The pipeline runs unattended for tens of minutes and then, twice, needs a
human: once when the reviewer has questions, once when the documents are on
disk. Before this module each stage grew its own `subprocess.run([hermes, ...])`
call, which meant every install had to own the same external notifier and a
missing binary silently swallowed the "your notes are ready" message.

So: one `send()`, four interchangeable back ends, zero third-party imports.

    none      nothing leaves the machine (still logged, see below)
    telegram  Bot API over urllib — works on a bare Python install
    command   hand off to any CLI notifier the user already trusts
    webhook   POST signed JSON to something the user runs themselves

Two rules hold for every mode:

  1. `send()` never raises. A notification is a side effect of the job, not
     part of it; a dead network must not fail a meeting that transcribed fine.
  2. Anything that could not be delivered is appended to
     `logs/undelivered.log`. "Silently dropped" is the only unacceptable
     outcome — a parked message can still be read tomorrow.

    bin/notify.py --event done --title "測試" --body "..." [--file P ...]
    bin/notify.py --test
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import jobstate  # noqa: E402
from config import ROOT  # noqa: E402

# jobstate states + one extra: "transcribed" is not a state (ASR finishing
# hands straight over to `scanning`) but it is the moment a user most wants a
# ping, so it is notifiable without being persistable.
EVENT_EMOJI: dict[str, str] = {
    "transcribing":     "🎧",
    "transcribed":      "🎙️",
    "scanning":         "🔍",
    "awaiting_answers": "❓",
    "writing":          "✍️",
    "done":             "✅",
    "error":            "⚠️",
}
EVENT_LABEL: dict[str, str] = {
    "transcribing":     "轉寫中",
    "transcribed":      "轉寫完成",
    "scanning":         "掃描中",
    "awaiting_answers": "待回答",
    "writing":          "撰寫中",
    "done":             "完成",
    "error":            "失敗",
}

MODES = ("none", "telegram", "command", "webhook")

TELEGRAM_TEXT_LIMIT = 4096
TRUNCATED_SUFFIX = "\n…（訊息過長已截斷）"
# Bot API refuses larger uploads outright; checking here turns a confusing
# HTTP 413 into a line in the log that names the file.
TELEGRAM_FILE_LIMIT = 45 * 1024 * 1024

MSG_TIMEOUT = 30
UPLOAD_TIMEOUT = 180
COMMAND_TIMEOUT = 300
WEBHOOK_TIMEOUT = 30
WEBHOOK_RETRIES = 2  # i.e. 3 attempts total
# A 429 can legally ask us to wait an hour. We honour it once but refuse to
# block a pipeline for longer than this; past the cap the message is parked.
MAX_RETRY_AFTER = 60

UNDELIVERED = "logs/undelivered.log"


def _cfg() -> dict:
    """Fresh notify config on every call.

    `config.CONFIG` is snapshotted at import time, which is wrong for the two
    callers that matter: the setup wizard (saves, then immediately asks us to
    send a test) and the long-lived upload server (config edited while it
    runs). Re-reading one small JSON file per notification is free compared to
    the HTTP request that follows.
    """
    try:
        return config.load().get("notify") or {}
    except Exception:  # noqa: BLE001 - a broken config must not break sending
        return dict(config.DEFAULTS["notify"])


def _result(ok: bool, mode: str, skipped: bool = False,
            reason: str = "", detail: str = "") -> dict:
    return {"ok": ok, "mode": mode, "skipped": skipped, "reason": reason, "detail": detail}


# ------------------------------------------------------------------ parking --
def park(message: str, event: str = "", mode: str = "", reason: str = "") -> str | None:
    """Append an undeliverable message to logs/undelivered.log.

    Best effort by design: if even this fails we print and move on, because the
    caller is a pipeline stage that must still return its own success.
    """
    path = ROOT / UNDELIVERED
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"=== {stamp} | event={event or '-'} | mode={mode or '-'} | {reason or '-'} ==="
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{header}\n{message}\n\n")
        return str(path)
    except Exception as exc:  # noqa: BLE001
        print(f"[notify] cannot write {path}: {exc}", file=sys.stderr)
        return None


# ----------------------------------------------------------------- compose ---
def compose(event: str, title: str, body: str = "", files: list[str] | None = None,
            job_id: str | None = None, url: str | None = None) -> str:
    """The human-readable message, identical across every mode.

    Markdown is written in the portable `**bold**` form; the Telegram back end
    downgrades it to the Bot API's legacy single-asterisk syntax. Doing it the
    other way round would leave stray asterisks in webhook payloads and in
    whatever a `command` notifier does with plain text.
    """
    emoji = EVENT_EMOJI.get(event, "🔔")
    label = EVENT_LABEL.get(event, event)
    lines = [f"{emoji} **{title or label}**"]
    if event in ("error", "awaiting_answers"):
        # For the two events that need an action, spell the state out; for the
        # rest the emoji plus the title already says it.
        lines.append(f"狀態：{label}")
    if body:
        lines += ["", body.strip()]
    names = [Path(f).name for f in (files or [])]
    if names:
        lines += ["", "📎 附件：" + "、".join(names)]
    tail = []
    if job_id:
        tail.append(f"Job: `{job_id}`")
    if url:
        tail.append(f"🔗 {url}")
    if tail:
        lines += [""] + tail
    return "\n".join(lines)


# ---------------------------------------------------------------- telegram ---
def _tg_markdown(text: str) -> str:
    """`**bold**` -> `*bold*` for Telegram's legacy Markdown parser.

    Rejected: parse_mode=MarkdownV2, which would force escaping of 18 ASCII
    characters that occur constantly in meeting titles and file paths. Legacy
    Markdown plus the no-parse-mode retry below is far less brittle.
    """
    return text.replace("**", "*")


def _tg_call(token: str, method: str, payload: dict, timeout: int = MSG_TIMEOUT) -> dict:
    """POST JSON to the Bot API. Returns the decoded body; raises on transport."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    return _read_json(req, timeout)


def _read_json(req: urllib.request.Request, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        try:
            out = json.loads(raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            out = {"ok": False, "description": raw.decode("utf-8", "replace")[:300]}
        out.setdefault("ok", False)
        out["_http_status"] = exc.code
        return out
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "description": f"bad JSON from API: {exc}"}


def _retry_after(resp: dict) -> int | None:
    if resp.get("_http_status") != 429:
        return None
    params = resp.get("parameters") or {}
    val = params.get("retry_after", resp.get("retry_after"))
    try:
        return int(val)
    except (TypeError, ValueError):
        return 1


def build_multipart(fields: dict[str, str], file_field: str, filename: str,
                    content: bytes, boundary: str) -> bytes:
    """Hand-rolled multipart/form-data body.

    Written out rather than pulled from `email.mime` because that module
    rewraps and base64-encodes binary payloads, which the Bot API rejects.
    `boundary` is a parameter purely so this is unit-testable byte-for-byte.
    """
    out = bytearray()
    dash = f"--{boundary}".encode()
    for name, value in fields.items():
        out += dash + b"\r\n"
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        out += str(value).encode("utf-8") + b"\r\n"
    safe = filename.replace('"', "%22").replace("\r", " ").replace("\n", " ")
    out += dash + b"\r\n"
    out += (f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{safe}"\r\n').encode("utf-8")
    out += b"Content-Type: application/octet-stream\r\n\r\n"
    out += content + b"\r\n"
    out += dash + b"--\r\n"
    return bytes(out)


def _tg_send_document(token: str, chat_id: str, path: Path) -> tuple[bool, str]:
    boundary = f"----MeetingScribe{os.urandom(12).hex()}"
    body = build_multipart({"chat_id": chat_id}, "document", path.name,
                           path.read_bytes(), boundary)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    resp = _read_json(req, UPLOAD_TIMEOUT)
    wait = _retry_after(resp)
    if wait is not None and wait <= MAX_RETRY_AFTER:
        time.sleep(wait)
        resp = _read_json(req, UPLOAD_TIMEOUT)
    if resp.get("ok"):
        return True, ""
    return False, str(resp.get("description") or resp)[:300]


def _send_telegram(cfg: dict, message: str, files: list[str]) -> tuple[bool, str]:
    tg = cfg.get("telegram") or {}
    token, chat_id = (tg.get("bot_token") or "").strip(), str(tg.get("chat_id") or "").strip()
    if not token or not chat_id:
        return False, "notify.telegram.bot_token / chat_id 尚未設定"

    text = _tg_markdown(message)
    if len(text) > TELEGRAM_TEXT_LIMIT:
        text = text[: TELEGRAM_TEXT_LIMIT - len(TRUNCATED_SUFFIX)] + TRUNCATED_SUFFIX

    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
               "disable_web_page_preview": True}
    try:
        resp = _tg_call(token, "sendMessage", payload)
        wait = _retry_after(resp)
        if wait is not None and wait <= MAX_RETRY_AFTER:
            time.sleep(wait)
            resp = _tg_call(token, "sendMessage", payload)
        if not resp.get("ok") and "parse" in str(resp.get("description", "")).lower():
            # An unbalanced * or _ in a meeting title is not worth losing the
            # message over: resend as plain text.
            resp = _tg_call(token, "sendMessage",
                            {**payload, "text": message, "parse_mode": None})
        if not resp.get("ok"):
            return False, str(resp.get("description") or resp)[:300]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:300]

    problems = []
    for f in files:
        p = Path(f)
        try:
            if not p.is_file():
                problems.append(f"{p.name}: 檔案不存在")
                continue
            if p.stat().st_size > TELEGRAM_FILE_LIMIT:
                problems.append(f"{p.name}: 超過 45MB 未上傳")
                continue
            # Images go through sendDocument too: sendPhoto re-encodes and caps
            # at 10MB, and a lossless PDF-quality screenshot matters more than
            # an inline preview.
            ok, why = _tg_send_document(token, chat_id, p)
            if not ok:
                problems.append(f"{p.name}: {why}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{p.name}: {type(exc).__name__}: {exc}")
    if problems:
        # The text went out; only attachments failed. Report it as a failure so
        # the message (which names the files) is still parked for retry by hand.
        return False, "訊息已送出，附件失敗：" + "；".join(problems)[:300]
    return True, f"sent + {len(files)} file(s)"


# ----------------------------------------------------------------- command ---
def build_argv(template: str, message: str, files: list[str]) -> list[str]:
    """Split the template FIRST, then substitute into the argv elements.

    This ordering is the whole point. Substituting into the string and then
    splitting would let a meeting title containing a quote, a space or
    `$(...)` become extra arguments; splitting first means the message can
    only ever land inside one already-decided argv slot. `shell=True` is never
    used anywhere in this module for the same reason.
    """
    argv = shlex.split(template)
    if not argv:
        return []
    files_block = "\n".join(f"MEDIA:{os.path.abspath(f)}" for f in files)
    subbed, seen = [], False
    for arg in argv:
        if "{message}" in arg or "{files}" in arg:
            seen = True
            arg = arg.replace("{message}", message).replace("{files}", files_block)
        subbed.append(arg)
    if not seen:
        subbed.append(message)
    subbed[0] = os.path.expanduser(subbed[0])
    return subbed


def _send_command(cfg: dict, message: str, files: list[str]) -> tuple[bool, str]:
    template = (cfg.get("command") or "").strip()
    if not template:
        return False, "notify.command 尚未設定"
    try:
        argv = build_argv(template, message, files)
    except ValueError as exc:  # unbalanced quotes in the template
        return False, f"notify.command 無法解析：{exc}"
    if not argv:
        return False, "notify.command 為空"
    if not os.path.isabs(argv[0]) and shutil.which(argv[0]) is None:
        return False, f"找不到指令：{argv[0]}"
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"指令逾時（{COMMAND_TIMEOUT}s）：{argv[0]}"
    except (OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300]
        return False, f"exit {proc.returncode}: {err}"
    return True, (proc.stdout or "").strip()[:200]


# ----------------------------------------------------------------- webhook ---
def sign(secret: str, body: bytes) -> str:
    """`sha256=<hex>` over the exact bytes we put on the wire."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _send_webhook(cfg: dict, event: str, title: str, body_text: str,
                  files: list[str], job_id: str | None, url: str | None) -> tuple[bool, str]:
    wh = cfg.get("webhook") or {}
    endpoint = (wh.get("url") or "").strip()
    secret = wh.get("secret") or ""
    if not endpoint:
        return False, "notify.webhook.url 尚未設定"

    payload = {
        "event": event,
        "job_id": job_id,
        "title": title,
        "body": body_text,
        "files": [os.path.abspath(f) for f in files],
        "url": url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # Serialise once: the signature must cover the bytes actually sent, not a
    # re-encoding of an equivalent dict.
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-MeetingScribe-Signature"] = sign(secret, raw)

    last = ""
    for attempt in range(WEBHOOK_RETRIES + 1):
        req = urllib.request.Request(endpoint, data=raw, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT) as resp:
                return True, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}: {(exc.read() or b'').decode('utf-8', 'replace')[:200]}"
            if exc.code < 500:
                return False, last  # 4xx is our bug, not theirs: retrying cannot help
        except Exception as exc:  # noqa: BLE001 - URLError, socket.timeout, ssl errors
            last = f"{type(exc).__name__}: {exc}"[:300]
        if attempt < WEBHOOK_RETRIES:
            time.sleep(1 + 2 * attempt)
    return False, last


def _job_dir_for(files: list[str], job_id: str | None) -> Path | None:
    if job_id:
        d = ROOT / "archive" / job_id
        if d.is_dir():
            return d
    for f in files:
        p = Path(f)
        parent = p.parent
        if (parent / "meta.json").is_file() or (parent / "status.json").is_file():
            return parent
    return None


def _read_path_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except Exception:  # noqa: BLE001
        return {}


def _prepare_exports(files: list[str], job_id: str | None) -> list[str]:
    if not files:
        return []
    job_dir = _job_dir_for(files, job_id)
    if not job_dir:
        return [str(Path(f).expanduser()) for f in files]

    meta = _read_path_json(job_dir / "meta.json")
    status = _read_path_json(job_dir / "status.json")
    client = status.get("result", {}).get("client") or meta.get("client") or ""
    title = status.get("result", {}).get("title") or meta.get("title") or job_dir.name
    meeting_date = status.get("result", {}).get("date") or meta.get("date") or ""
    export_dir = job_dir / ".export"
    export_dir.mkdir(parents=True, exist_ok=True)

    out: list[str] = []
    seen: set[str] = set()
    for raw in files:
        src = Path(raw).expanduser()
        if not src.is_file():
            out.append(str(src))
            continue
        base = jobstate.display_name(src.name, client=client, title=title, meeting_date=meeting_date)
        alias = export_dir / base
        stem, suffix = alias.stem, alias.suffix
        n = 2
        while alias.name in seen:
            alias = export_dir / f"{stem}_{n}{suffix}"
            n += 1
        seen.add(alias.name)
        try:
            if alias.exists() or alias.is_symlink():
                alias.unlink()
            os.link(src, alias)
        except OSError:
            shutil.copy2(src, alias)
        out.append(str(alias))
    return out


# -------------------------------------------------------------------- send ---
def _dispatch(mode: str, cfg: dict, event: str, title: str, body: str,
              files: list[str], job_id: str | None, url: str | None,
              message: str) -> tuple[bool, str]:
    if mode == "telegram":
        return _send_telegram(cfg, message, files)
    if mode == "command":
        return _send_command(cfg, message, files)
    if mode == "webhook":
        return _send_webhook(cfg, event, title, body, files, job_id, url)
    return False, f"未知的 notify.mode：{mode}"


def _send(event: str, title: str, body: str = "", files: list[str] | None = None,
          job_id: str | None = None, url: str | None = None,
          mode: str | None = None) -> dict:
    cfg = _cfg()
    mode = mode or cfg.get("mode") or "none"
    files = _prepare_exports([str(f) for f in (files or [])], job_id)
    events = cfg.get("events") or []

    # `error` ignores the subscription list on purpose. A user who muted
    # everything still needs to know the job died, otherwise the failure mode
    # is "I waited two hours for nothing".
    if event != "error" and event not in events:
        return _result(True, mode, skipped=True, reason="event not subscribed")

    message = compose(event, title, body, files, job_id, url)

    if mode == "none":
        # Not delivered, but not lost either: this is what makes "none" a safe
        # default rather than a black hole.
        park(message, event, mode, "mode=none")
        return _result(True, mode, skipped=True, reason="mode=none",
                       detail=f"已寫入 {UNDELIVERED}")

    ok, detail = _dispatch(mode, cfg, event, title, body, files, job_id, url, message)
    if not ok:
        park(message, event, mode, detail or "delivery failed")
    return _result(ok, mode, skipped=False, reason="" if ok else "delivery failed",
                   detail=detail)


def send(event: str, title: str, body: str = "", files: list[str] | None = None,
         job_id: str | None = None, url: str | None = None) -> dict:
    """Notify the user about `event`. Never raises; see module docstring.

    Returns {"ok", "mode", "skipped", "reason", "detail"}. `ok=True` with
    `skipped=True` means "nothing to do", which is a success from the caller's
    point of view — the pipeline should not treat a muted event as an error.
    """
    try:
        return _send(event, title, body, files, job_id, url)
    except Exception as exc:  # noqa: BLE001 - the whole contract of this module
        detail = f"{type(exc).__name__}: {exc}"
        try:
            park(f"{title}\n{body}", event, "?", f"internal error: {detail}")
        except Exception:  # noqa: BLE001
            pass
        return _result(False, "?", reason="internal error", detail=detail[:300])


def test(mode: str | None = None) -> dict:
    """Send a fixed message so the setup wizard can prove the channel works."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        return _send(
            "done",
            "Meeting Scribe 通知測試",
            f"如果你看到這則訊息，通知設定已完成。\n（測試時間：{stamp}）",
            files=None, job_id="test-0000", url=None, mode=mode,
        )
    except Exception as exc:  # noqa: BLE001
        return _result(False, mode or "?", reason="internal error",
                       detail=f"{type(exc).__name__}: {exc}"[:300])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Send a Meeting Scribe notification.")
    ap.add_argument("--event", default="done", help="|".join(EVENT_EMOJI))
    ap.add_argument("--title", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--file", dest="files", action="append", default=[])
    ap.add_argument("--job-id", dest="job_id", default=None)
    ap.add_argument("--url", default=None)
    ap.add_argument("--mode", choices=MODES, default=None, help="override notify.mode")
    ap.add_argument("--test", action="store_true", help="send a fixed test message")
    a = ap.parse_args(argv)

    if a.test:
        res = test(a.mode)
    else:
        if not a.title:
            ap.error("--title is required (or use --test)")
        res = _send(a.event, a.title, a.body, a.files, a.job_id, a.url, a.mode)
    print(json.dumps(res, ensure_ascii=False))
    # skipped is a success: a muted event must not fail a shell pipeline.
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
