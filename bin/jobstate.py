#!/usr/bin/env python
"""
Canonical job state — the single source of truth for "where is this job".

Before this module the truth was scattered: `status.json` knew about
transcription, `delivery.json` knew about documents, and nothing knew about
the review stage in between. The web UI needs one answer, so it reads
`state.json` and nothing else.

    transcribing -> scanning -> awaiting_answers -> writing -> done
                                                        \\-> error (any stage)

`status.json` is still written by process_meeting.py and is still the
authority on ASR progress; `state.json` links to it rather than duplicating
it. Both are written atomically (tmp + rename) because the HTTP server polls
these files while background jobs rewrite them.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import ROOT  # noqa: E402

ARCHIVE = ROOT / "archive"

# state -> (label, is_terminal, needs_user)
STATES: dict[str, tuple[str, bool, bool]] = {
    "transcribing":     ("轉寫中", False, False),
    "scanning":         ("掃描中", False, False),
    "awaiting_answers": ("待回答", False, True),
    "writing":          ("撰寫中", False, False),
    "done":             ("完成", True, False),
    "error":            ("失敗", True, False),
}

# Files that are cheap to regenerate. "Clean outputs" deletes exactly these and
# nothing else — the audio, the raw transcript and the job metadata survive so
# the job can always be re-run.
OUTPUT_GLOBS = (
    "note_*.md", "note_*.pdf", "note_*.docx",
    "transcript_clean.*", "transcript_draft.md",
    "action_items.json", "email_draft.md", "INDEX.md",
    "delivery.json", "agent_report.json",
    "*.md.txt",
)

# Never deleted by "clean outputs" — losing any of these makes the job
# unrepeatable.
PROTECTED = ("source.*", "meta.json", "status.json", "state.json",
             "transcript.md", "transcript.json", "transcript.txt",
             "questions.json", "answers.json")


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {} if default is None else default


def job_dir(job_id: str) -> Path:
    """Resolve a job id to its folder, refusing anything outside archive/."""
    d = (ARCHIVE / job_id).resolve()
    if not str(d).startswith(str(ARCHIVE.resolve()) + os.sep):
        raise ValueError(f"job id escapes archive/: {job_id!r}")
    return d


def load(job_id_or_dir) -> dict:
    d = Path(job_id_or_dir) if os.sep in str(job_id_or_dir) else job_dir(str(job_id_or_dir))
    st = read_json(d / "state.json")
    if not st:
        st = _infer(d)
    st.setdefault("job_id", d.name)
    return st


def _infer(d: Path) -> dict:
    """Best-effort state for jobs created before state.json existed.

    Purely derived from files on disk, so re-running it is harmless. Old jobs
    that already have documents land in `done`; ones that stopped after ASR
    land in `scanning`-eligible `transcribing`+done-transcript.
    """
    status = read_json(d / "status.json")
    has_tr = (d / "transcript.md").exists()
    has_note = any(d.glob("note_*.md"))
    if status.get("state") == "error":
        state = "error"
    elif has_note:
        state = "done"
    elif has_tr:
        state = "awaiting_answers" if (d / "questions.json").exists() else "transcribing"
    else:
        state = "transcribing"
    if state == "transcribing" and status.get("state") == "done" and has_tr:
        # ASR finished but no review ever started: treat as ready for stage 1.
        state = "transcribing"
    return {
        "job_id": d.name,
        "state": state,
        "archived": False,
        "outputs_cleaned": False,
        "error": status.get("error"),
        "updated_at": status.get("updated_at") or 0,
        "history": [],
        "legacy": True,
    }


def save(job_id_or_dir, **fields) -> dict:
    """Merge `fields` into the job's state and persist it."""
    d = Path(job_id_or_dir) if os.sep in str(job_id_or_dir) else job_dir(str(job_id_or_dir))
    d.mkdir(parents=True, exist_ok=True)
    st = load(d)
    prev = st.get("state")
    st.update(fields)
    st["job_id"] = d.name
    st["updated_at"] = time.time()
    st.pop("legacy", None)
    if fields.get("state") and fields["state"] != prev:
        st.setdefault("history", []).append(
            {"t": time.time(), "state": fields["state"], "note": fields.get("note", "")}
        )
        st["history"] = st["history"][-40:]
    _atomic_write(d / "state.json", st)
    return st


def set_state(job_id_or_dir, state: str, note: str = "", error: str | None = None) -> dict:
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    return save(job_id_or_dir, state=state, note=note, error=error)


# --------------------------------------------------------------- summaries --
def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _filename_part(text: str, fallback: str, limit: int) -> str:
    text = unicodedata.normalize("NFKC", str(text or "").strip())
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    return (text or fallback)[:limit]


def display_name(name: str, client: str = "", title: str = "", meeting_date: str = "") -> str:
    """User-facing download name; the on-disk name stays untouched."""
    parts = [
        _filename_part(client, "未命名對象", 24),
        _filename_part(title, "未命名會議", 36),
        _filename_part(meeting_date, "未定日期", 12),
    ]
    return "_".join(parts + [name])


# The download panel is an allowlist, not a denylist. A denylist loses by
# default: the job directory is also the pipeline's scratch space, so every
# intermediate it learns to write (transcript.json, transcript_draft.md,
# INDEX.md, action_items.json) shows up as a "deliverable" until someone
# remembers to exclude it by name. These are the documents a person asked for.
# Note this is deliberately NOT the same list as OUTPUT_GLOBS / cleanup_plan():
# cleanup has to reach every intermediate, which is the opposite job.
DELIVERABLE_ORDER = {"transcript_clean": 0, "note_general": 1, "note_client": 2,
                     "note_self": 3, "note_partner": 4, "note_interview": 5,
                     "email_draft": 6}


def list_files(d: Path, client: str = "", title: str = "", meeting_date: str = "",
               formats: list[str] | None = None) -> list[dict]:
    """User-facing deliverables in a stable order, newest content first.

    `formats` is the user's format selection from `meta.json`. `.md` is always
    written because it is the source for both PDF and Word, so a job that asked
    for PDF only still has `note_general.md` sitting on disk; it belongs in the
    archive, not in the download list. A deliverable whose every format was
    filtered out falls back to `.md`, which keeps an md-only document like
    `email_draft.md` — and any job whose PDF step failed — from vanishing.
    """
    fmt_order = {"md": 0, "pdf": 1, "docx": 2, "json": 3, "txt": 4}
    wanted = {f for f in (formats or []) if f} or None

    by_stem: dict[str, list[Path]] = {}
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        stem, _, _ext = p.name.rpartition(".")
        if stem not in DELIVERABLE_ORDER:
            continue
        by_stem.setdefault(stem, []).append(p)

    out = []
    for stem, paths in by_stem.items():
        keep = [p for p in paths if wanted is None or p.suffix.lstrip(".") in wanted]
        if not keep:
            keep = [p for p in paths if p.suffix == ".md"]
        for p in keep:
            ext = p.suffix.lstrip(".")
            out.append({
                "name": p.name, "stem": stem, "fmt": ext, "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
                "display_name": display_name(p.name, client=client, title=title,
                                             meeting_date=meeting_date),
                "_k": (DELIVERABLE_ORDER[stem], fmt_order.get(ext, 9)),
            })
    out.sort(key=lambda r: r.pop("_k"))
    return out


def summary(job_id_or_dir) -> dict:
    """Everything the jobs list needs about one job, in one dict."""
    d = Path(job_id_or_dir) if os.sep in str(job_id_or_dir) else job_dir(str(job_id_or_dir))
    st = load(d)
    meta = read_json(d / "meta.json")
    status = read_json(d / "status.json")
    res = status.get("result") or {}
    questions = read_json(d / "questions.json")
    answers = read_json(d / "answers.json")
    cards = questions.get("cards") or []
    answered = len(answers.get("cards") or {})

    audio = next((p for p in d.glob("source.*") if p.is_file()), None)
    label, terminal, needs_user = STATES.get(st.get("state", "done"), ("未知", True, False))

    return {
        "job_id": d.name,
        "state": st.get("state"),
        "state_label": label,
        "terminal": terminal,
        "needs_user": needs_user,
        "archived": bool(st.get("archived")),
        "outputs_cleaned": bool(st.get("outputs_cleaned")),
        "error": st.get("error") or status.get("error"),
        "updated_at": st.get("updated_at") or status.get("updated_at") or 0,
        "title": res.get("title") or meta.get("title") or "未命名會議",
        "client": res.get("client") or meta.get("client") or "",
        "date": res.get("date") or meta.get("date") or "",
        "participants": res.get("participants") or meta.get("participants") or "",
        "duration": res.get("duration") or 0,
        "num_speakers": res.get("num_speakers"),
        "expected_speakers": res.get("expected_speakers") or meta.get("num_speakers"),
        "meeting_type": meta.get("meeting_type") or "general",
        "agent_preset": meta.get("agent_preset") or "general",
        "want_note": meta.get("want_note", True),
        "want_transcript": meta.get("want_transcript", False),
        "formats": meta.get("formats") or ["pdf", "md"],
        "questions_total": len(cards),
        "questions_answered": answered,
        "questions_open": max(0, len(cards) - answered),
        # ASR progress, only meaningful while transcribing
        "progress": status.get("progress") or 0,
        "step_label": status.get("step_label") or "",
        "message": status.get("message") or "",
        "size_bytes": dir_size(d),
        "audio_bytes": audio.stat().st_size if audio else 0,
        "has_audio": audio is not None,
        "files": list_files(
            d,
            client=res.get("client") or meta.get("client") or "",
            title=res.get("title") or meta.get("title") or "未命名會議",
            meeting_date=res.get("date") or meta.get("date") or "",
            formats=meta.get("formats") or ["pdf", "md"],
        ),
    }


def all_jobs(include_archived: bool = True) -> list[dict]:
    rows = []
    if not ARCHIVE.is_dir():
        return rows
    for d in ARCHIVE.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            s = summary(d)
        except Exception as exc:  # noqa: BLE001 - one broken job must not hide the rest
            s = {"job_id": d.name, "state": "error", "state_label": "失敗",
                 "error": f"讀取失敗：{exc}", "title": d.name, "files": [],
                 "size_bytes": 0, "updated_at": 0, "archived": False}
        if not include_archived and s.get("archived"):
            continue
        rows.append(s)
    rows.sort(key=lambda r: (r.get("date") or "", r.get("updated_at") or 0), reverse=True)
    return rows


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Inspect or set job state.")
    ap.add_argument("job", nargs="?", help="job id ('all' to list)")
    ap.add_argument("--set", dest="new", choices=sorted(STATES))
    ap.add_argument("--note", default="")
    a = ap.parse_args()
    if not a.job or a.job == "all":
        for r in all_jobs():
            print(f"{r['state_label']:<6} {r['job_id']}  {r.get('title','')}")
    elif a.new:
        print(json.dumps(set_state(a.job, a.new, a.note), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(summary(a.job), ensure_ascii=False, indent=2))
