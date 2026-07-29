#!/usr/bin/env python
"""
Two-stage note production: scan the transcript, ask the user, then write.

The single-pass writer (`agent_note.py`) is fine for a twenty-minute meeting.
It fails in two specific ways on real work, both observed:

  1. A two-hour transcript is ~127k characters. One prompt cannot hold it, and
     the failure is silent — the model writes a confident note about the first
     twenty minutes and the exit code is 0.
  2. The things that make a note wrong are things only the user knows. Speaker
     diarisation on a conference call split three people into 112 clusters; ASR
     rendered one surname seven different ways. A model asked to guess will
     guess, and the guess ends up stated as fact in a client document.

So the work is split at the point where those two problems live:

    stage 1 (scan)   chunk -> parallel readers -> draft + questions.json
                     -> park in `awaiting_answers`, notify
    -- the user answers in the web UI, by clicking --
    stage 2 (write)  apply answers deterministically -> transcript_clean.md
                     -> write notes (single pass, or map-reduce if long)
                     -> convert, deliver

`--stage auto` runs both back to back using every `best_guess`, for when
nobody is going to answer. That is a worse document, and it says so in the
document.

Division of labour, unchanged from agent_note.py: the CLI agent produces
judgement (`.json`, `.md`); Python does everything verifiable (chunking,
find/replace, format conversion, delivery, state). In particular the answers
are applied by `re.sub` here, not by asking a model to remember them.

    bin/review.py <job_dir|latest> --stage scan|write|auto [--deliver]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chunker  # noqa: E402
import jobstate  # noqa: E402
import notify as notifier  # noqa: E402
from agent_note import (  # noqa: E402
    DOC_KINDS,
    FMT_NAME,
    build_plan,
    convert,
    read_json,
    resolve_bin,
)
from agent_note import backend_cmd as _backend_cmd  # noqa: E402
from agent_note import run_agent as _run_agent  # noqa: E402
from config import CONFIG, ROOT  # noqa: E402

# Every stage takes minutes and the only progress signal is these prints, which
# the web UI tails out of logs/<job>-agent.log. Block buffering would hide all
# of it until the process exited, so the log is line-buffered on purpose.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):  # not a real stream (tests, pipes)
    pass

SPEC_PATH = ROOT / "templates" / "NOTE_SPECS.md"
T_SCAN = ROOT / "templates" / "SCAN_TASK.md"
T_PARTIAL = ROOT / "templates" / "PARTIAL_TASK.md"
T_WRITE = ROOT / "templates" / "AGENT_TASK.md"

# Ordering for the question cards. Speaker attribution first: every other
# answer is cosmetic next to putting the wrong name on a quote.
TYPE_RANK = {"speaker": 0, "term": 1, "unclear": 2, "conflict": 3, "undecided": 4}
TYPE_LABEL = {
    "speaker": "講者對應",
    "term": "專有名詞",
    "unclear": "辨識不清",
    "conflict": "前後矛盾",
    "undecided": "未拍板",
}
# The option that means "none of these" — never treated as a real answer.
OPT_OTHER = "以上皆非／我來補充"


# --------------------------------------------------------------------------- #
# tolerant json
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def loads_loose(text: str) -> dict | None:
    """Parse JSON an agent wrote to a file, tolerating the usual damage.

    Agents fence their output in ```json blocks, add a sentence before it, or
    append a trailing note. None of that is worth failing a two-hour job over,
    so: try straight, then defenced, then the outermost balanced {...}.
    Returns None only when there is genuinely no object in there.
    """
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


def load_agent_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return loads_loose(path.read_text(errors="ignore"))


def atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data)
    else:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def hms(x) -> str:
    x = int(float(x or 0))
    return f"{x // 3600:02d}:{(x % 3600) // 60:02d}:{x % 60:02d}"


# --------------------------------------------------------------------------- #
# job context
# --------------------------------------------------------------------------- #
class Job:
    """Everything the stages need to know about one job, resolved once."""

    def __init__(self, job_dir: Path, backend: str, model: str | None,
                 binary: str, timeout: int):
        self.dir = job_dir
        self.meta = read_json(job_dir / "meta.json")
        self.result = read_json(job_dir / "status.json").get("result") or {}
        self.plan = build_plan(self.meta)
        self.backend, self.model, self.binary, self.timeout = backend, model, binary, timeout
        self.work = job_dir / ".review"
        self.work.mkdir(parents=True, exist_ok=True)
        self.log = ROOT / "logs" / f"{job_dir.name}-agent.log"
        self.acfg = CONFIG.get("agent", {})

    # -- fields the prompts interpolate ------------------------------------ #
    def f(self, key: str, default: str = "") -> str:
        return str(self.result.get(key) or self.meta.get(key) or default)

    @property
    def title(self) -> str:
        return self.f("title", "未命名會議")

    @property
    def user_context(self) -> str:
        parts = [(self.meta.get("context") or "").strip()]
        parts.append((read_json(self.dir / "answers.json").get("context") or "").strip())
        body = "\n".join(p for p in parts if p)
        return f"## 使用者提供的背景／專有名詞\n\n{body}\n" if body else ""

    @property
    def speaker_warning(self) -> str:
        """Flag unusable diarisation so prompts stop trusting the labels.

        Empirically the labels are worthless once the cluster count runs well
        past the declared headcount (online meetings, mixed mic quality): 3
        people became 112 clusters on one real job. Below that threshold the
        labels are usually right and second-guessing them costs accuracy.
        """
        got, want = self.result.get("num_speakers"), self.meta.get("num_speakers")
        try:
            got, want = int(got), int(str(want).strip())
        except (TypeError, ValueError):
            return ""
        if got <= max(want * 2, want + 3):
            return ""
        return (
            f"> ⚠️ **聲紋分群不可靠**：使用者填 {want} 位講者，系統卻切出 {got} 群"
            f"（線上會議錄音常見）。逐字稿的講者標籤不可信，請改用發言內容、人稱與角色"
            f"重新判斷是誰在講，並在文件開頭註明「講者對應為推測」。"
        )

    def source_transcript(self) -> Path:
        """Best available transcript: resolved > draft > raw."""
        for name in ("transcript_clean.md", "transcript_draft.md", "transcript.md"):
            p = self.dir / name
            if p.exists():
                return p
        return self.dir / "transcript.md"


# --------------------------------------------------------------------------- #
# agent invocation
# --------------------------------------------------------------------------- #
def fill(template: Path, subs: dict) -> str:
    text = template.read_text()
    for key, val in subs.items():
        text = text.replace("{{" + key + "}}", "" if val is None else str(val))
    return text


def call_agent(job: Job, prompt: str, out_path: Path, label: str,
               retries: int = 1) -> tuple[dict | None, str]:
    """Run the CLI once and read back the file it was told to write.

    Success is defined by the file, never by the exit code — both supported
    CLIs can exit 0 having written nothing (not logged in) and exit non-zero
    having written everything (a tool-permission grumble on the way out).
    A retry gets one more shot at a malformed write before we give up on the
    chunk; the caller degrades rather than failing the job.
    """
    for attempt in range(retries + 1):
        if out_path.exists():
            out_path.unlink()
        cmd = _backend_cmd(job.backend, job.binary, prompt, job.model)
        rc, elapsed = _run_agent(cmd, job.log, job.timeout)
        data = load_agent_json(out_path)
        if data is not None:
            print(f"[review] {label}: ok ({elapsed}s, rc={rc})")
            return data, ""
        why = "no output file" if not out_path.exists() else "unparseable JSON"
        print(f"[review] {label}: {why} (rc={rc}, {elapsed}s)"
              f"{' — retrying' if attempt < retries else ''}")
    return None, why


def in_parallel(job: Job, tasks: list, fn) -> list:
    workers = max(1, int(job.acfg.get("max_parallel") or 3))
    if len(tasks) <= 1:
        return [fn(t) for t in tasks]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, tasks))


# --------------------------------------------------------------------------- #
# stage 1 — scan
# --------------------------------------------------------------------------- #
def stage_scan(job: Job, deliver: bool) -> dict:
    src = job.dir / "transcript.md"
    if not src.exists():
        raise SystemExit(f"[review] no transcript.md in {job.dir}")

    chunks = chunker.chunk_file(
        src,
        int(job.acfg.get("chunk_chars") or 14000),
        int(job.acfg.get("chunk_overlap_turns") or 3),
    )
    if not chunks:
        raise SystemExit("[review] transcript parsed into 0 chunks — check the format")

    jobstate.set_state(job.dir, "scanning", f"{len(chunks)} 段")
    total = len(chunks)
    per_chunk = max(2, int(job.acfg.get("max_questions") or 8) // 2)
    print(f"[review] scan: {total} chunk(s), {job.backend}, "
          f"{job.acfg.get('max_parallel', 3)} in parallel")

    def run_one(ch: chunker.Chunk):
        out = job.work / f"scan_{ch.index:02d}.json"
        prompt = fill(T_SCAN, {
            "TOTAL": total, "INDEX": ch.index + 1,
            "TITLE": job.title, "CLIENT": job.f("client", "—"),
            "DATE": job.f("date"), "PARTICIPANTS": job.f("participants", "未提供"),
            "EXPECTED_SPEAKERS": job.meta.get("num_speakers") or "未填",
            "NUM_SPEAKERS": job.result.get("num_speakers") or "?",
            "SPEAKER_WARNING": job.speaker_warning,
            "USER_CONTEXT": job.user_context,
            "MAX_PER_CHUNK": per_chunk,
            "OUT_PATH": str(out),
            "CONTEXT": chunker.render_turns(ch.overlap_turns) if ch.overlap_turns else "（本段為開頭，無上文）",
            "BODY": chunker.render_turns(ch.own_turns),
        })
        data, why = call_agent(job, prompt, out, f"scan {ch.index + 1}/{total}")
        return ch, data, why

    results = in_parallel(job, chunks, run_one)
    merged = merge_scans(job, results)

    atomic_write(job.dir / "transcript_draft.md", merged["draft"])
    atomic_write(job.dir / "questions.json", merged["questions_doc"])

    n_q = len(merged["questions_doc"]["cards"])
    jobstate.save(job.dir, state="awaiting_answers",
                  note=f"{n_q} 題待回答", error=None,
                  scan={"chunks": total, "failed": merged["failed"],
                        "questions": n_q, "at": time.time()})

    if deliver:
        notify_questions(job, merged, total)
    return merged


def merge_scans(job: Job, results: list) -> dict:
    """Fold per-chunk scan output into one draft and one question set.

    Two deliberate choices:
      * A chunk whose agent failed contributes its *raw* turns to the draft.
        A gap in the middle of a transcript is far worse than an unpolished
        paragraph, and stage 2 still sees the real words.
      * Questions are deduplicated on (type, key) across chunks, because the
        same unclear surname surfaces in every chunk it appears in. The merged
        card remembers which chunks raised it and is ranked partly on that —
        a name that confused four readers matters more than one that confused
        one.
    """
    raw_text = (job.dir / "transcript.md").read_text(errors="ignore")
    draft_parts: list[str] = []
    failed: list[int] = []
    reps: dict[tuple[str, str], dict] = {}
    speakers: dict[str, dict] = {}
    cards: dict[tuple[str, str], dict] = {}

    for ch, data, why in results:
        if not data:
            failed.append(ch.index)
            draft_parts.append(chunker.render_turns(ch.own_turns))
            continue

        text = (data.get("draft") or "").strip()
        draft_parts.append(text or chunker.render_turns(ch.own_turns))
        if not text:
            failed.append(ch.index)

        for r in data.get("replacements") or []:
            find, rep = str(r.get("find") or "").strip(), str(r.get("replace") or "").strip()
            if find and rep and find != rep:
                reps.setdefault((find, rep), {"find": find, "replace": rep,
                                              "note": str(r.get("note") or "")})

        for s in data.get("speakers") or []:
            label = str(s.get("label") or "").strip()
            if not label:
                continue
            row = speakers.setdefault(label, {"label": label, "guess": "",
                                              "confidence": "low", "why": "", "turns": 0})
            row["turns"] += 1
            # A high-confidence read replaces a low-confidence one; between two
            # of equal confidence the first stands, so the merge is stable.
            if s.get("guess") and (not row["guess"] or
                                   (s.get("confidence") == "high" and row["confidence"] != "high")):
                row.update(guess=str(s["guess"]).strip(),
                           confidence="high" if s.get("confidence") == "high" else "low",
                           why=str(s.get("why") or ""))

        for q in data.get("questions") or []:
            qtype = str(q.get("type") or "").strip()
            if qtype not in TYPE_RANK:
                continue
            key = str(q.get("key") or q.get("question") or "").strip()
            if not key:
                continue
            slot = cards.get((qtype, key))
            if slot is None:
                opts = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()]
                slot = {
                    "type": qtype, "key": key,
                    "question": str(q.get("question") or "").strip() or f"「{key}」要如何處理？",
                    "options": opts,
                    "best_guess": str(q.get("best_guess") or "").strip(),
                    "evidence": [], "chunks": [],
                }
                cards[(qtype, key)] = slot
            else:
                for o in (q.get("options") or []):
                    o = str(o).strip()
                    if o and o not in slot["options"]:
                        slot["options"].append(o)
            slot["chunks"].append(ch.index)
            for ev in (q.get("evidence") or [])[:2]:
                if len(slot["evidence"]) < 4:
                    slot["evidence"].append({
                        "timestamp": str(ev.get("timestamp") or ""),
                        "text": str(ev.get("text") or "")[:400],
                    })

    # Speaker cards the readers forgot to raise. A label that carries real
    # dialogue and has no confirmed name is exactly the question worth asking,
    # so it is synthesised rather than left to chance.
    for label, row in speakers.items():
        if ("speaker", label) in cards or row["turns"] < 2:
            continue
        opts = [row["guess"]] if row["guess"] else []
        cards[("speaker", label)] = {
            "type": "speaker", "key": label,
            "question": f"「{label}」是誰？",
            "options": opts, "best_guess": row["guess"],
            "evidence": [{"timestamp": "", "text": row["why"]}] if row["why"] else [],
            "chunks": [],
        }

    ranked = sorted(
        cards.values(),
        key=lambda c: (TYPE_RANK[c["type"]], -len(set(c["chunks"])), c["key"]),
    )[: max(1, int(job.acfg.get("max_questions") or 8))]

    for i, c in enumerate(ranked, 1):
        c["id"] = f"q{i:02d}"
        c["type_label"] = TYPE_LABEL[c["type"]]
        c["chunks"] = sorted(set(c["chunks"]))
        opts = [o for o in c["options"] if o != OPT_OTHER]
        if c["best_guess"] and c["best_guess"] not in opts:
            opts.insert(0, c["best_guess"])
        if not c["best_guess"] and opts:
            c["best_guess"] = opts[0]
        c["options"] = opts[:5] + [OPT_OTHER]

    draft = "\n\n".join(p.strip() for p in draft_parts if p.strip()) + "\n"

    # Drop "replacements" that describe the work instead of naming a string —
    # a real reader produced {"find": "简体全文", "replace": "繁體中文"}, which
    # is a note about the whole chunk, not a substitution. Anything whose
    # `find` never occurs in the source is either that, or a hallucination;
    # either way applying it globally in stage 2 is a hazard and listing it in
    # the correction table is a lie.
    haystack = raw_text + draft
    kept = [r for r in reps.values() if r["find"] in haystack]
    if len(kept) != len(reps):
        dropped = [r["find"] for r in reps.values() if r["find"] not in haystack]
        print(f"[review] dropped {len(dropped)} phantom replacement(s): {dropped}")

    return {
        "draft": draft,
        "failed": failed,
        "speakers": list(speakers.values()),
        "replacements": kept,
        "questions_doc": {
            "job_id": job.dir.name,
            "generated_at": time.time(),
            "chunks": len(results),
            "failed_chunks": failed,
            "speakers": sorted(speakers.values(), key=lambda s: -s["turns"]),
            "replacements": kept,
            "cards": ranked,
        },
    }


def notify_questions(job: Job, merged: dict, total: int) -> None:
    doc = merged["questions_doc"]
    cards = doc["cards"]
    lines = [f"**{job.title}**", "",
             f"逐字稿掃描完成（{total} 段），整理出 **{len(cards)} 題**需要你確認。", ""]
    for c in cards[:5]:
        lines.append(f"- 〔{c['type_label']}〕{c['question']}"
                     + (f"　建議：{c['best_guess']}" if c["best_guess"] else ""))
    if len(cards) > 5:
        lines.append(f"- …另外 {len(cards) - 5} 題")
    if doc["failed_chunks"]:
        lines.append(f"\n⚠️ 有 {len(doc['failed_chunks'])} 段清稿失敗，"
                     f"該段落在草稿中維持原始逐字稿。")
    lines += ["", f"到網頁回答後才會開始寫會議記錄：", f"/job/{job.dir.name}"]
    notifier.send("awaiting_answers", "有問題要你確認", "\n".join(lines),
                  job_id=job.dir.name)


# --------------------------------------------------------------------------- #
# answers -> deterministic resolution
# --------------------------------------------------------------------------- #
def answer_of(card: dict, answers: dict) -> tuple[str, bool]:
    """(value, confirmed_by_user) for one card.

    Falls back to `best_guess` when unanswered, and the boolean is what the
    write prompt uses to decide between stating a name and hedging it.
    """
    row = (answers.get("cards") or {}).get(card["id"]) or {}
    custom = str(row.get("custom") or "").strip()
    if custom:
        return custom, True
    choice = str(row.get("choice") or "").strip()
    if choice and choice != OPT_OTHER:
        return choice, True
    return str(card.get("best_guess") or "").strip(), False


def resolve(job: Job) -> dict:
    """Turn questions + answers into a substitution plan and a facts block."""
    questions = read_json(job.dir / "questions.json")
    answers = read_json(job.dir / "answers.json")
    cards = questions.get("cards") or []

    speaker_map: dict[str, str] = {}
    speaker_guess: dict[str, str] = {}
    term_map: dict[str, str] = {}
    confirmed: list[str] = []
    guessed: list[str] = []

    for card in cards:
        value, sure = answer_of(card, answers)
        if not value:
            continue
        line = f"{card['question']} → **{value}**"
        (confirmed if sure else guessed).append(line)
        if card["type"] == "speaker":
            (speaker_map if sure else speaker_guess)[card["key"]] = value
        elif card["type"] == "term" and sure and card["key"] != value:
            term_map[card["key"]] = value

    reps = [dict(r) for r in (questions.get("replacements") or [])]
    for find, rep in term_map.items():
        reps.append({"find": find, "replace": rep, "note": "使用者確認"})
    for r in (answers.get("replacements") or []):
        find, rep = str(r.get("find") or "").strip(), str(r.get("replace") or "").strip()
        if find and rep and find != rep:
            reps.append({"find": find, "replace": rep, "note": "使用者指定"})

    seen: set[tuple[str, str]] = set()
    reps = [r for r in reps
            if (r["find"], r["replace"]) not in seen and not seen.add((r["find"], r["replace"]))]

    return {
        "questions": questions, "answers": answers,
        "speaker_map": speaker_map, "speaker_guess": speaker_guess,
        "replacements": reps,
        "confirmed": confirmed, "guessed": guessed,
        "skipped": bool(answers.get("skipped")) or not answers,
    }


def apply_substitutions(text: str, res: dict) -> str:
    """Rewrite the draft with the user's answers. Deterministic on purpose.

    Longest-first so `講者1` can never eat the prefix of `講者11`, plus a
    negative lookahead on trailing digits for the same reason. This is the one
    part of the answer path a model is never allowed to do from memory.
    """
    pairs = [(k, v) for k, v in res["speaker_map"].items()]
    pairs += [(r["find"], r["replace"]) for r in res["replacements"]]
    for find, rep in sorted(pairs, key=lambda kv: -len(kv[0])):
        if not find:
            continue
        pattern = re.escape(find) + (r"(?!\d)" if find[-1].isdigit() else "")
        text = re.sub(pattern, rep.replace("\\", r"\\"), text)
    return text


def write_clean_transcript(job: Job, res: dict) -> Path:
    """Assemble transcript_clean.md from the draft + the resolution table."""
    draft = job.dir / "transcript_draft.md"
    src = draft if draft.exists() else job.dir / "transcript.md"
    body = apply_substitutions(src.read_text(errors="ignore"), res)

    head = [f"# 逐字稿（定稿）：{job.title}", ""]
    meta_rows = [("對象", job.f("client", "—")), ("日期", job.f("date", "—")),
                 ("長度", hms(job.result.get("duration"))),
                 ("與會者", job.f("participants", "未提供"))]
    head += [f"- **{k}**：{v}" for k, v in meta_rows]
    if res["speaker_map"]:
        head += ["", "**講者對應（使用者確認）**", ""]
        head += [f"- {k} = {v}" for k, v in res["speaker_map"].items()]
    if res["speaker_guess"]:
        head += ["", "**講者對應（系統推測，未經確認）**", ""]
        head += [f"- {k} ≈ {v}" for k, v in res["speaker_guess"].items()]
    head += ["", "---", ""]

    tail: list[str] = []
    if res["replacements"]:
        tail = ["", "---", "", "## 修正對照表", "",
                "| 原文（ASR） | 修正後 | 依據 |", "|---|---|---|"]
        tail += [f"| {r['find']} | {r['replace']} | {r.get('note') or '上下文判讀'} |"
                 for r in res["replacements"]]

    out = job.dir / "transcript_clean.md"
    atomic_write(out, "\n".join(head) + body.strip() + "\n" + "\n".join(tail) + "\n")
    return out


def confirmed_block(job: Job, res: dict) -> str:
    """The `{{CONFIRMED}}` section injected into the writing prompt."""
    out = ["## 已確認事實（優先於逐字稿字面）", ""]
    if res["confirmed"]:
        out += ["**使用者已確認——這些一律照辦，不要再推測：**", ""]
        out += [f"- {line}" for line in res["confirmed"]]
        out.append("")
    if res["speaker_map"]:
        out += ["**逐字稿中的講者標籤已由 Python 直接換成真實姓名**，"
                "所以你讀到的姓名是確定的，不必再註明「推測」。", ""]
    if res["guessed"]:
        out += ["**以下是系統推測、使用者尚未確認**——可以採用，但文件開頭必須註明"
                "「講者對應為推測」，並且不要把推測寫成既成事實：", ""]
        out += [f"- {line}" for line in res["guessed"]]
        out.append("")
    if res["skipped"]:
        out += ["> ⚠️ 使用者選擇「跳過問題」，以上全部是系統推測。"
                "文件開頭請註明「本文件未經使用者確認關鍵資訊」。", ""]
    if not res["confirmed"] and not res["guessed"]:
        out += ["（本次沒有需要確認的事項。）", ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# stage 2 — write
# --------------------------------------------------------------------------- #
def stage_write(job: Job, deliver: bool) -> dict:
    jobstate.set_state(job.dir, "writing", "撰寫文件")
    res = resolve(job)

    clean = write_clean_transcript(job, res)
    print(f"[review] transcript_clean.md ← {len(res['replacements'])} 處取代、"
          f"{len(res['speaker_map'])} 位講者確認")

    if job.plan["want_note"]:
        source = clean
        chars = len(source.read_text(errors="ignore"))
        threshold = int(job.acfg.get("mapreduce_threshold_chars") or 60000)
        if chars > threshold:
            print(f"[review] {chars:,} chars > {threshold:,} → map-reduce")
            source, desc = write_materials(job, res, source), "分段素材（唯一資料來源）"
        else:
            desc = "定稿逐字稿"
        run_writer(job, res, source, desc)

    return finish(job, deliver)


def write_materials(job: Job, res: dict, source: Path) -> Path:
    """Map phase: extract raw material per chunk so the merge fits in a prompt.

    The merged file is structured JSON rather than prose because the reduce
    step must not have to re-parse someone else's summary — it needs the
    topics, decisions, numbers and quotes as data, with timestamps intact.
    """
    chunks = chunker.chunk_file(
        source,
        int(job.acfg.get("chunk_chars") or 14000),
        int(job.acfg.get("chunk_overlap_turns") or 3),
    )
    total = len(chunks)
    print(f"[review] materials: {total} chunk(s)")
    confirmed = confirmed_block(job, res)

    def run_one(ch: chunker.Chunk):
        out = job.work / f"material_{ch.index:02d}.json"
        prompt = fill(T_PARTIAL, {
            "TOTAL": total, "INDEX": ch.index + 1,
            "TITLE": job.title, "CLIENT": job.f("client", "—"),
            "DATE": job.f("date"), "PARTICIPANTS": job.f("participants", "未提供"),
            "CONFIRMED": confirmed, "USER_CONTEXT": job.user_context,
            "OUT_PATH": str(out),
            "BODY": chunker.render_turns(ch.own_turns),
        })
        data, _ = call_agent(job, prompt, out, f"material {ch.index + 1}/{total}")
        return ch, data

    keys = ("topics", "decisions", "actions", "risks", "numbers", "quotes", "open")
    bundle: dict = {k: [] for k in keys}
    bundle["_missing_chunks"] = []
    for ch, data in in_parallel(job, chunks, run_one):
        if not data:
            bundle["_missing_chunks"].append(
                {"index": ch.index + 1, "range": f"{ch.stamp_start}–{ch.stamp_end}"})
            continue
        for k in keys:
            for item in (data.get(k) or []):
                bundle[k].append(item)

    if bundle["_missing_chunks"]:
        print(f"[review] WARNING {len(bundle['_missing_chunks'])} material chunk(s) missing")

    out = job.work / "materials.json"
    atomic_write(out, bundle)
    return out


def run_writer(job: Job, res: dict, source: Path, source_desc: str) -> None:
    stems = [s for s in job.plan["stems"] if s != "transcript_clean"]
    lines = [f"- `{s}.md` — {DOC_KINDS[s][0]}" for s in stems]
    lines.append("- `action_items.json` — 決議／待辦／風險／關鍵數字（結構化）")
    lines.append("- `INDEX.md` — 檔案清單與機密層級")

    prompt = fill(T_WRITE, {
        "ROOT": ROOT, "JOB_DIR": job.dir, "SPEC_PATH": SPEC_PATH,
        "SOURCE_DESC": source_desc, "TRANSCRIPT_PATH": source,
        "META_PATH": job.dir / "meta.json",
        "TITLE": job.title, "CLIENT": job.f("client", "—"), "DATE": job.f("date"),
        "DURATION": hms(job.result.get("duration")),
        "PARTICIPANTS": job.f("participants", "未提供"),
        "NUM_SPEAKERS": job.result.get("num_speakers") or "?",
        "TRANSCRIPT_CHARS": f"{len(source.read_text(errors='ignore')):,}",
        # Labels are already resolved in the text, so re-warning the writer
        # about diarisation would only make it hedge names it can trust.
        "SPEAKER_WARNING": "" if res["speaker_map"] else job.speaker_warning,
        "CONFIRMED": confirmed_block(job, res),
        "DELIVERABLES": "\n".join(lines),
    }) + "\n" + job.user_context

    (job.work / "write_prompt.md").write_text(prompt)
    cmd = _backend_cmd(job.backend, job.binary, prompt, job.model)
    rc, elapsed = _run_agent(cmd, job.log, job.timeout)
    print(f"[review] writer exited {rc} after {elapsed}s")


# --------------------------------------------------------------------------- #
# convert + deliver
# --------------------------------------------------------------------------- #
def finish(job: Job, deliver: bool) -> dict:
    """Convert whatever the writer produced, then report the gaps honestly."""
    delivery: list[dict] = []
    missing: list[str] = []
    failed: list[str] = []

    for stem in job.plan["stems"]:
        md = job.dir / f"{stem}.md"
        if not md.exists():
            missing.append(f"{stem}.md")
            continue
        for fmt in job.plan["formats"]:
            if fmt == "md":
                delivery.append({"stem": stem, "fmt": "md", "path": str(md)})
                continue
            ok, info = convert(job.dir, stem, fmt, job.result, job.meta)
            (delivery if ok else failed).append(
                {"stem": stem, "fmt": fmt, "path": info} if ok else f"{stem}.{fmt}: {info}")

    report = {
        "job_id": job.dir.name, "backend": job.backend, "model": job.model,
        "plan": job.plan, "delivery": delivery, "missing": missing,
        "convert_failed": failed,
        "agent_report": read_json(job.dir / "agent_report.json"),
        "finished_at": time.time(),
        "ok": not missing and not failed,
    }
    atomic_write(job.dir / "delivery.json", report)

    for line in missing:
        print(f"[review] MISSING  {line}")
    for line in failed:
        print(f"[review] CONVERT  {line}")
    print(f"[review] {len(delivery)} file(s) ready")

    jobstate.save(job.dir, state="done" if report["ok"] else "error",
                  note=f"{len(delivery)} 檔",
                  error=None if report["ok"] else
                  "、".join(missing + [str(f) for f in failed])[:300])

    if deliver:
        notify_done(job, report)
    return report


def notify_done(job: Job, report: dict) -> None:
    ar = report.get("agent_report") or {}
    fmts = "＋".join(FMT_NAME[f] for f in job.plan["formats"])
    body = [f"**{job.title}**", "", f"文件產出完成 · {fmts} · 由 `{job.backend}` 在本機撰寫"]
    if ar.get("notes"):
        body += ["", str(ar["notes"])]
    if ar.get("uncertain"):
        body += ["", "**待確認：**"] + [f"- {u}" for u in list(ar["uncertain"])[:5]]
    if report["missing"] or report["convert_failed"]:
        body += ["", "⚠️ 未完成：" +
                 "、".join(report["missing"] + [str(f) for f in report["convert_failed"]])[:300]]
    body += ["", f"/job/{job.dir.name}"]

    notifier.send("done" if report["ok"] else "error",
                  "會議文件完成" if report["ok"] else "會議文件產出未完成",
                  "\n".join(body),
                  files=[d["path"] for d in report["delivery"]],
                  job_id=job.dir.name)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def resolve_job_dir(arg: str) -> Path:
    if arg == "latest":
        jobs = [d for d in (ROOT / "archive").iterdir() if d.is_dir()]
        if not jobs:
            raise SystemExit("[review] archive/ is empty")
        return max(jobs, key=lambda p: p.stat().st_mtime).resolve()
    p = Path(arg).expanduser()
    return (p if p.is_absolute() or p.exists() else jobstate.job_dir(arg)).resolve()


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan a transcript, ask, then write the notes.")
    ap.add_argument("job", help="archive/<job_id>, a job id, or 'latest'")
    ap.add_argument("--stage", choices=["scan", "write", "auto"], default="scan")
    ap.add_argument("--backend", choices=["claude", "codex"], default=None)
    ap.add_argument("--bin", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=None)
    ap.add_argument("--deliver", action="store_true")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep .review/ scratch files (default: kept on failure only)")
    args = ap.parse_args()

    acfg = CONFIG.get("agent", {})
    backend = args.backend or acfg.get("backend") or "claude"
    binary = resolve_bin(backend, args.bin or acfg.get("bin"))
    job = Job(resolve_job_dir(args.job), backend, args.model or acfg.get("model"),
              binary, args.timeout or int(acfg.get("timeout_sec") or 3600))

    (ROOT / "logs").mkdir(exist_ok=True)
    print(f"[review] {job.dir.name} · stage={args.stage} · backend={backend}")

    try:
        if args.stage in ("scan", "auto"):
            stage_scan(job, deliver=args.deliver and args.stage == "scan")
        if args.stage in ("write", "auto"):
            report = stage_write(job, deliver=args.deliver)
            return 0 if report["ok"] else 1
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a crash must leave the job diagnosable
        jobstate.set_state(job.dir, "error", "", f"{type(exc).__name__}: {exc}"[:300])
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
