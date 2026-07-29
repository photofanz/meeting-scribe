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
        # `hermes send` delivers the "transcription finished" ping.
        # Set "enabled": false on a machine with no Hermes install — the
        # pipeline still runs, the message just goes to logs/undelivered.log.
        "enabled": True,
        "bin": "~/.local/bin/hermes",
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
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load() -> dict:
    """Config with defaults filled in. Never raises on a malformed file."""
    path = ROOT / "config.json"
    user: dict = {}
    if path.exists():
        try:
            user = json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001 - a broken config must not brick the pipeline
            print(f"[config] ignoring malformed config.json: {exc}")
    return _merge(DEFAULTS, user)


CONFIG = load()


def hermes_bin() -> Path:
    return Path(os.path.expanduser(CONFIG["notify"]["bin"]))


if __name__ == "__main__":  # `python bin/config.py port` -> for shell scripts
    import sys

    cur = CONFIG
    for key in sys.argv[1:]:
        cur = cur[key]
    print(cur if not isinstance(cur, (dict, list)) else json.dumps(cur))
