"""JSON schemas for the private (local-model) pipeline. Single source of truth.

LM Studio implements OpenAI's `response_format: json_schema` with
`strict: true`, which constrains decoding rather than merely asking nicely.
Nothing in this project used it, and the private path paid for that in
truncated replies and unparseable JSON.

Two rules shape every schema here, and both come from measurement rather than
taste:

  1. **No field may carry source text.** Asked to quote 「就這樣定了。」the
     model returned 「就這樣定了.」 — a full-width period silently changed to
     an ASCII one. A model that is allowed to restate the transcript will
     edit it, and an edited quote is a fabricated quote. So the model returns
     `turn` indices and Python does the extraction from `transcript_clean.md`.
     The one exception is `numbers[].literal`, which exists precisely so the
     verifier can check a copied string against its own turn; anything that
     fails that check is dropped rather than shipped.

  2. **Strict means strict.** OpenAI's strict mode requires every property to
     appear in `required` and every object to set `additionalProperties:
     false`. "Optional" is expressed as a nullable type, never as an absent
     key, so a schema violation is a real disagreement and not a shrug.

`title` doubles as the `json_schema.name` sent on the wire.
"""
from __future__ import annotations


def _obj(props: dict, *, title: str = "") -> dict:
    """An object node that satisfies strict mode without repeating boilerplate."""
    node = {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }
    if title:
        node["title"] = title
    return node


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_INT_OR_NULL = {"type": ["integer", "null"]}

# 已決議 / 待定 / 保留, in the wire form the pipeline stores. NOTE_SPECS.md
# renders them in Chinese; keeping the enum ASCII means a model that drifts on
# punctuation cannot invent a fourth status.
STATUS_VALUES = ["decided", "pending", "parked"]
STATUS_LABEL = {"decided": "已決議", "pending": "待定", "parked": "保留"}


# --------------------------------------------------------------------------- #
# S1 — scan (private path only)
# --------------------------------------------------------------------------- #
# The shared SCAN_TASK.md asks for the cleaned transcript back as a JSON string
# field. On a 14,000-character chunk that means output ≈ input, and with
# max_output_tokens at 16,384 a Chinese transcript overruns it essentially
# every time — the reply is truncated mid-string and the whole chunk is lost.
#
# The find/replace list was always the real product of this stage; the full
# draft was redundant with it. Python applies the replacements instead, so the
# reply drops from ~14k characters to a few hundred.
SCAN = _obj({
    "replacements": _arr(_obj({
        "find": _STR,
        "replace": _STR,
        "note": _STR,
    })),
    "speakers": _arr(_obj({
        "label": _STR,
        "guess": _STR,
        "confidence": {"type": "string", "enum": ["high", "low"]},
        "why": _STR,
    })),
    "questions": _arr(_obj({
        "type": {"type": "string",
                 "enum": ["speaker", "term", "unclear", "conflict", "undecided"]},
        "key": _STR,
        "question": _STR,
        "options": _arr(_STR),
        "best_guess": _STR,
        # Evidence is a turn index, never a quotation: the question card's
        # excerpt is cut by Python from the transcript it already has.
        "evidence_turns": _arr(_INT),
    })),
}, title="scan_result")


# --------------------------------------------------------------------------- #
# S2 — evidence extraction (map)
# --------------------------------------------------------------------------- #
EVIDENCE = _obj({
    "topics": _arr(_obj({
        # Chunk-local running id; S3 renumbers globally.
        "topic_id": _STR,
        # The model's own short label. Not a quotation — a heading it wrote.
        "label": _STR,
        "turns": _arr(_INT),
        "status": {"type": "string", "enum": STATUS_VALUES},
        # Which turn settles the status. null when it genuinely cannot tell,
        # which is information; a guessed index would not be.
        "status_turn": _INT_OR_NULL,
        "speakers": _arr(_STR),
    })),
    "points": _arr(_obj({
        "turn": _INT,
        "speaker": _STR,
        # Paraphrase is allowed here and nowhere else: this is the model's
        # reading of the turn, and it is never rendered as a quotation.
        "gist": _STR,
        "topic_id": _STR,
    })),
    "numbers": _arr(_obj({
        "turn": _INT,
        # The only near-verbatim field in the whole pipeline. note_verify
        # checks it really is a substring of that turn and drops it if not.
        "literal": _STR,
        "means": _STR,
    })),
    "actions": _arr(_obj({
        "turn": _INT,
        "what": _STR,
        "owner": _STR,
        "due": _STR,
    })),
    "unclear": _arr(_obj({
        "turn": _INT,
        "why": _STR,
    })),
}, title="evidence")


# --------------------------------------------------------------------------- #
# S4 — one topic, one section
# --------------------------------------------------------------------------- #
# The model never emits markdown. It fills this, and S5 renders the heading
# level, the ordering and the quotations — so a section can be malformed in
# content but never in structure.
SECTION = _obj({
    "heading": _STR,
    "discussion": _arr(_obj({
        # Attribution comes from the turn, not from the model retyping a name.
        "turn": _INT,
        "point": _STR,
    })),
    "status": {"type": "string", "enum": STATUS_VALUES},
    "basis": _STR,
    # Turns whose wording carries the section. Python cuts the quotes.
    "quote_turns": _arr(_INT),
}, title="section")


# --------------------------------------------------------------------------- #
# S5 — the three-sentence opener
# --------------------------------------------------------------------------- #
# NOTE_SPECS.md caps 「本次會議重點」at three sentences and demands results
# rather than narration. It is the one place a whole-meeting view is needed,
# and it is short enough that a 27B model can hold it — but it is written from
# the merged evidence, not from the transcript, so it cannot drift.
OVERVIEW = _obj({
    "highlights": _arr(_STR),
}, title="overview")


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def validate(data, schema: dict, path: str = "$") -> list[str]:
    """Check `data` against the subset of JSON Schema used in this file.

    Deliberately not `jsonschema`: this project ships with no validation
    dependency, and adding one to check five hand-written schemas would be a
    poor trade. What is supported is exactly what `_obj`/`_arr` can express —
    object, array, string, integer, nullable, enum — and anything outside that
    is a bug in the schema rather than input to tolerate.

    Returns a list of human-readable problems; empty means valid.
    """
    out: list[str] = []
    types = schema.get("type")
    types = [types] if isinstance(types, str) else list(types or [])

    def kind(value) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, str):
            return "string"
        if isinstance(value, list):
            return "array"
        if isinstance(value, dict):
            return "object"
        return type(value).__name__

    if types and kind(data) not in types:
        return [f"{path}: 期望 {'/'.join(types)}，實得 {kind(data)}"]

    if schema.get("enum") is not None and data not in schema["enum"]:
        out.append(f"{path}: {data!r} 不在允許值 {schema['enum']} 內")

    if "object" in types and isinstance(data, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in data:
                out.append(f"{path}.{key}: 缺少必要欄位")
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in props:
                    out.append(f"{path}.{key}: 不允許的額外欄位")
        for key, sub in props.items():
            if key in data:
                out += validate(data[key], sub, f"{path}.{key}")

    if "array" in types and isinstance(data, list):
        items = schema.get("items")
        if items:
            for i, item in enumerate(data):
                out += validate(item, items, f"{path}[{i}]")
    return out
