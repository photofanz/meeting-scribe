#!/usr/bin/env python
"""
Measure a finished meeting note against the transcript it came from.

P4 of the redesign, and not optional: without it every later change to the
private pipeline is a guess. The private path was already tuned once on a
single impression ("the note looks short"), which is how a 0.061 output ratio
survived long enough to reach a client folder.

Everything measured here is computed from files on disk, so a note written by
Claude a month ago and one written by the local pipeline this morning are
scored by identical rules:

    coverage    產出／來源字數比, against the 0.30 target the redesign set
    grounding   what fraction of the note's quotations exist in the transcript
    structure   how much of the required skeleton is actually present
    depth       topics per 10k source characters, and points per topic
    cleanliness 簡體字 and stray transcript timestamps

The composite is a weighted mean of those five, each clamped to 0–1. It is a
ranking aid, not a grade: the point is that the ordering it produces is
reproducible and that a bad note cannot look good by being confident.

    python bin/note_eval.py --archive          # score every archived job
    python bin/note_eval.py jobs/<id> --json   # one job, machine-readable
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chunker  # noqa: E402
import private_pipeline as pp  # noqa: E402
from config import ROOT  # noqa: E402
from zhtw import to_zhtw  # noqa: E402

# The redesign's acceptance bar for P2. Notes written by a CLI agent measure
# 0.22–0.79 of their source; the private single-writer run measured 0.061.
TARGET_RATIO = 0.30

# A one-hour meeting that yields fewer than five topics is under-covered, per
# NOTE_SPECS.md. An hour of Mandarin runs ~21k characters, so five topics per
# 21k is the reference density.
TARGET_TOPIC_DENSITY = 5 / 21_000

# 「…」 spans this long are quotations. Shorter ones are how Chinese writes
# scare quotes and defined terms (「未指派」, 「已決議」), and counting those as
# unattributable quotations would punish a note for its own vocabulary.
QUOTE_MIN_CHARS = 10
INLINE_QUOTE = re.compile(r"[「『]([^「」『』]{%d,})[」』]" % QUOTE_MIN_CHARS)
BLOCK_QUOTE = re.compile(r"^>\s*(.+?)\s*$", re.MULTILINE)
TIMESTAMP = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")
HEADING = re.compile(r"^#{2,3}\s+(.+?)\s*$", re.MULTILINE)
TOPIC_HEADING = re.compile(r"^###\s+", re.MULTILINE)

REQUIRED_SECTIONS = ["重點", "議題", "決議", "待辦", "未解決", "下次"]

WEIGHTS = {"coverage": 0.30, "grounding": 0.25, "structure": 0.15,
           "depth": 0.20, "cleanliness": 0.10}


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def source_text(job_dir: Path) -> tuple[str, str]:
    """(text, which file). The cleaned transcript if there is one."""
    for name in ("transcript_clean.md", "transcript_draft.md", "transcript.md"):
        path = job_dir / name
        if path.exists():
            return path.read_text(errors="ignore"), name
    return "", ""


def spoken_text(raw: str) -> tuple[str, str]:
    """(normalised utterances, how they were obtained).

    Front matter is not source material, so the parsed turns are preferred.
    But the archive predates the current transcript writer, and jobs from July
    use `**高老師**：…` with no timestamp, which `parse_transcript` reads as one
    big preamble. Scoring those as having a zero-character source would report
    a perfectly good note as the worst in the archive — so the raw file is the
    fallback, and the row says which was used.
    """
    turns = [t for t in chunker.parse_transcript(raw) if not t.is_preamble]
    if len(turns) >= 2:
        return pp.squash(" ".join(t.text for t in turns)), "turns"
    return pp.squash(raw), "raw"


def quotations(note: str) -> list[str]:
    """Every span the note presents as somebody's actual words."""
    out = [m.strip() for m in INLINE_QUOTE.findall(note)]
    for line in BLOCK_QUOTE.findall(note):
        inner = INLINE_QUOTE.search(line)
        text = inner.group(1) if inner else line
        text = re.sub(r"^\[[\d:]+\]\s*", "", text).strip()
        text = re.sub(r"^[^：:]{0,24}[：:]\s*", "", text).strip()
        if len(text) >= QUOTE_MIN_CHARS:
            out.append(text)
    return [q for q in dict.fromkeys(out) if q]


def metrics(job_dir: Path) -> dict:
    """Everything measurable about one job's deliverables."""
    job_dir = Path(job_dir)
    raw, src_name = source_text(job_dir)
    spoken, how = spoken_text(raw)
    source_chars = len(spoken)

    notes = sorted(job_dir.glob("note_*.md"))
    note_text = "\n".join(p.read_text(errors="ignore") for p in notes)
    note_chars = len(note_text)

    quotes = quotations(note_text)
    traceable = [q for q in quotes if pp.squash(q) in spoken]
    grounding = len(traceable) / len(quotes) if quotes else 0.0

    headings = HEADING.findall(note_text)
    present = sum(1 for want in REQUIRED_SECTIONS
                  if any(want in h for h in headings))
    topics = len(TOPIC_HEADING.findall(note_text))

    simplified = sum(1 for a, b in zip(note_text, to_zhtw(note_text)) if a != b)
    stamps = len(TIMESTAMP.findall(note_text))

    ratio = note_chars / source_chars if source_chars else 0.0
    density = topics / source_chars if source_chars else 0.0
    decided = note_text.count("已決議")
    pending = note_text.count("待定")

    scores = {
        "coverage": _clamp(ratio / TARGET_RATIO),
        # A note with no quotations at all is not "perfectly grounded" — it is
        # a note that never showed its work, which NOTE_SPECS.md forbids.
        "grounding": grounding,
        "structure": present / len(REQUIRED_SECTIONS),
        "depth": _clamp(density / TARGET_TOPIC_DENSITY),
        "cleanliness": _clamp(1.0 - (simplified / 50.0) - (stamps / 20.0)),
    }
    return {
        "job": job_dir.name,
        "source_file": src_name,
        "source_parsed": how,
        "source_chars": source_chars,
        "note_files": [p.name for p in notes],
        "note_chars": note_chars,
        "ratio": round(ratio, 3),
        "topics": topics,
        "topics_per_hour": round(density * 21_000, 1),
        "quotes": len(quotes),
        "quotes_traceable": len(traceable),
        "quote_rate": round(grounding, 3),
        "untraceable_examples": [q[:60] for q in quotes if q not in traceable][:3],
        "sections_present": f"{present}/{len(REQUIRED_SECTIONS)}",
        "simplified_chars": simplified,
        "timestamps": stamps,
        "decided_mentions": decided,
        "pending_mentions": pending,
        "scores": {k: round(v, 3) for k, v in scores.items()},
        "score": round(sum(scores[k] * w for k, w in WEIGHTS.items()), 3),
    }


def evaluate(job_dirs: list[Path]) -> list[dict]:
    rows = [metrics(d) for d in job_dirs if (d / "meta.json").exists() or list(d.glob("note_*.md"))]
    return sorted(rows, key=lambda r: -r["score"])


def separation(rows: list[dict], worst_job: str) -> dict:
    """How far the known-bad job sits below everything else.

    A ranking that merely puts the disaster last is not evidence of anything;
    an ordering can do that by chance. The gap is the claim worth making, so
    it is computed rather than eyeballed.
    """
    target = next((r for r in rows if worst_job in r["job"]), None)
    others = [r for r in rows if target is None or r["job"] != target["job"]]
    if target is None or not others:
        return {}
    floor = min(r["score"] for r in others)
    return {
        "job": target["job"],
        "score": target["score"],
        "rank": rows.index(target) + 1,
        "of": len(rows),
        "next_worst": min(others, key=lambda r: r["score"])["job"],
        "next_worst_score": floor,
        "gap": round(floor - target["score"], 3),
        "is_last": rows[-1]["job"] == target["job"],
    }


def table(rows: list[dict]) -> str:
    head = (f"{'#':>2}  {'job':<38} {'score':>6} {'ratio':>6} {'topics':>6} "
            f"{'quote':>10} {'sect':>5} {'note':>8} {'src':>8}")
    lines = [head, "-" * len(head)]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i:>2}  {r['job']:<38} {r['score']:>6.3f} {r['ratio']:>6.3f} "
            f"{r['topics']:>6} {r['quotes_traceable']:>4}/{r['quotes']:<5} "
            f"{r['sections_present']:>5} {r['note_chars']:>8,} {r['source_chars']:>8,}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Score meeting notes against their transcripts.")
    ap.add_argument("job_dirs", nargs="*")
    ap.add_argument("--archive", action="store_true", help="score every job in archive/")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--worst", default="群耀_94db8a",
                    help="job expected to rank last; the separation gap is reported for it")
    args = ap.parse_args()

    dirs = [Path(d).expanduser().resolve() for d in args.job_dirs]
    if args.archive or not dirs:
        dirs += [d for d in sorted((ROOT / "archive").iterdir()) if d.is_dir()]
    rows = evaluate(dirs)
    if not rows:
        print("[eval] 沒有可評分的 job")
        return 2

    gap = separation(rows, args.worst)
    if args.json:
        print(json.dumps({"rows": rows, "separation": gap}, ensure_ascii=False, indent=2))
        return 0

    print(table(rows))
    print()
    if gap:
        verdict = "✓" if gap["is_last"] else "✗"
        print(f"{verdict} {gap['job']}：score {gap['score']}，"
              f"排名 {gap['rank']}/{gap['of']}，"
              f"與次低的 {gap['next_worst']}（{gap['next_worst_score']}）相距 {gap['gap']}")
    print(f"權重：{WEIGHTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
