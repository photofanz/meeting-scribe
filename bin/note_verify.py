#!/usr/bin/env python
"""
S6 — the verification gate for the private pipeline.

Every check here is decidable by a program. That is the entry requirement, not
a coincidence: the private path's original failure was that nothing checked the
output at all, and a check that needs a model to adjudicate it would just move
the trust problem rather than solve it.

    每則引文必須能回溯到某個 turn        -> 移除該引文並記錄
    每個數字必須出現在它自己的 turn 原文  -> 移除並記錄
    簡體字                              -> 自動轉換（bin/zhtw.py）
    議題數 == S3 算出的議題數            -> 重跑缺的那一節
    action_items.json 結構              -> 重跑
    章節骨架完整                         -> Python 補（本來就由 Python 生成）

Two of those are worth stating plainly. Quote traceability *should* be
impossible to fail — `private_pipeline.quote_for` cuts every quotation out of
the transcript by turn index, so a failure here means a bug in this repo, not a
hallucination. Checking it anyway is the point: it is the assertion that the
design property still holds, and it is what a fabricated quote injected into a
note would trip on.

A failure never fails the job. Bad material is removed and recorded, a missing
section is rewritten on its own, and everything that survived is still
delivered — a note with one quote fewer beats no note at all.

    python bin/note_verify.py <job_dir>     # audit a job on disk
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chunker  # noqa: E402
import private_pipeline as pp  # noqa: E402
import schemas  # noqa: E402
from chunker import Turn  # noqa: E402
from zhtw import to_zhtw  # noqa: E402

# `> [00:12:01] 王總：「原文」`, with the timestamp and speaker both optional so
# a hand-edited note is still checked rather than skipped.
QUOTE_LINE = re.compile(r"^>\s*(?:\[[\d:]+\]\s*)?(?:[^：:「]{0,24}[：:])?\s*「(?P<text>.+)」\s*$")
SECTION_HEADING = re.compile(r"^###\s+\d+\.\s*(?P<title>.+?)\s*$")
DOC_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$")

ACTION_ITEMS = schemas._obj({
    "meeting": {"type": "object"},
    "decisions": schemas._arr(schemas._obj({"decision": {"type": "string"},
                                            "basis": {"type": "string"}})),
    "actions": schemas._arr(schemas._obj({"item": {"type": "string"},
                                          "responsible": {"type": "string"},
                                          "deadline": {"type": "string"},
                                          "status": {"type": "string"}})),
    "risks": schemas._arr(schemas._obj({"risk": {"type": "string"},
                                        "source": {"type": "string"}})),
    "numbers": schemas._arr({"type": "string"}),
}, title="action_items")


@dataclass
class Finding:
    kind: str            # quote | number | simplified | topic_count | schema | skeleton
    severity: str        # error | warn
    detail: str
    file: str = ""
    topic_id: str = ""
    # Whether this is fixable only by asking the model for that section again.
    rerun: bool = False
    evidence: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "severity": self.severity, "detail": self.detail,
                "file": self.file, "topic_id": self.topic_id, "rerun": self.rerun,
                "evidence": self.evidence[:200]}


def _haystack(turns: list[Turn]) -> str:
    """Everything that was actually said, in the one normalised form quotes use."""
    return pp.squash(" ".join(t.text for t in turns))


def quote_lines(text: str) -> list[tuple[int, str]]:
    """(line number, quoted text) for every blockquote in a note."""
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        m = QUOTE_LINE.match(line.strip())
        if m:
            out.append((n, m.group("text").strip()))
    return out


def verify(job_dir: Path, evidence: dict, turns: list[Turn], *,
           stems: list[str] | None = None) -> list[Finding]:
    """Audit the assembled deliverables against the evidence they came from."""
    job_dir = Path(job_dir)
    stems = stems or [p.stem for p in sorted(job_dir.glob("note_*.md"))]
    findings: list[Finding] = []
    hay = _haystack(turns)
    by_index = {t.index: t for t in turns}

    # -- numbers: the one near-verbatim field the model is allowed ----------- #
    for number in evidence.get("numbers") or []:
        turn = by_index.get(number.get("turn"))
        literal = pp.squash(number.get("literal") or "")
        if turn is None or literal not in pp.squash(turn.text):
            findings.append(Finding(
                "number", "error",
                f"數字「{number.get('literal')}」在 turn {number.get('turn')} 的原文裡找不到",
                evidence=literal))

    expected_topics = len(evidence.get("topics") or [])
    expected_headings = {t["id"]: t for t in (evidence.get("topics") or [])}

    for stem in stems:
        path = job_dir / f"{stem}.md"
        if not path.exists():
            findings.append(Finding("skeleton", "error", f"{stem}.md 不存在", file=f"{stem}.md"))
            continue
        text = path.read_text(errors="ignore")

        # -- quotes must be traceable to a turn --------------------------- #
        for line_no, quoted in quote_lines(text):
            if pp.squash(quoted) not in hay:
                findings.append(Finding(
                    "quote", "error",
                    f"{stem}.md 第 {line_no} 行的引文無法回溯到任何 turn",
                    file=f"{stem}.md", evidence=quoted))

        # -- simplified characters ----------------------------------------- #
        if to_zhtw(text) != text:
            findings.append(Finding("simplified", "warn", f"{stem}.md 含簡體字，將自動轉換",
                                    file=f"{stem}.md"))

        # -- coverage: Python counted the topics, so this cannot be argued -- #
        sections = [m.group("title") for m in
                    (SECTION_HEADING.match(l) for l in text.split("\n")) if m]
        if len(sections) != expected_topics:
            missing = _missing_topics(sections, expected_headings)
            findings.append(Finding(
                "topic_count", "error",
                f"{stem}.md 有 {len(sections)} 個議題，S3 算出 {expected_topics} 個",
                file=f"{stem}.md",
                topic_id=missing[0] if len(missing) == 1 else "",
                rerun=bool(missing)))
            for topic_id in missing[1:]:
                findings.append(Finding("topic_count", "error",
                                        f"{stem}.md 缺少議題 {topic_id}",
                                        file=f"{stem}.md", topic_id=topic_id, rerun=True))

        # -- skeleton ------------------------------------------------------- #
        present = {m.group("title") for m in
                   (DOC_HEADING.match(l) for l in text.split("\n")) if m}
        for key in pp.SKELETONS.get(stem, pp.SKELETONS["note_general"]):
            wanted = pp.HEADINGS[key]
            if not any(wanted in title for title in present):
                findings.append(Finding("skeleton", "error",
                                        f"{stem}.md 缺少章節「{wanted}」", file=f"{stem}.md"))

    # -- action_items.json ------------------------------------------------- #
    ai_path = job_dir / "action_items.json"
    if not ai_path.exists():
        findings.append(Finding("schema", "error", "action_items.json 不存在",
                                file="action_items.json"))
    else:
        try:
            data = json.loads(ai_path.read_text())
        except Exception as exc:  # noqa: BLE001
            findings.append(Finding("schema", "error", f"action_items.json 不是合法 JSON：{exc}",
                                    file="action_items.json"))
        else:
            for problem in schemas.validate(data, ACTION_ITEMS)[:5]:
                findings.append(Finding("schema", "error", f"action_items.json {problem}",
                                        file="action_items.json"))
    return findings


def _missing_topics(section_titles: list[str], topics: dict[str, dict]) -> list[str]:
    """Which topic ids have no section, matched on heading similarity.

    S4 is allowed to sharpen the heading it was given, so this cannot be an
    equality test; a topic whose best match among the written sections is
    weak is the one that went missing.
    """
    missing = []
    for topic_id, topic in topics.items():
        best = max((pp.label_similarity(topic["label"], title) for title in section_titles),
                   default=0.0)
        if best < pp.LABEL_SIMILARITY:
            missing.append(topic_id)
    return missing


def autofix(job_dir: Path, stems: list[str] | None = None) -> dict:
    """Apply the repairs that need no judgement: drop bad quotes, convert 簡體.

    Removal rather than rewriting, deliberately. A quotation that cannot be
    traced to a turn has no correct replacement — the honest fix is that the
    reader never sees it.
    """
    job_dir = Path(job_dir)
    stems = stems or [p.stem for p in sorted(job_dir.glob("note_*.md"))]
    source = job_dir / "transcript_clean.md"
    turns = [t for t in chunker.parse_transcript(source.read_text(errors="ignore"))
             if not t.is_preamble] if source.exists() else []
    hay = _haystack(turns)
    counts = {"quotes_removed": 0, "simplified_converted": 0}

    for stem in stems:
        path = job_dir / f"{stem}.md"
        if not path.exists():
            continue
        original = path.read_text(errors="ignore")
        kept = []
        for line in original.split("\n"):
            m = QUOTE_LINE.match(line.strip())
            if m and hay and pp.squash(m.group("text")) not in hay:
                counts["quotes_removed"] += 1
                continue
            kept.append(line)
        text = "\n".join(kept)
        converted = to_zhtw(text)
        if converted != text:
            counts["simplified_converted"] += 1
            text = converted
        if text != original:
            path.write_text(text)
    return counts


def report(findings: list[Finding]) -> None:
    if not findings:
        print("[verify] 全部通過")
        return
    errors = [f for f in findings if f.severity == "error"]
    print(f"[verify] {len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
    for finding in findings[:12]:
        mark = "✗" if finding.severity == "error" else "!"
        print(f"[verify] {mark} {finding.kind}: {finding.detail}")
    if len(findings) > 12:
        print(f"[verify]   …另外 {len(findings) - 12} 項")


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit a private-pipeline job's deliverables.")
    ap.add_argument("job_dir")
    ap.add_argument("--fix", action="store_true", help="apply the no-judgement repairs")
    args = ap.parse_args()

    job_dir = Path(args.job_dir).expanduser().resolve()
    evidence_path = job_dir / ".review" / "evidence.json"
    if not evidence_path.exists():
        print(f"[verify] 找不到 {evidence_path}——這個 job 不是 evidence 管線產出的？")
        return 2
    evidence = json.loads(evidence_path.read_text())
    turns = [t for t in chunker.parse_transcript(
        (job_dir / "transcript_clean.md").read_text(errors="ignore")) if not t.is_preamble]
    findings = verify(job_dir, evidence, turns)
    report(findings)
    if args.fix:
        print(f"[verify] autofix: {autofix(job_dir)}")
    return 1 if any(f.severity == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
