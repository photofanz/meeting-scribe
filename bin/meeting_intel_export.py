#!/usr/bin/env python
"""
Export a completed meeting-scribe job into the meeting-intel ingest bundle shape.

This is the local-first bridge between the two apps:
- upstream: Meetings / meeting-scribe finishes a job under archive/<job_id>/
- downstream: meeting-intel watches a folder for `.json` bundles

Usage:
    python bin/meeting_intel_export.py latest
    python bin/meeting_intel_export.py <job_id>
    python bin/meeting_intel_export.py <job_id> --outdir /path/to/watch/meeting-scribe
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import CONFIG, ROOT  # noqa: E402
from jobstate import read_json  # noqa: E402

EXPORT_VERSION = "0.2"
DEFAULT_MEETING_INTEL_WATCH_DIR = ROOT.parent / "meeting-intel" / "watch" / "meeting-scribe"


def resolve_job_dir(arg: str) -> Path:
    archive = ROOT / "archive"
    if arg == "latest":
        jobs = [d for d in archive.iterdir() if d.is_dir()]
        if not jobs:
            raise SystemExit("[meeting-intel-export] archive/ is empty")
        return max(jobs, key=lambda p: p.stat().st_mtime).resolve()
    p = Path(arg).expanduser()
    if p.exists():
        return p.resolve()
    return (archive / arg).resolve()


def read_text(path: Path) -> str:
    try:
        return path.read_text(errors="ignore").strip()
    except Exception:
        return ""


def first_non_heading_paragraph(markdown_text: str) -> str:
    for raw in markdown_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(">") or line.startswith("-"):
            continue
        return line
    return ""


def normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def query_tokens(*parts: str) -> list[str]:
    joined = " ".join(p for p in parts if p)
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", joined)
    latin = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_./:-]{2,}", joined)
    tokens: list[str] = []
    seen: set[str] = set()
    for token in cjk + latin:
        norm = normalize_text(token)
        if len(norm) < 2 or norm in seen:
            continue
        seen.add(norm)
        tokens.append(norm)
    return tokens[:24]


def pick_evidence_segment_ids(segments: list[dict[str, Any]], *parts: str, limit: int = 3) -> list[str]:
    tokens = query_tokens(*parts)
    scored: list[tuple[int, str]] = []
    for segment in segments:
        segment_id = str(segment.get("id") or "")
        hay = normalize_text(str(segment.get("text") or ""))
        if not segment_id or not hay:
            continue
        score = 0
        for token in tokens:
            if token in hay:
                score += min(len(token), 8)
        if score > 0:
            scored.append((score, segment_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    picked = [segment_id for _, segment_id in scored[:limit]]
    if picked:
        return picked
    fallback = next((str(seg.get("id")) for seg in segments if seg.get("id")), "")
    return [fallback] if fallback else []


def parse_speaker_guess(value: str) -> tuple[str, str | None]:
    text = (value or "").strip()
    m = re.match(r"^(.*?)（(.+?)）$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text, None


def build_speakers(transcript: dict[str, Any], questions: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    guessed_by_label: dict[str, dict[str, str | None]] = {}
    for item in questions.get("speakers") or []:
        label = str(item.get("label") or "").strip()
        guess = str(item.get("guess") or "").strip()
        if not label or not guess:
            continue
        display_name, role_hint = parse_speaker_guess(guess)
        guessed_by_label[label] = {
            "displayName": display_name,
            "roleHint": role_hint or str(item.get("confidence") or "").strip() or None,
        }

    speakers_seen: dict[str, dict[str, Any]] = {}
    speaker_id_map: dict[str, str] = {}
    for segment in transcript.get("segments") or []:
        raw_speaker = segment.get("speaker")
        if isinstance(raw_speaker, int) and raw_speaker >= 0:
            ordinal = raw_speaker + 1
            speaker_id = f"spk-{ordinal}"
            label = f"講者{ordinal}"
        else:
            ordinal = None
            speaker_id = f"spk-unknown-{len(speakers_seen) + 1}"
            label = "未知講者"
        speaker_id_map[str(raw_speaker)] = speaker_id
        if speaker_id in speakers_seen:
            continue
        guessed = guessed_by_label.get(label, {})
        speakers_seen[speaker_id] = {
            "id": speaker_id,
            "label": label,
            **({"displayName": guessed.get("displayName")} if guessed.get("displayName") else {}),
            **({"roleHint": guessed.get("roleHint")} if guessed.get("roleHint") else {}),
        }
    return list(speakers_seen.values()), speaker_id_map


def build_segments(transcript: dict[str, Any], speaker_id_map: dict[str, str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, segment in enumerate(transcript.get("segments") or [], start=1):
        raw_speaker = segment.get("speaker")
        if isinstance(raw_speaker, int) and raw_speaker >= 0:
            speaker_label = f"講者{raw_speaker + 1}"
        else:
            speaker_label = "未知講者"
        speaker_id = speaker_id_map.get(str(raw_speaker)) or f"spk-fallback-{index}"
        items.append(
            {
                "id": f"seg-{index}",
                "speakerId": speaker_id,
                "speakerLabel": speaker_label,
                "startMs": int(round(float(segment.get("start") or 0) * 1000)),
                "endMs": int(round(float(segment.get("end") or 0) * 1000)),
                "text": str(segment.get("text") or "").strip(),
            }
        )
    return items


def severity_from_text(text: str) -> str:
    norm = text.lower()
    if any(key in norm for key in ["高", "critical", "重大", "立即", "勒索", "資安", "風險", "無法"]):
        return "high"
    if any(key in norm for key in ["中", "待", "依賴", "未定", "需要"]):
        return "medium"
    return "low"


def build_topics(data: dict[str, Any], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    topics: list[dict[str, Any]] = []
    decisions = [item for item in (data.get("decisions") or []) if isinstance(item, dict)]
    actions = [item for item in (data.get("action_items") or []) if isinstance(item, dict)]
    risks = [item for item in (data.get("risks") or []) if isinstance(item, dict)]

    for item in decisions[:2]:
        title = str(item.get("decision") or "").strip()
        basis = str(item.get("basis") or item.get("status") or "").strip()
        if title:
            topics.append(
                {
                    "title": title[:28],
                    "summary": basis or title,
                    "confidence": 0.82,
                    "evidenceSegmentIds": pick_evidence_segment_ids(segments, title, basis),
                }
            )

    if actions:
        action_summary = "；".join(str(item.get("item") or "").strip() for item in actions[:3] if str(item.get("item") or "").strip())
        if action_summary:
            topics.append(
                {
                    "title": "待辦與推進節點",
                    "summary": action_summary,
                    "confidence": 0.79,
                    "evidenceSegmentIds": pick_evidence_segment_ids(segments, action_summary),
                }
            )

    if risks:
        risk_summary = "；".join(str(item.get("risk") or item.get("label") or "").strip() for item in risks[:2] if str(item.get("risk") or item.get("label") or "").strip())
        if risk_summary:
            topics.append(
                {
                    "title": "主要風險與前置限制",
                    "summary": risk_summary,
                    "confidence": 0.77,
                    "evidenceSegmentIds": pick_evidence_segment_ids(segments, risk_summary),
                }
            )

    return topics[:4]


def build_decisions(data: dict[str, Any], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for decision in [item for item in (data.get("decisions") or []) if isinstance(item, dict)]:
        summary = str(decision.get("decision") or "").strip()
        basis = str(decision.get("basis") or "").strip()
        status_text = str(decision.get("status") or "").strip()
        if not summary:
            continue
        lowered = status_text.lower()
        status = "confirmed" if "已決議" in status_text or "confirmed" in lowered else "uncertain" if "未定" in status_text else "proposed"
        items.append(
            {
                "summary": summary,
                "confidence": 0.84 if status == "confirmed" else 0.68,
                "status": status,
                "evidenceSegmentIds": pick_evidence_segment_ids(segments, summary, basis, status_text),
            }
        )
    return items


def build_actions(data: dict[str, Any], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for action in [item for item in (data.get("action_items") or []) if isinstance(item, dict)]:
        summary = str(action.get("item") or "").strip()
        owner = str(action.get("owner") or "").strip() or None
        due = str(action.get("due") or "").strip() or None
        status = str(action.get("status") or "").strip()
        if not summary:
            continue
        items.append(
            {
                "action": summary,
                **({"ownerCandidate": owner} if owner else {}),
                **({"dueCandidate": due} if due else {}),
                **({"dependencyCandidate": status} if status else {}),
                "confidence": 0.86 if owner and due and due != "未定" else 0.73,
                "evidenceSegmentIds": pick_evidence_segment_ids(segments, summary, owner or "", due or "", status),
            }
        )
    return items


def build_risks(data: dict[str, Any], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for risk in [item for item in (data.get("risks") or []) if isinstance(item, dict)]:
        label = str(risk.get("risk") or risk.get("label") or "").strip()
        detail = str(risk.get("mitigation") or risk.get("detail") or "").strip()
        raised_by = str(risk.get("raised_by") or "").strip()
        if not label:
            continue
        detail_text = detail or raised_by or label
        items.append(
            {
                "label": label[:60],
                "detail": detail_text,
                "severity": severity_from_text(f"{label} {detail_text}"),
                "evidenceSegmentIds": pick_evidence_segment_ids(segments, label, detail_text, raised_by),
            }
        )
    return items


def detect_report_type(meta: dict[str, Any], actions: list[dict[str, Any]]) -> tuple[str, str]:
    meeting_type = str(meta.get("meeting_type") or "").strip().lower()
    if meeting_type in {"client", "partner"}:
        return "advisory", "客戶／合作型會議，較適合先進 advisory review flow。"
    if len(actions) >= 3:
        return "action", "待辦項目明確且數量較多，適合 action report 起手。"
    return "record", "目前資訊較中性，先以 meeting record 承接。"


def build_artifacts(job_dir: Path, report: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for source in sorted(job_dir.glob("source.*")):
        if source.is_file():
            artifacts.append({"kind": "audio", "path": str(source), "note": "原始錄音"})
            break
    if (job_dir / "transcript.md").exists():
        artifacts.append({"kind": "transcript_raw", "path": str(job_dir / "transcript.md"), "mimeType": "text/markdown", "note": "原始逐字稿 markdown"})
    if (job_dir / "transcript_clean.md").exists():
        artifacts.append({"kind": "transcript_cleaned", "path": str(job_dir / "transcript_clean.md"), "mimeType": "text/markdown", "note": "回答後整理過的逐字稿"})
    if (job_dir / "questions.json").exists():
        artifacts.append({"kind": "diarization", "path": str(job_dir / "questions.json"), "mimeType": "application/json", "note": "掃描階段的講者／名詞候選"})
    for item in report.get("delivery") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not path:
            continue
        artifacts.append({"kind": "notes", "path": path, "note": f"交付文件：{item.get('stem')}.{item.get('fmt')}"})
    return artifacts


def meeting_intel_export_config(cfg: dict | None = None) -> dict[str, Any]:
    cfg = cfg or CONFIG
    integrations = cfg.get("integrations") or {}
    mi = integrations.get("meeting_intel") or {}
    return {
        "enabled": bool(mi.get("enabled", False)),
        "watch_dir": str(Path(mi.get("watch_dir") or os.environ.get("MEETING_INTEL_WATCH_DIR") or DEFAULT_MEETING_INTEL_WATCH_DIR).expanduser()),
        "auto_export_on_done": bool(mi.get("auto_export_on_done", False)),
    }


def build_bundle(job_dir: Path) -> dict[str, Any]:
    meta = read_json(job_dir / "meta.json") or {}
    status = read_json(job_dir / "status.json") or {}
    report = read_json(job_dir / "delivery.json") or {}
    transcript = read_json(job_dir / "transcript.json") or {}
    questions = read_json(job_dir / "questions.json") or {}
    extracted_data = read_json(job_dir / "action_items.json") or {}

    if not transcript.get("segments"):
        raise RuntimeError("transcript.json is missing segments")

    speakers, speaker_id_map = build_speakers(transcript, questions)
    segments = build_segments(transcript, speaker_id_map)
    actions = build_actions(extracted_data, segments)
    report_type, recommendation_reason = detect_report_type(meta, actions)

    note_paths = [
        job_dir / "note_general.md",
        job_dir / "note_client.md",
        job_dir / "note_self.md",
        job_dir / "note_partner.md",
        job_dir / "note_interview.md",
    ]
    note_summary = ""
    for path in note_paths:
        if path.exists():
            note_summary = first_non_heading_paragraph(read_text(path))
            if note_summary:
                break

    cleaned_text = read_text(job_dir / "transcript_clean.md") or read_text(job_dir / "transcript_draft.md") or read_text(job_dir / "transcript.md")
    raw_text = read_text(job_dir / "transcript.md") or cleaned_text
    summary = note_summary or str((extracted_data.get("meeting") or {}).get("note") or "").strip()
    if not summary:
        title = str(meta.get("title") or status.get("result", {}).get("title") or "這場會議").strip()
        summary = f"{title} 已完成轉寫與初步結構化整理，可進入 meeting-intel review flow。"

    result = status.get("result") or {}
    duration_minutes = None
    try:
        duration_minutes = round(float(result.get("duration") or 0) / 60)
    except Exception:
        duration_minutes = None

    client = str(meta.get("client") or result.get("client") or "").strip() or None
    project = client or str(meta.get("context") or "").strip()[:40] or None
    meeting_language = str(meta.get("language") or result.get("asr_language") or "unknown").strip().lower() or "unknown"
    expected = meta.get("num_speakers") or result.get("expected_speakers")
    try:
        expected_int = int(str(expected).strip()) if expected not in (None, "") else None
    except Exception:
        expected_int = None

    bundle = {
        "bundle": {
            "bundleId": job_dir.name,
            "source": "meeting-scribe",
            "exportVersion": EXPORT_VERSION,
            "exportedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        },
        "meeting": {
            "title": str(meta.get("title") or result.get("title") or job_dir.name),
            **({"project": project} if project else {}),
            **({"client": client} if client else {}),
            **({"meetingDate": str(meta.get('date') or result.get('date') or '').strip()} if str(meta.get('date') or result.get('date') or '').strip() else {}),
            "language": meeting_language if meeting_language in {"zh", "en", "mixed", "unknown"} else "unknown",
            "sensitivity": "private" if str(meta.get("agent_preset") or "").strip().lower() == "private" else "standard",
            **({"durationMinutes": duration_minutes} if duration_minutes else {}),
            **({"participantsExpected": expected_int} if expected_int is not None else {}),
            "speakersDetected": len(speakers),
            "tags": [tag for tag in [str(meta.get("meeting_type") or "").strip(), str(meta.get("agent_preset") or "").strip(), "meeting-scribe-export"] if tag],
        },
        "speakers": speakers,
        "transcript": {
            "rawText": raw_text,
            "cleanedText": cleaned_text,
            "segments": segments,
        },
        "extracted": {
            "summary": summary,
            "topics": build_topics(extracted_data, segments),
            "decisions": build_decisions(extracted_data, segments),
            "actions": actions,
            "risks": build_risks(extracted_data, segments),
            "recommendedReportType": report_type,
            "recommendationReason": recommendation_reason,
        },
        "artifacts": build_artifacts(job_dir, report),
    }
    return bundle


def export_job(job_dir: Path, outdir: Path) -> Path:
    bundle = build_bundle(job_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"{job_dir.name}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(bundle, ensure_ascii=False, indent=2))
    tmp.replace(path)
    return path


def maybe_auto_export(job_dir: Path, *, cfg: dict | None = None) -> Path | None:
    mi = meeting_intel_export_config(cfg)
    if not (mi["enabled"] and mi["auto_export_on_done"]):
        return None
    return export_job(job_dir, Path(mi["watch_dir"]))


def main() -> int:
    ap = argparse.ArgumentParser(description="Export a meeting-scribe job as a meeting-intel ingest bundle.")
    ap.add_argument("job", help="archive/<job_id>, a job id, or 'latest'")
    ap.add_argument("--outdir", default=None, help="target watch folder; defaults to integrations.meeting_intel.watch_dir")
    args = ap.parse_args()

    job_dir = resolve_job_dir(args.job)
    mi = meeting_intel_export_config(CONFIG)
    outdir = Path(args.outdir or mi["watch_dir"]).expanduser().resolve()
    path = export_job(job_dir, outdir)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
