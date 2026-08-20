#!/usr/bin/env python
"""
The private-mode note pipeline: S2–S5 of docs/private-pipeline-redesign.md.

Private mode used to hand a 27B local model the same job a cloud agent gets —
here is a task brief, here is a 20,000 character transcript, decide what
matters and write it. That is long-range autonomous planning, which is the one
thing models this size are worst at, and it measured exactly as badly as that
suggests: 2,572 seconds for a 1,272 character note, 0.061 of its source, every
topic marked 待定.

The premise here is that the cloud model's advantage — one long coherent pass —
cannot be replicated, but it can be *routed around*, by cutting the work until
no single step needs it:

    S2  map     one chunk, one strict schema  -> which turns discuss what
    S3  reduce  pure Python                   -> merge, cluster, dedupe, quote
    S4  map     one topic, one strict schema  -> the prose for one section
    S5  emit    pure Python                   -> skeleton, tables, assembly
    S6  gate    pure Python (note_verify)     -> and rewrite only what failed

Coverage stops depending on the model remembering how many topics there were —
Python counts them. Structure stops depending on it remembering the skeleton —
Python emits it. And quotation stops depending on it copying carefully:

**The model returns turn numbers. It never returns transcript text.**

That last one is the whole design in one line. Asked for a quotation the model
returned 「就這樣定了.」 for 「就這樣定了。」 — a full-width period quietly
turned into an ASCII one. A model allowed to restate the source will edit it,
and an edited quote is a fabricated quote. Here every quotation is cut by
`quote_for()` out of the same `transcript_clean.md` the verifier checks
against, so fabrication is not discouraged, it is unreachable.
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chunker  # noqa: E402
import schemas  # noqa: E402
from chunker import Chunk, Turn  # noqa: E402
from config import CONFIG, ROOT  # noqa: E402

SPEC_PATH = ROOT / "templates" / "NOTE_SPECS.md"
T_EVIDENCE = ROOT / "templates" / "EVIDENCE_TASK.md"
T_SECTION = ROOT / "templates" / "SECTION_TASK.md"

EVIDENCE_SYSTEM = (
    "你是逐字稿證據抽取器。你只回傳 JSON，欄位固定。"
    "指出某句話時一律回傳該句的發言編號（整數），"
    "**任何欄位都不可以貼上逐字稿原文**——引文由程式依編號擷取。"
)
SECTION_SYSTEM = (
    "你是會議記錄撰寫員，一次只寫一個議題的內容。你只回傳 JSON，欄位固定。"
    "引文由程式依 `quote_turns` 的編號擷取，**你不要自己打出逐字稿的句子**。"
)

# One topic's evidence pack, in source characters. Past this the model is
# reading rather than writing, and section quality falls off; an over-long
# topic is split into consecutive parts instead (`split_oversized`).
TOPIC_MAX_CHARS = 3000

# Two labels are the same topic if their character bigrams overlap this much.
# A topic cut by a chunk boundary gets near-identical labels from both readers
# (「付款條件」/「付款條件討論」 scores 0.6); two genuinely different topics
# score far below — see tests/test_private_pipeline.py.
LABEL_SIMILARITY = 0.5


# --------------------------------------------------------------------------- #
# document skeletons (S5)
# --------------------------------------------------------------------------- #
# Which sections each deliverable carries, in order. Section bodies are all
# rendered from the same merged evidence — what differs per reader is which
# parts are shown, not what is true.
SKELETONS: dict[str, list[str]] = {
    "note_general": ["highlights", "topics", "decisions", "actions", "open", "next"],
    "note_client": ["highlights", "topics", "decisions", "actions", "open", "next"],
    "note_self": ["highlights", "topics", "decisions", "actions", "numbers", "open", "next"],
    "note_partner": ["highlights", "topics", "decisions", "actions", "open", "next"],
    "note_interview": ["highlights", "topics", "quotes", "open", "next"],
}
HEADINGS = {
    "highlights": "本次會議重點",
    "topics": "議題討論",
    "decisions": "決議事項",
    "actions": "待辦事項",
    "numbers": "關鍵數字",
    "quotes": "重點語錄",
    "open": "未解決事項與待確認",
    "next": "下次會議",
}
CN_NUM = "〇一二三四五六七八九"


def cn_index(n: int) -> str:
    """1 -> 一, 12 -> 十二. Section numbering, so it never needs to reach 100."""
    if n < 10:
        return CN_NUM[n]
    if n < 20:
        return "十" + (CN_NUM[n % 10] if n % 10 else "")
    return CN_NUM[n // 10] + "十" + (CN_NUM[n % 10] if n % 10 else "")


# --------------------------------------------------------------------------- #
# S3 helpers — deterministic, model-free, unit-testable
# --------------------------------------------------------------------------- #
_NOISE = re.compile(r"[\s　。，、；：！？「」『』（）()\[\]【】·・.,!?;:'\"-]+")


def _normalize(label: str) -> str:
    return _NOISE.sub("", str(label or "")).lower()


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def label_similarity(a: str, b: str) -> float:
    """Jaccard over character bigrams of the normalised labels.

    Deliberately not embeddings. The machine has
    `text-embedding-nomic-embed-text-v1.5` sitting right there, but a network
    call inside the merge would make the merge non-deterministic and
    unavailable offline, and the job it does here — spotting that a topic
    continued across a chunk boundary — is a job for string overlap.
    """
    ga, gb = _bigrams(_normalize(a)), _bigrams(_normalize(b))
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def roster_of(meta: dict, res: dict | None = None) -> list[str]:
    """Canonical names: the participants the user typed, plus confirmed labels."""
    names: list[str] = []
    raw = str((meta or {}).get("participants") or "")
    for part in re.split(r"[、,，/／\s]+", raw):
        part = part.strip()
        if part and part not in names:
            names.append(part)
    for value in ((res or {}).get("speaker_map") or {}).values():
        value = str(value).strip()
        if value and value not in names:
            names.append(value)
    return names


def normalize_speaker(name: str, roster: list[str], turn: Turn | None) -> tuple[str, bool]:
    """(name, matched). The turn's own label wins; the model's name is a fallback.

    Attribution is the one error a reader cannot catch — a real name on the
    wrong sentence reads exactly like a fact. So the transcript's own speaker
    label is preferred over anything the model typed, and a model-supplied name
    that matches nothing in the roster is kept but flagged rather than quietly
    corrected into someone who was in the room.
    """
    if turn is not None and turn.speaker:
        return turn.speaker, True
    name = str(name or "").strip()
    if not name:
        return "未知講者", False
    for known in roster:
        if known and (known == name or known in name or name in known):
            return known, True
    return name, False


def squash(text: str) -> str:
    """Collapse whitespace runs. The one normalisation quotes are compared under.

    A turn can span several lines, and a blockquote cannot, so rendering has to
    join them. Verification therefore compares squashed against squashed —
    which still catches every edit that matters: 「就這樣定了。」 turning into
    「就這樣定了.」 fails here exactly as loudly as an invented sentence does.
    """
    return re.sub(r"[\s\u3000]+", " ", str(text or "")).strip()


# A quotation should show which sentence the conclusion came from, not replay
# the turn. Real turns in this archive run 377–853 characters, and quoting them
# whole both buries the point and inflates the output/source ratio the eval
# measures — a note can look thorough purely by transcribing.
QUOTE_MAX_CHARS = 160

# Sentence boundaries in Chinese, keeping the delimiter with its sentence so a
# rejoined run is still a byte-exact substring of the turn.
_SENTENCE = re.compile(r"[^。！？!?]*[。！？!?]|[^。！？!?]+")


def excerpt(text: str, focus: str = "", max_chars: int = QUOTE_MAX_CHARS) -> str:
    """The most relevant *contiguous* run of sentences that fits.

    Contiguous is the whole trick: any window of consecutive sentences is
    still an exact substring of the turn, so trimming a quotation cannot
    change it. Which window is chosen is decided by overlap with `focus` —
    the model's own paraphrase of the point being made — so the model steers
    the selection without ever handing back a character of transcript.
    """
    text = squash(text)
    if len(text) <= max_chars:
        return text
    sentences = [s for s in _SENTENCE.findall(text) if s.strip()]
    if not sentences:
        return text[:max_chars]

    best, best_key = sentences[0][:max_chars], (-1.0, 0, 0)
    for start in range(len(sentences)):
        window = ""
        for end in range(start, len(sentences)):
            candidate = window + sentences[end]
            if len(candidate) > max_chars:
                break
            window = candidate
            key = (label_similarity(focus, window) if focus else 0.0,
                   len(window), -start)
            if key > best_key:
                best, best_key = window, key
    return best.strip()


def quote_for(turn: Turn, *, with_stamp: bool = False, focus: str = "") -> dict:
    """One quotation, cut from the transcript by index rather than retyped."""
    text = excerpt(chunker._LEADING_TS.sub("", turn.text), focus)
    return {
        "turn": turn.index,
        "stamp": turn.stamp,
        "speaker": turn.speaker or "未知講者",
        "text": text,
        "with_stamp": with_stamp,
    }


def render_quote(quote: dict) -> str:
    """`> 王總：「原文」`, or with a timestamp where the spec allows one.

    NOTE_SPECS.md forbids timestamps in every deliverable except the transcript
    and an interview's 重點語錄, and `agent_note.scrub_note` enforces that
    after the fact. Emitting them everywhere and having them stripped again
    would work, but it would also mean the file on disk never matches what was
    written, so the rule is applied here instead.
    """
    lead = f"[{quote['stamp']}] " if quote.get("with_stamp") else ""
    return f"> {lead}{quote['speaker']}：「{quote['text']}」"


# --------------------------------------------------------------------------- #
# S3 — merge (pure Python, zero model calls)
# --------------------------------------------------------------------------- #
def merge_evidence(parts: list[tuple[Chunk, dict | None]], turns: list[Turn],
                   *, roster: list[str] | None = None,
                   max_topic_chars: int = TOPIC_MAX_CHARS) -> dict:
    """Fold per-chunk evidence into one ordered, deduplicated, quoted document.

    Every rule here is deterministic on purpose: given the same chunk replies
    this returns the same evidence, so a bad note can be traced to a bad
    extraction rather than to the merge having had a different day.

    Anything referring to a turn the chunk did not own is dropped, not
    reconciled. `Chunk.overlap_count` says exactly which leading turns were
    shown as context, so double-counting the overlap is a solved problem and
    nothing here is allowed to guess at it.
    """
    roster = roster or []
    by_index = {t.index: t for t in turns}
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    topics: list[dict] = []
    numbers: list[dict] = []
    actions: list[dict] = []
    unclear: list[dict] = []
    seen_numbers: set[tuple[int, str]] = set()

    for chunk, data in parts:
        if not data:
            drop("chunk_failed")
            continue
        own = {t.index for t in chunk.own_turns}

        def valid(raw) -> Turn | None:
            try:
                idx = int(raw)
            except (TypeError, ValueError):
                return None
            if idx not in own:
                return None
            return by_index.get(idx)

        local: dict[str, dict] = {}
        for raw_topic in data.get("topics") or []:
            kept = sorted({t.index for t in
                           (valid(x) for x in raw_topic.get("turns") or []) if t})
            if not kept:
                drop("topic_no_valid_turns")
                continue
            status_turn = valid(raw_topic.get("status_turn"))
            topic = {
                "label": str(raw_topic.get("label") or "").strip() or "未命名議題",
                "turns": kept,
                "status": raw_topic.get("status") if raw_topic.get("status") in schemas.STATUS_VALUES else "pending",
                "status_turn": status_turn.index if status_turn else None,
                "speakers": [],
                "points": [],
                "numbers": [],
                "chunk": chunk.index,
            }
            topics.append(topic)
            local[str(raw_topic.get("topic_id") or "")] = topic

        for raw_point in data.get("points") or []:
            turn = valid(raw_point.get("turn"))
            if turn is None:
                drop("point_out_of_range")
                continue
            topic = local.get(str(raw_point.get("topic_id") or ""))
            if topic is None:
                topic = next((t for t in topics
                              if t["chunk"] == chunk.index and turn.index in t["turns"]), None)
            if topic is None:
                drop("point_no_topic")
                continue
            speaker, matched = normalize_speaker(raw_point.get("speaker"), roster, turn)
            topic["points"].append({
                "turn": turn.index, "speaker": speaker,
                "gist": str(raw_point.get("gist") or "").strip(),
                "speaker_verified": matched,
            })

        for raw_number in data.get("numbers") or []:
            turn = valid(raw_number.get("turn"))
            literal = str(raw_number.get("literal") or "").strip()
            if turn is None or not literal:
                drop("number_out_of_range")
                continue
            # `literal` is the one field allowed anywhere near the source text,
            # so it is the one field checked against it here. Measured on a
            # real run: the model reported 「50%」 and 「一百五十萬」 for turns
            # whose text contained neither. Those must not reach the document,
            # and "the verifier will mention it" is not the same as not
            # shipping it — so they are dropped at the merge.
            if squash(literal) not in squash(turn.text):
                drop("number_not_in_its_turn")
                continue
            key = (turn.index, literal)
            if key in seen_numbers:
                continue
            seen_numbers.add(key)
            numbers.append({"turn": turn.index, "stamp": turn.stamp, "literal": literal,
                            "means": str(raw_number.get("means") or "").strip()})

        for raw_action in data.get("actions") or []:
            turn = valid(raw_action.get("turn"))
            what = str(raw_action.get("what") or "").strip()
            if turn is None or not what:
                drop("action_out_of_range")
                continue
            owner, _ = normalize_speaker(raw_action.get("owner"), roster, None)
            actions.append({
                "turn": turn.index, "stamp": turn.stamp, "what": what,
                "owner": owner if owner != "未知講者" else "未指派",
                "due": str(raw_action.get("due") or "").strip() or "未定",
            })

        for raw_unclear in data.get("unclear") or []:
            turn = valid(raw_unclear.get("turn"))
            if turn is None:
                drop("unclear_out_of_range")
                continue
            unclear.append({"turn": turn.index, "stamp": turn.stamp,
                            "why": str(raw_unclear.get("why") or "").strip()})

    topics = cluster_topics(topics)
    topics.sort(key=lambda t: min(t["turns"]))
    topics = split_oversized(topics, by_index, max_topic_chars)

    # Numbers belong to whichever topic covers their turn, and are also kept
    # whole so 關鍵數字 and the verifier can see every one of them.
    for number in numbers:
        for topic in topics:
            if number["turn"] in topic["turns"]:
                topic["numbers"].append(number)
                break

    for i, topic in enumerate(topics, 1):
        topic["id"] = f"T{i:02d}"
        topic["points"].sort(key=lambda p: p["turn"])
        topic["speakers"] = sorted({by_index[i].speaker for i in topic["turns"]
                                    if i in by_index and by_index[i].speaker})
        topic["source_chars"] = sum(len(by_index[i].text) for i in topic["turns"] if i in by_index)

    actions.sort(key=lambda a: a["turn"])
    unclear.sort(key=lambda u: u["turn"])
    numbers.sort(key=lambda n: n["turn"])
    return {
        "topics": topics,
        "actions": dedupe_actions(actions),
        "numbers": numbers,
        "unclear": unclear,
        "roster": roster,
        "dropped": dropped,
        "turn_count": len(turns),
    }


def cluster_topics(topics: list[dict]) -> list[dict]:
    """Rejoin a topic the chunk boundary cut in half.

    Only across *adjacent* chunks and only when the turn ranges actually
    adjoin: two similarly-named topics twenty minutes apart are two topics, and
    merging them would silently delete a section of the meeting.
    """
    merged: list[dict] = []
    for topic in sorted(topics, key=lambda t: (t["chunk"], min(t["turns"]))):
        target = None
        for candidate in merged:
            if candidate["chunk"] != topic["chunk"] - 1:
                continue
            if min(topic["turns"]) > max(candidate["turns"]) + 1:
                continue
            if label_similarity(candidate["label"], topic["label"]) < LABEL_SIMILARITY:
                continue
            target = candidate
            break
        if target is None:
            merged.append(topic)
            continue
        target["turns"] = sorted(set(target["turns"]) | set(topic["turns"]))
        target["points"] += topic["points"]
        target["chunk"] = topic["chunk"]
        # A later chunk saw the end of the discussion, so its verdict is the
        # one that counts — a topic settled at the end was not "pending".
        if topic["status"] != "pending" or target["status"] == "pending":
            target["status"] = topic["status"]
            target["status_turn"] = topic["status_turn"]
        if len(topic["label"]) > len(target["label"]):
            target["label"] = topic["label"]
    return merged


def split_oversized(topics: list[dict], by_index: dict[int, Turn],
                    max_chars: int) -> list[dict]:
    """Cut a topic whose evidence pack would not fit in one focused prompt.

    A 6,000-character topic makes S4 a reading task again, which is the
    failure this whole pipeline exists to avoid. Splitting keeps every section
    small enough to write well, and the parts stay consecutive so the meeting
    still reads in order.
    """
    out: list[dict] = []
    for topic in topics:
        size = sum(len(by_index[i].text) for i in topic["turns"] if i in by_index)
        if size <= max_chars or len(topic["turns"]) < 2:
            out.append(topic)
            continue
        parts = max(2, -(-size // max_chars))
        per = -(-len(topic["turns"]) // parts)
        slices = [topic["turns"][i:i + per] for i in range(0, len(topic["turns"]), per)]
        slices = [s for s in slices if s]
        for n, chunk_turns in enumerate(slices, 1):
            piece = dict(topic)
            piece["turns"] = chunk_turns
            piece["label"] = topic["label"] if n == 1 else f"{topic['label']}（續 {n}）"
            piece["points"] = [p for p in topic["points"] if p["turn"] in set(chunk_turns)]
            piece["numbers"] = []
            # Only the part that actually contains the deciding turn may claim
            # the verdict; the rest are still under discussion.
            if topic["status_turn"] not in chunk_turns:
                piece["status"], piece["status_turn"] = "pending", None
            out.append(piece)
    return out


def dedupe_actions(actions: list[dict]) -> list[dict]:
    """Same turn, same wording — the overlap already handled, this is repetition."""
    seen: set[tuple[int, str]] = set()
    out = []
    for action in actions:
        key = (action["turn"], _normalize(action["what"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(action)
    return out


# --------------------------------------------------------------------------- #
# spec fragment (S4 input)
# --------------------------------------------------------------------------- #
def spec_fragment(meeting_type: str, spec_path: Path = SPEC_PATH) -> str:
    """The part of NOTE_SPECS.md that governs this document type, plus the bans.

    The whole file is 200 lines of delivery rules, PDF flags and four document
    types. Handing all of it to a model writing one section of one document is
    how a 27B model ends up writing about `make_pdf.py`.
    """
    try:
        text = spec_path.read_text()
    except OSError:
        return ""
    blocks: dict[str, list[str]] = {}
    current = ""
    fenced = False
    for line in text.split("\n"):
        # The skeletons in this file are markdown inside fences, and they
        # contain their own `## 一、本次會議重點` headings. Reading those as
        # section boundaries truncates every spec block at its own example.
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            current = line[3:].strip()
            blocks[current] = [line]
        elif current:
            blocks[current].append(line)
    wanted = [k for k in blocks if k.startswith(f"{meeting_type} —")]
    wanted += [k for k in blocks if k.startswith("通用禁則")]
    out = "\n".join("\n".join(blocks[k]).strip() for k in wanted).strip()
    return out or text


# --------------------------------------------------------------------------- #
# rendering (S5, pure Python)
# --------------------------------------------------------------------------- #
def render_topic(section: dict, topic: dict, index: int, by_index: dict[int, Turn],
                 *, with_stamp: bool = False) -> str:
    """One `###` section: the model's prose, the Python skeleton, real quotes."""
    heading = str(section.get("heading") or topic["label"]).strip() or topic["label"]
    lines = [f"### {index}. {heading}", ""]

    rows = []
    used: set[int] = set()
    for item in section.get("discussion") or []:
        turn = by_index.get(int(item.get("turn", -1))) if str(item.get("turn", "")).lstrip("-").isdigit() else None
        point = str(item.get("point") or "").strip()
        if not point:
            continue
        speaker = turn.speaker if (turn is not None and turn.speaker) else ""
        rows.append(f"  - {speaker}：{point}" if speaker else f"  - {point}")
        if turn is not None:
            used.add(turn.index)

    # NOTE_SPECS.md wants at least three attributed points per topic. Rather
    # than send the section back for being thin, top it up from the evidence
    # already extracted for those turns — grounded material the model produced,
    # just in the earlier stage.
    if len(rows) < 3:
        for point in topic["points"]:
            if len(rows) >= 3:
                break
            if point["turn"] in used or not point["gist"]:
                continue
            rows.append(f"  - {point['speaker']}：{point['gist']}")
            used.add(point["turn"])
    if not rows:
        rows = ["  - （音檔不清楚，本議題無法整理出具體討論內容）"]
    elif len(rows) == 1 and len(topic["turns"]) <= 2:
        rows.append("  - 本議題僅一次發言。")

    lines.append("- **討論內容**：")
    lines += rows

    status = section.get("status") if section.get("status") in schemas.STATUS_VALUES else topic["status"]
    basis = str(section.get("basis") or "").strip()
    lines.append(f"- **結論**：{schemas.STATUS_LABEL[status]}"
                 + (f"——{basis}" if basis else ""))

    # What the section says it is about, used to pick which sentences of a long
    # turn actually get quoted.
    focus = " ".join([str(section.get("basis") or "")]
                     + [str(i.get("point") or "") for i in section.get("discussion") or []])
    quotes = []
    for raw in (section.get("quote_turns") or [])[:2]:
        turn = by_index.get(int(raw)) if isinstance(raw, (int, float)) else None
        if turn is not None and turn.index in set(topic["turns"]) and turn.text.strip():
            quotes.append(quote_for(turn, with_stamp=with_stamp, focus=focus))
    if not quotes:
        # Every topic needs one quotation. Falling back to the longest turn in
        # the topic is arbitrary but honest: it is real text from this topic.
        candidates = [by_index[i] for i in topic["turns"] if i in by_index and by_index[i].text.strip()]
        if candidates:
            quotes = [quote_for(max(candidates, key=lambda t: len(t.text)),
                                with_stamp=with_stamp, focus=focus)]
    if quotes:
        lines.append("")
        lines += [render_quote(q) for q in quotes]
    lines.append("")
    return "\n".join(lines)


def render_document(stem: str, meta: dict, evidence: dict, sections: list[dict],
                    highlights: list[str], by_index: dict[int, Turn]) -> str:
    """Assemble one deliverable. The skeleton can never be incomplete: it is
    generated here, not remembered by a model."""
    title = str(meta.get("title") or "未命名會議")
    with_stamp = stem == "note_interview"
    label = {"note_general": "會議記錄", "note_client": "會議記錄",
             "note_self": "內部覆盤", "note_partner": "夥伴補課摘要",
             "note_interview": "訪談紀要"}.get(stem, "會議記錄")
    out = [f"# {title} {label}", ""]

    topics = evidence["topics"]
    section_by_id = {s["topic_id"]: s for s in sections}
    n = 0
    for key in SKELETONS.get(stem, SKELETONS["note_general"]):
        n += 1
        out.append(f"## {cn_index(n)}、{HEADINGS[key]}")
        out.append("")

        if key == "highlights":
            out += [f"- {h}" for h in highlights] or ["- （本次會議未整理出重點）"]

        elif key == "topics":
            for i, topic in enumerate(topics, 1):
                section = section_by_id.get(topic["id"]) or {}
                out.append(render_topic(section, topic, i, by_index, with_stamp=with_stamp))
            if not topics:
                out.append("（逐字稿中未辨識出可整理的議題。）")

        elif key == "decisions":
            decided = [t for t in topics if t["status"] == "decided"]
            out += ["| # | 決議 | 依據／理由 |", "|---|---|---|"]
            for i, topic in enumerate(decided, 1):
                section = section_by_id.get(topic["id"]) or {}
                head = str(section.get("heading") or topic["label"]).strip()
                basis = str(section.get("basis") or "").strip() or "會中明確拍板"
                out.append(f"| {i} | {head} | {_cell(basis)} |")
            if not decided:
                out.append("| — | 本次會議未產生明確決議 | 所有議題均未拍板 |")

        elif key == "actions":
            out += ["| # | 事項 | 負責人 | 期限 | 狀態 |", "|---|---|---|---|---|"]
            for i, action in enumerate(evidence["actions"], 1):
                out.append(f"| {i} | {_cell(action['what'])} | {action['owner']} | "
                           f"{action['due']} | 未開始 |")
            if not evidence["actions"]:
                out.append("| — | 本次會議未產生待辦事項 | — | — | — |")

        elif key == "numbers":
            for number in evidence["numbers"]:
                out.append(f"- **{number['literal']}** — {number['means'] or '（未說明用途）'}")
            if not evidence["numbers"]:
                out.append("- 未討論")

        elif key == "quotes":
            for topic in topics:
                section = section_by_id.get(topic["id"]) or {}
                focus = str(section.get("basis") or "")
                for raw in (section.get("quote_turns") or [])[:2]:
                    turn = by_index.get(int(raw)) if isinstance(raw, (int, float)) else None
                    if turn is not None and turn.text.strip():
                        out.append(render_quote(quote_for(turn, with_stamp=True, focus=focus)))
                        out.append("")
            if not any(line.startswith(">") for line in out):
                out.append("- 未討論")

        elif key == "open":
            pending = [t for t in topics if t["status"] in {"pending", "parked"}]
            for topic in pending:
                section = section_by_id.get(topic["id"]) or {}
                head = str(section.get("heading") or topic["label"]).strip()
                out.append(f"- {head}——{schemas.STATUS_LABEL[topic['status']]}"
                           f"{'；' + _cell(str(section.get('basis') or '')) if section.get('basis') else ''}")
            for item in evidence["unclear"][:5]:
                out.append(f"- （音檔不清楚）{item['why']}")
            if not pending and not evidence["unclear"]:
                out.append("- 本次會議沒有懸而未決的事項。")

        elif key == "next":
            dated = [a for a in evidence["actions"] if a["due"] != "未定"]
            for action in dated[:5]:
                out.append(f"- {action['due']}：{_cell(action['what'])}（{action['owner']}）")
            if not dated:
                out.append("- 未討論")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _cell(text: str) -> str:
    """Markdown table cells cannot contain a raw pipe or newline."""
    return str(text or "").replace("|", "／").replace("\n", " ").strip()


def build_action_items(meta: dict, result: dict, evidence: dict,
                       sections: list[dict]) -> dict:
    """`action_items.json` straight from the merged evidence — no model call.

    Every row here already survived the turn-range check in S3, so this is
    transcription of verified data rather than another chance to invent.
    """
    section_by_id = {s["topic_id"]: s for s in sections}
    decisions = []
    for topic in evidence["topics"]:
        if topic["status"] != "decided":
            continue
        section = section_by_id.get(topic["id"]) or {}
        decisions.append({
            "decision": str(section.get("heading") or topic["label"]).strip(),
            "basis": str(section.get("basis") or "").strip() or "會中明確拍板",
        })
    return {
        "meeting": {
            "title": str(meta.get("title") or ""),
            "client": str(meta.get("client") or ""),
            "date": str(meta.get("date") or ""),
            "participants": roster_of(meta),
            "meeting_type": str(meta.get("meeting_type") or "general"),
            "pipeline": "evidence",
        },
        "decisions": decisions,
        "actions": [{"item": a["what"], "responsible": a["owner"],
                     "deadline": a["due"], "status": "未開始"}
                    for a in evidence["actions"]],
        "risks": [{"risk": u["why"], "source": f"turn {u['turn']}"}
                  for u in evidence["unclear"]],
        "numbers": [n["literal"] for n in evidence["numbers"]],
    }


def build_index(stems: list[str], meta: dict, evidence: dict) -> str:
    kinds = {"transcript_clean": ("定稿逐字稿（已校訂）", "內部；經確認後可供客戶查核"),
             "note_general": ("會議記錄：重點、議題討論、決議、待辦", "全體與會者"),
             "note_client": ("會議記錄（客戶版）", "客戶"),
             "note_self": ("內部覆盤", "僅限我方——嚴禁外傳"),
             "note_partner": ("夥伴補課摘要", "我方未出席夥伴"),
             "note_interview": ("訪談紀要（含重點語錄）", "研究用途")}
    out = [f"# 檔案清單 — {meta.get('title') or '未命名會議'}", "",
           f"- **對象**：{meta.get('client') or '—'}",
           f"- **日期**：{meta.get('date') or '—'}",
           f"- **議題數**：{len(evidence['topics'])}",
           f"- **待辦數**：{len(evidence['actions'])}", "",
           "## 交付文件", "", "| 檔案 | 內容 | 讀者 |", "|---|---|---|"]
    for stem in stems:
        desc, reader = kinds.get(stem, ("會議文件", "內部"))
        out.append(f"| `{stem}.md` | {desc} | {reader} |")
    out.append("| `action_items.json` | 決議／待辦／風險／關鍵數字（結構化） | 內部 |")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# model-facing stages
# --------------------------------------------------------------------------- #
def _call(job, prompt: str, schema: dict, system: str, label: str) -> dict | None:
    from agent_note import describe_api_error, invoke_backend, last_api_error

    rc, elapsed, text = invoke_backend(
        job.backend, job.binary, prompt, job.model, job.log, job.timeout,
        job.acfg, schema=schema, system=system)
    if not text:
        print(f"[private] {label}: FAILED ({elapsed}s) — {describe_api_error(last_api_error())}")
        return None
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[private] {label}: unparseable JSON after {elapsed}s: {exc}")
        return None
    problems = schemas.validate(data, schema)
    if problems:
        # Strict decoding should make this unreachable; if a backend ever
        # ignores the schema we would rather see the reason than the wreckage.
        print(f"[private] {label}: schema mismatch: {problems[:3]}")
        return None
    print(f"[private] {label}: ok ({elapsed}s, rc={rc})")
    return data


def _parallel(job, items, fn):
    workers = max(1, int(job.acfg.get("max_parallel") or 3))
    if len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def _fill(template: Path, subs: dict) -> str:
    text = template.read_text()
    for key, value in subs.items():
        text = text.replace("{{" + key + "}}", "" if value is None else str(value))
    return text


def extract_evidence(job, chunks: list[Chunk], meta: dict,
                     user_context: str = "") -> list[tuple[Chunk, dict | None]]:
    """S2. One prompt per chunk, one strict schema, all of them in parallel."""
    total = len(chunks)
    print(f"[private] S2 evidence: {total} chunk(s), "
          f"{job.acfg.get('max_parallel', 3)} in parallel")

    def run_one(chunk: Chunk):
        prompt = _fill(T_EVIDENCE, {
            "TOTAL": total, "INDEX": chunk.index + 1,
            "TITLE": meta.get("title") or "未命名會議",
            "CLIENT": meta.get("client") or "—",
            "DATE": meta.get("date") or "—",
            "PARTICIPANTS": meta.get("participants") or "未提供",
            "USER_CONTEXT": user_context,
            "CONTEXT": (chunker.render_indexed(chunk.overlap_turns)
                        if chunk.overlap_turns else "（本段為開頭，無上文）"),
            "BODY": chunker.render_indexed(chunk.own_turns),
        })
        data = _call(job, prompt, schemas.EVIDENCE, EVIDENCE_SYSTEM,
                     f"S2 {chunk.index + 1}/{total}")
        (job.work / f"evidence_raw_{chunk.index:02d}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) if data else "null")
        return chunk, data

    return _parallel(job, chunks, run_one)


def write_sections(job, evidence: dict, meta: dict, by_index: dict[int, Turn],
                   user_context: str = "", only: set[str] | None = None) -> list[dict]:
    """S4. One prompt per topic, and nothing in it about the rest of the meeting."""
    topics = [t for t in evidence["topics"] if only is None or t["id"] in only]
    total = len(evidence["topics"])
    spec = spec_fragment(str(meta.get("meeting_type") or "general"))
    print(f"[private] S4 sections: {len(topics)} topic(s) of {total}")

    def run_one(topic: dict):
        body = chunker.render_indexed([by_index[i] for i in topic["turns"] if i in by_index])
        points = "\n".join(f"- #{p['turn']} {p['speaker']}：{p['gist']}"
                           for p in topic["points"]) or "（上一階段未抽出重點）"
        numbers = ""
        if topic["numbers"]:
            numbers = "## 本議題出現的數字（照原文，不要改寫）\n\n" + "\n".join(
                f"- `{n['literal']}` — {n['means']}" for n in topic["numbers"])
        prompt = _fill(T_SECTION, {
            "TOTAL": total, "INDEX": evidence["topics"].index(topic) + 1,
            "TITLE": meta.get("title") or "未命名會議",
            "CLIENT": meta.get("client") or "—",
            "DATE": meta.get("date") or "—",
            "PARTICIPANTS": meta.get("participants") or "未提供",
            "USER_CONTEXT": user_context,
            "LABEL": topic["label"], "STATUS": schemas.STATUS_LABEL[topic["status"]],
            "SPEC": spec, "BODY": body, "POINTS": points, "NUMBERS": numbers,
        })
        data = _call(job, prompt, schemas.SECTION, SECTION_SYSTEM, f"S4 {topic['id']}")
        if data is None:
            # A failed section still gets a section: the evidence for it is
            # already extracted, so the topic appears with its points rather
            # than vanishing from the document.
            data = {"heading": topic["label"], "discussion": [], "basis": "",
                    "status": topic["status"], "quote_turns": []}
        data["topic_id"] = topic["id"]
        (job.work / f"section_{topic['id']}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2))
        return data

    return _parallel(job, topics, run_one)


def write_overview(job, evidence: dict, sections: list[dict], meta: dict) -> list[str]:
    """The three-sentence opener, written from the evidence rather than the source."""
    section_by_id = {s["topic_id"]: s for s in sections}
    rows = []
    for topic in evidence["topics"]:
        section = section_by_id.get(topic["id"]) or {}
        rows.append(f"- {str(section.get('heading') or topic['label'])}"
                    f"（{schemas.STATUS_LABEL[section.get('status') or topic['status']]}）"
                    f"：{str(section.get('basis') or '')}")
    prompt = "\n".join([
        f"這是「{meta.get('title') or '未命名會議'}」的議題清單與結論。",
        "",
        "\n".join(rows) or "（沒有議題）",
        "",
        "請寫「本次會議重點」：**最多 3 句**，每句一則，只寫結果不寫過程。",
        "不要寫「本次會議討論了……」這種開場白，直接寫結論。",
        "不要提到任何上面沒有的內容。回傳 `highlights` 陣列。",
    ])
    data = _call(job, prompt, schemas.OVERVIEW, SECTION_SYSTEM, "S5 overview")
    if not data:
        # Deriving it from the decided topics is worse prose but never wrong.
        return [f"{str((section_by_id.get(t['id']) or {}).get('heading') or t['label'])}"
                f"：{schemas.STATUS_LABEL[t['status']]}"
                for t in evidence["topics"][:3]]
    return [str(h).strip() for h in (data.get("highlights") or [])[:3] if str(h).strip()]


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run(job, res: dict, source: Path) -> dict:
    """S2–S6 for one job. Writes the deliverables into `job.dir`."""
    import note_verify   # imported here: note_verify imports this module back

    started = time.time()
    meta = dict(job.meta)
    meta.setdefault("title", job.title)
    meta["meeting_type"] = job.plan["meeting_type"]
    turns = [t for t in chunker.parse_transcript(source.read_text(errors="ignore"))
             if not t.is_preamble]
    if not turns:
        raise RuntimeError("[private] 逐字稿解析後沒有任何發言段落")
    by_index = {t.index: t for t in turns}
    source_chars = sum(len(t.text) for t in turns)

    chunks = chunker.chunk(turns,
                           int(job.acfg.get("chunk_chars") or 14000),
                           int(job.acfg.get("chunk_overlap_turns") or 3))
    print(f"[private] evidence pipeline: {len(turns)} turns, {source_chars:,} chars, "
          f"{len(chunks)} chunk(s)")

    parts = extract_evidence(job, chunks, meta, job.user_context)
    evidence = merge_evidence(parts, turns, roster=roster_of(meta, res))
    (job.work / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2))
    print(f"[private] S3 merged: {len(evidence['topics'])} topic(s), "
          f"{len(evidence['actions'])} action(s), {len(evidence['numbers'])} number(s), "
          f"dropped={evidence['dropped'] or '{}'}")
    if not evidence["topics"]:
        raise RuntimeError("[private] 證據抽取後沒有任何議題——請檢查 "
                           f"{job.work}/evidence_raw_*.json")

    sections = write_sections(job, evidence, meta, by_index, job.user_context)
    highlights = write_overview(job, evidence, sections, meta)
    stems = [s for s in job.plan["stems"] if s != "transcript_clean"]

    def emit() -> None:
        for stem in stems:
            (job.dir / f"{stem}.md").write_text(
                render_document(stem, meta, evidence, sections, highlights, by_index))
        (job.dir / "action_items.json").write_text(json.dumps(
            build_action_items(meta, job.result, evidence, sections),
            ensure_ascii=False, indent=2))
        (job.dir / "INDEX.md").write_text(build_index(stems, meta, evidence))

    emit()

    # S6. Only the sections that failed are rewritten — one topic missing from
    # the document is no reason to spend another six minutes on the other
    # eleven that came out fine.
    findings = note_verify.verify(job.dir, evidence, turns, stems=stems)
    note_verify.report(findings)
    retry = {f.topic_id for f in findings if f.rerun and f.topic_id}
    if retry:
        print(f"[private] S6 rewriting {len(retry)} section(s): {sorted(retry)}")
        redone = {s["topic_id"]: s for s in
                  write_sections(job, evidence, meta, by_index, job.user_context, only=retry)}
        sections = [redone.get(s["topic_id"], s) for s in sections]
        emit()
        findings = note_verify.verify(job.dir, evidence, turns, stems=stems)
        note_verify.report(findings)

    fixed = note_verify.autofix(job.dir, stems)
    if any(fixed.values()):
        print(f"[private] S6 autofix: {fixed}")

    elapsed = time.time() - started
    produced = sum(len((job.dir / f"{s}.md").read_text()) for s in stems
                   if (job.dir / f"{s}.md").exists())
    report = {
        "files": [f"{s}.md" for s in stems] + ["action_items.json", "INDEX.md"],
        "pipeline": "evidence",
        "topics": len(evidence["topics"]),
        "actions": len(evidence["actions"]),
        "numbers": len(evidence["numbers"]),
        "source_chars": source_chars,
        "produced_chars": produced,
        "ratio": round(produced / source_chars, 3) if source_chars else 0.0,
        "elapsed_sec": round(elapsed, 1),
        "dropped": evidence["dropped"],
        "autofixed": fixed,
        "findings": [f.as_dict() for f in findings],
        "uncertain": [f"turn {u['turn']}：{u['why']}" for u in evidence["unclear"]][:10],
        "corrections": len(res.get("replacements") or []),
        "notes": "由 evidence 管線產出：引文與數字皆由 Python 依 turn 編號自逐字稿擷取並驗證。",
    }
    (job.dir / "agent_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
    print(f"[private] done in {elapsed:.0f}s — {produced:,} chars from {source_chars:,} "
          f"(ratio {report['ratio']})")
    return report
