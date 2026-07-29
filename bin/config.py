#!/usr/bin/env python
"""
Deployment-local configuration.

Everything that differs between machines (install path, port, service label,
document branding) lives in `config.json` at the repo root. That file is
gitignored; `config.example.json` is the tracked template.

`ROOT` is derived from this file's location rather than hardcoded to
~/Meetings, so the repo can be cloned anywhere. `MEETING_ROOT` overrides it
for tests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("MEETING_ROOT") or Path(__file__).resolve().parent.parent)

DEFAULTS: dict = {
    "port": 8765,
    "service_label": "com.meetingscribe.uploader",
    "language": "zh",
    "notify": {
        # How this machine tells you a job moved on. Every mode is optional —
        # with "none" the web UI is the only surface, which is the default for
        # a fresh clone so the project depends on no other software.
        #
        #   none      no push at all; everything is read from /jobs
        #   telegram  talk to the Telegram Bot API directly (zero dependencies)
        #   command   shell out to an external notifier, e.g. `hermes send`
        #   webhook   POST JSON + HMAC signature to your own endpoint
        "mode": "none",
        # Which transitions are worth a push. Deliberately short: a chatty
        # channel gets muted, and a muted channel is the same as none.
        "events": ["awaiting_answers", "done", "error"],
        "telegram": {"bot_token": "", "chat_id": ""},
        # {message} and {files} are substituted; {files} expands to one
        # `MEDIA:/abs/path` line per deliverable.
        "command": "",
        "webhook": {"url": "", "secret": ""},
        # --- legacy (pre-mode) --------------------------------------------
        # A config.json still carrying notify.enabled/bin/target keeps working:
        # load() maps it onto mode="command".
        "enabled": None,
        "bin": "",
        "target": "telegram",
    },
    "branding": {
        # Masthead text printed at the top of every generated PDF.
        "brand_name": "MEETING NOTES",
        # Footer line printed on client-facing PDF/Word documents.
        "client_footer": "本文件內容以雙方會議討論為準。",
        # Shown as a placeholder in the upload form's participants field.
        "participants_hint": "例：王總、張經理、我",
    },
    "asr": {
        "whisper_model": "mlx-community/whisper-large-v3-turbo",
        "diarization_threads": 4,
    },
    "agent": {
        # What happens once the transcript exists.
        #
        #   manual  stop; a human (or a chat agent) starts the write by hand
        #   auto    scan and write back-to-back, nobody in the loop
        #   review  scan, surface the questions in the web UI, wait for
        #           answers, then write — the default, because those answers
        #           are what stop the notes inventing names and numbers
        "mode": "review",
        "backend": "claude",  # claude (Claude Code CLI) | codex (OpenAI Codex CLI)
        "bin": None,  # None => resolve `backend` on PATH
        "model": None,  # None => whatever the CLI defaults to
        "timeout_sec": 3600,
        # Long transcripts are scanned in slices instead of one oversized
        # prompt. ~20 minutes of Chinese speech is ~14k characters.
        "chunk_chars": 14000,
        "chunk_overlap_turns": 3,
        # Concurrent CLI processes. Each is a full model session, so this is
        # about memory and rate limits, not CPU cores.
        "max_parallel": 3,
        # Above this size the note is written map-reduce instead of one pass.
        # Below it, a single pass produces a more coherent document.
        "mapreduce_threshold_chars": 60000,
        "max_questions": 8,
    },
    "retention": {
        # Housekeeping hints surfaced in the UI. Nothing is ever deleted
        # without an explicit click — these only decide what gets flagged.
        "suggest_archive_days": 30,
        "suggest_clean_days": 60,
        "suggest_delete_days": 180,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _migrate_notify(cfg: dict) -> dict:
    """Map a pre-`mode` notify block onto the four-mode scheme.

    Older configs said `{"enabled": true, "bin": "~/.local/bin/hermes",
    "target": "telegram"}`. That is exactly `command` mode, so translate it
    instead of silently dropping the user's only notification channel on
    upgrade. An explicit `mode` in config.json always wins.
    """
    n = cfg.get("notify") or {}
    if n.get("mode") and n["mode"] != DEFAULTS["notify"]["mode"]:
        return cfg
    if n.get("enabled") is None:
        return cfg
    if n["enabled"] and n.get("bin"):
        n["mode"] = "command"
        n.setdefault("command", "")
        if not n["command"]:
            n["command"] = f'{n["bin"]} send --to {n.get("target", "telegram")} {{message}}'
    else:
        n["mode"] = "none"
    return cfg


def load() -> dict:
    """Config with defaults filled in. Never raises on a malformed file."""
    path = ROOT / "config.json"
    user: dict = {}
    if path.exists():
        try:
            user = json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001 - a broken config must not brick the pipeline
            print(f"[config] ignoring malformed config.json: {exc}")
    return _migrate_notify(_merge(DEFAULTS, user))


CONFIG = load()


def save(cfg: dict) -> None:
    """Persist config.json. Used by bin/notify_setup.py."""
    path = ROOT / "config.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def hermes_bin() -> Path:
    """Deprecated: only still used by the legacy `command`-mode helper."""
    return Path(os.path.expanduser(CONFIG["notify"].get("bin") or "/nonexistent"))


if __name__ == "__main__":  # `python bin/config.py port` -> for shell scripts
    import sys

    cur = CONFIG
    for key in sys.argv[1:]:
        cur = cur[key]
    print(cur if not isinstance(cur, (dict, list)) else json.dumps(cur))
