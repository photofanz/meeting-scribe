#!/usr/bin/env python
"""
Deterministic transcript slicing.

A two-hour meeting is ~127k characters. Handing that to one agent in one
prompt is how the single-pass writer used to fail: the model reads the first
few thousand characters, writes a confident note about the first twenty
minutes, and silently drops the rest. Nothing in the exit code says so.

So the transcript is sliced here, in Python, before any model sees it. This
module is deliberately model-free and side-effect-free: given the same file it
always produces the same chunks, and a human can eyeball the split with

    python bin/chunker.py <transcript.md> [--max-chars N] [--overlap N]

Two invariants the rest of the pipeline leans on:

  1. A turn is never split. A speaker's utterance is the atom; half an
     utterance produces hallucinated context in the neighbouring chunk.
  2. Chunk N starts with the last `overlap_turns` turns of chunk N-1, flagged
     as context-only. The prompt tells the agent not to re-transcribe them,
     and `Chunk.overlap_count` lets the merge drop them without asking a model
     what it did. Concatenating `chunk.turns[chunk.overlap_count:]` over all
     chunks reproduces the input turn sequence exactly — reviewers should
     assume that property and tests assert it.

Transcript formats in the wild (all tolerated, because old jobs on disk use
older writers):

    **── 講者1 ──**　`[00:00:00 – 00:00:09]`     <- current process_meeting.py
    **[00:00:12] 講者1**                          <- what the agents write back
    **講者1** `[00:00:12]`
    [00:00:12] 講者1：內容

Anything before the first recognised header (the `# 逐字稿：…` front matter)
is kept as a synthetic preamble turn rather than discarded: it carries the
title, date and the "speaker labels are guesses" warning, which is exactly the
context the scanning agent needs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# header patterns
# --------------------------------------------------------------------------- #
# A timestamp is HH:MM:SS or MM:SS. The range separator varies by writer
# (en dash from process_meeting.py, hyphen when hand-edited).
_TS = r"\d{1,2}:\d{2}(?::\d{2})?"
_RANGE = rf"`?\[\s*(?P<ts>{_TS})\s*(?:[–\-—~]\s*(?P<te>{_TS})\s*)?\]`?"

# Order matters: the `──` form is the only one allowed to omit its timestamp,
# because it is unambiguous. The looser bold forms must carry a timestamp or a
# heading like `**修正對照表**` would be mistaken for a speaker turn.
_HEADERS = (
    re.compile(rf"^\*\*\s*──\s*(?P<spk>.*?)\s*──\s*\*\*[\s　]*(?:{_RANGE})?[\s　]*$"),
    re.compile(rf"^\*\*\s*{_RANGE}\s*(?P<spk>[^*]*?)\s*\*\*[\s　]*$"),
    re.compile(rf"^\*\*\s*(?P<spk>[^*\[\]]*?)\s*\*\*[\s　]*{_RANGE}[\s　]*$"),
    re.compile(rf"^{_RANGE}[\s　]*(?P<spk>[^：:]{{0,24}}?)[：:][\s　]*(?P<rest>.*)$"),
)

# Used to recover a start time when the header itself has none: the tiny
# demo jobs put `[00:00:06]` at the head of each body line instead.
_INLINE_TS = re.compile(rf"`?\[\s*(?P<ts>{_TS})\s*\]`?")


def parse_stamp(text: str | None) -> float | None:
    """'01:02:03' or '02:03' -> seconds. None/garbage -> None (never raises)."""
    if not text:
        return None
    parts = text.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        nums = [0] + nums
    if len(nums) != 3:
        return None
    return float(nums[0] * 3600 + nums[1] * 60 + nums[2])


def hhmmss(seconds: float | None) -> str:
    s = int(max(0.0, seconds or 0.0))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
@dataclass
class Turn:
    index: int
    t_start: float
    stamp: str
    speaker: str
    text: str
    raw: str
    t_end: float | None = None
    # The front matter / stray text before the first speaker header. Kept so
    # nothing is lost, flagged so prompts can label it as context.
    is_preamble: bool = False

    @property
    def chars(self) -> int:
        return len(self.raw)

    def to_dict(self) -> dict:
        return {
            "index": self.index, "t_start": self.t_start, "t_end": self.t_end,
            "stamp": self.stamp, "speaker": self.speaker, "text": self.text,
            "is_preamble": self.is_preamble, "chars": self.chars,
        }


@dataclass
class Chunk:
    index: int
    turns: list[Turn] = field(default_factory=list)
    # How many leading turns are carried over from the previous chunk. The
    # merge drops exactly this many; nothing else in the pipeline is allowed
    # to guess.
    overlap_count: int = 0

    @property
    def own_turns(self) -> list[Turn]:
        return self.turns[self.overlap_count:]

    @property
    def overlap_turns(self) -> list[Turn]:
        return self.turns[: self.overlap_count]

    @property
    def char_count(self) -> int:
        return sum(t.chars for t in self.turns)

    @property
    def own_chars(self) -> int:
        return sum(t.chars for t in self.own_turns)

    @property
    def t_start(self) -> float:
        return self.own_turns[0].t_start if self.own_turns else 0.0

    @property
    def t_end(self) -> float:
        if not self.own_turns:
            return 0.0
        last = self.own_turns[-1]
        return last.t_end if last.t_end is not None else last.t_start

    @property
    def stamp_start(self) -> str:
        return hhmmss(self.t_start)

    @property
    def stamp_end(self) -> str:
        return hhmmss(self.t_end)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "turns": len(self.own_turns),
            "overlap": self.overlap_count,
            "chars": self.char_count,
            "own_chars": self.own_chars,
            "t_start": self.t_start, "t_end": self.t_end,
            "stamp_start": self.stamp_start, "stamp_end": self.stamp_end,
        }


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #
def _match_header(line: str) -> dict | None:
    for pat in _HEADERS:
        m = pat.match(line)
        if m:
            g = m.groupdict()
            return {
                "speaker": (g.get("spk") or "").strip(),
                "ts": g.get("ts"),
                "te": g.get("te"),
                "rest": (g.get("rest") or "").strip(),
            }
    return None


def parse_transcript(md_text: str) -> list[Turn]:
    """Split transcript markdown into speaker turns.

    Tolerant by contract: an empty file gives `[]`, a file with no recognised
    header gives a single preamble turn, and a header with neither speaker nor
    timestamp still produces a turn (with `speaker=""` and the previous known
    time). Raising here would take down a whole job over a formatting quirk.
    """
    text = (md_text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []

    lines = text.split("\n")
    blocks: list[dict] = []          # {"header": dict|None, "lines": [str]}
    current: dict | None = None
    preamble: list[str] = []

    for line in lines:
        head = _match_header(line)
        if head is not None:
            current = {"header": head, "lines": [line]}
            if head["rest"]:
                current["body"] = [head["rest"]]
            else:
                current["body"] = []
            blocks.append(current)
        elif current is None:
            preamble.append(line)
        else:
            current["lines"].append(line)
            current["body"].append(line)

    turns: list[Turn] = []
    idx = 0
    if "".join(preamble).strip():
        raw = "\n".join(preamble).strip("\n")
        turns.append(Turn(index=idx, t_start=0.0, stamp="00:00:00", speaker="",
                          text=raw, raw=raw, t_end=None, is_preamble=True))
        idx += 1

    last_t = 0.0
    for block in blocks:
        head = block["header"]
        body = "\n".join(block["body"]).strip("\n")
        t_start = parse_stamp(head["ts"])
        t_end = parse_stamp(head["te"])
        if t_start is None:
            # Body-embedded timestamps are the fallback (older md writer).
            m = _INLINE_TS.search(body)
            t_start = parse_stamp(m.group("ts")) if m else None
        if t_start is None:
            t_start = last_t
        last_t = t_start
        raw = "\n".join(block["lines"]).strip("\n")
        turns.append(Turn(index=idx, t_start=t_start, stamp=hhmmss(t_start),
                          speaker=head["speaker"], text=body.strip(), raw=raw,
                          t_end=t_end))
        idx += 1
    return turns


# --------------------------------------------------------------------------- #
# chunk
# --------------------------------------------------------------------------- #
def chunk(turns: list[Turn], max_chars: int = 14000, overlap_turns: int = 3) -> list[Chunk]:
    """Pack turns into chunks of at most `max_chars` *new* characters.

    A single turn larger than `max_chars` gets a chunk of its own rather than
    being split — an over-long prompt is recoverable, half a sentence is not.
    The budget counts only the chunk's own turns; the overlap is context the
    agent is told to skip, so charging it against the budget would shrink the
    real coverage of every chunk after the first.
    """
    max_chars = max(1, int(max_chars))
    overlap_turns = max(0, int(overlap_turns))
    if not turns:
        return []

    chunks: list[Chunk] = []
    cur = Chunk(index=0)
    used = 0
    for turn in turns:
        if cur.own_turns and used + turn.chars > max_chars:
            chunks.append(cur)
            tail = cur.turns[-overlap_turns:] if overlap_turns else []
            cur = Chunk(index=len(chunks), turns=list(tail), overlap_count=len(tail))
            used = 0
        cur.turns.append(turn)
        used += turn.chars
    if cur.own_turns:
        chunks.append(cur)
    return chunks


def render(chunk_obj: Chunk, *, header: bool = True) -> str:
    """Markdown for a prompt: context turns first, clearly fenced off."""
    out: list[str] = []
    if header:
        out.append(
            f"<!-- chunk {chunk_obj.index:02d} · {chunk_obj.stamp_start}–{chunk_obj.stamp_end} "
            f"· {len(chunk_obj.own_turns)} 段 · {chunk_obj.char_count} 字 -->"
        )
    if chunk_obj.overlap_turns:
        out.append("### 上文（僅供理解，**不要**重複整理，不要寫進輸出）")
        out.append("")
        out += [render_turn(t) for t in chunk_obj.overlap_turns]
        out.append("### 本段（**只**整理這一段）")
        out.append("")
    out += [render_turn(t) for t in chunk_obj.own_turns]
    return "\n".join(out).strip() + "\n"


# A timestamp at the very head of a body, e.g. "`[00:00:06]` 王總你好". The
# older md writer put the time there instead of in the speaker header; once
# render_turn moves it into the header, leaving the original in place would
# print it twice in every downstream document.
_LEADING_TS = re.compile(rf"^\s*`?\[\s*{_TS}\s*\]`?[\s　]*")


def render_turn(turn: Turn) -> str:
    """One turn in the canonical form the agents are asked to echo back."""
    if turn.is_preamble:
        return turn.text.strip() + "\n"
    speaker = turn.speaker or "未知講者"
    return f"**[{turn.stamp}] {speaker}**\n\n{_LEADING_TS.sub('', turn.text)}\n"


def render_turns(turns: list[Turn]) -> str:
    return "\n".join(render_turn(t) for t in turns).strip() + "\n"


# Runs of whitespace inside a turn, collapsed for the indexed view below.
_WS = re.compile(r"[\s\u3000]+")


def render_indexed(turns: list[Turn]) -> str:
    """One line per turn, prefixed with its `Turn.index`.

    The private pipeline's contract with the local model is that the model
    answers with turn numbers and never with transcript text — Python does
    every extraction. That only works if the numbering the model sees is the
    numbering `parse_transcript` produced, so it is printed literally rather
    than left to be counted.

    Whitespace inside a turn is collapsed so that one turn is exactly one
    line: a model asked for "the number at the start of the line" should not
    have to work out which of five lines owns the index. The collapse is
    cosmetic — quotations are cut from `Turn.text`, not from this rendering.
    """
    out = []
    for t in turns:
        body = _WS.sub(" ", _LEADING_TS.sub("", t.text)).strip()
        speaker = t.speaker or ("（前言）" if t.is_preamble else "未知講者")
        out.append(f"#{t.index} [{t.stamp}] {speaker}：{body}")
    return "\n".join(out) + "\n"


def chunk_file(path: Path, max_chars: int = 14000, overlap_turns: int = 3) -> list[Chunk]:
    return chunk(parse_transcript(Path(path).read_text(errors="ignore")),
                 max_chars, overlap_turns)


# --------------------------------------------------------------------------- #
# CLI — sanity-check a split before spending model time on it
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Split a transcript into agent-sized chunks.")
    ap.add_argument("transcript")
    ap.add_argument("--max-chars", type=int, default=14000)
    ap.add_argument("--overlap", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="machine-readable plan")
    ap.add_argument("--show", type=int, default=None, metavar="N", help="print chunk N and exit")
    args = ap.parse_args()

    path = Path(args.transcript).expanduser()
    if not path.exists():
        print(f"[chunker] no such file: {path}", file=sys.stderr)
        return 2
    turns = parse_transcript(path.read_text(errors="ignore"))
    chunks = chunk(turns, args.max_chars, args.overlap)

    if args.show is not None:
        if not 0 <= args.show < len(chunks):
            print(f"[chunker] chunk {args.show} out of range (0..{len(chunks)-1})", file=sys.stderr)
            return 2
        print(render(chunks[args.show]))
        return 0

    if args.json:
        print(json.dumps({
            "file": str(path), "turns": len(turns),
            "chars": sum(t.chars for t in turns),
            "max_chars": args.max_chars, "overlap_turns": args.overlap,
            "chunks": [c.to_dict() for c in chunks],
        }, ensure_ascii=False, indent=2))
        return 0

    total = sum(t.chars for t in turns)
    print(f"{path.name}: {len(turns)} turns, {total:,} chars "
          f"-> {len(chunks)} chunks (max {args.max_chars:,}, overlap {args.overlap})")
    print(f"{'#':>3}  {'time range':<21} {'turns':>6} {'ovl':>4} {'chars':>8} {'own':>8}")
    print("-" * 56)
    for c in chunks:
        print(f"{c.index:>3}  {c.stamp_start} – {c.stamp_end:<9} "
              f"{len(c.own_turns):>6} {c.overlap_count:>4} {c.char_count:>8,} {c.own_chars:>8,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
