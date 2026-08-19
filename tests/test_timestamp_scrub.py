"""Notes must not carry transcript timestamps.

A prompt rule asking the writer not to emit `[00:12:34]` is a request. These
tests pin the guarantee: the pipeline strips them before conversion, so the
md, the PDF and the Word file all agree.

The regression that prompted this: a depth-contract line told the general spec
to append `[HH:MM:SS]` after every quoted claim. The writer generalised it to
every discussion bullet and a one-hour meeting came back with 154 of them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

from agent_note import (  # noqa: E402
    TIMESTAMP_OK_STEMS,
    scrub_note,
    strip_inline_timestamps,
)

SPEC = (ROOT / "templates" / "NOTE_SPECS.md").read_text()


# --------------------------------------------------------------------------- #
# the stripper
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "line, want",
    [
        # the exact shape the user reported
        ("- 檔案丟進去就更新[00:53:48]。", "- 檔案丟進去就更新。"),
        # after a closing quote mark
        ("他說「純粹就是一個意見」[00:03:05]。", "他說「純粹就是一個意見」。"),
        # a range, which the writer emitted as two joined marks
        ("形式是醫生對談[00:02:53]–[00:03:04]。", "形式是醫生對談。"),
        # comma-joined run
        ("之後會 benefit 大家[00:06:35]、[00:11:11]。", "之後會 benefit 大家。"),
        # backticked, as the spec itself writes it
        ("關鍵主張 `[00:12:01]` 在此", "關鍵主張 在此"),
        # short MM:SS form
        ("先看看[12:34]", "先看看"),
        # inside parens, which must not leave an empty pair behind
        ("這點稍後補充（[00:41:02]）", "這點稍後補充"),
        # in a table cell
        ("| 1 | 測手寫辨識度[00:01:54] | 未指派 |", "| 1 | 測手寫辨識度 | 未指派 |"),
    ],
)
def test_removes_timestamp_shapes(line, want):
    got, removed = strip_inline_timestamps(line)
    assert got == want
    assert removed >= 1


@pytest.mark.parametrize(
    "line",
    [
        "見 [附件一](./annex.md) 的說明",          # markdown link
        "工項 [待補] 由誰負責尚未確定",              # bracketed prose
        "版本 [v2] 已經送出",
        "預算是 12:34 這個比例",                    # a colon pair with no brackets
        "## 二、議題討論",
    ],
)
def test_leaves_everything_else_alone(line):
    got, removed = strip_inline_timestamps(line)
    assert got == line
    assert removed == 0


def test_skips_fenced_code_blocks():
    text = "正文[00:01:02]。\n\n```\n樣本 [00:01:02]\n```\n\n尾段[00:02:03]。"
    got, removed = strip_inline_timestamps(text)
    assert "樣本 [00:01:02]" in got      # inside the fence, untouched
    assert "正文。" in got
    assert "尾段。" in got
    assert removed == 2


def test_counts_every_mark_removed():
    _, removed = strip_inline_timestamps("a[00:00:01] b[00:00:02] c[00:00:03]")
    assert removed == 3


# --------------------------------------------------------------------------- #
# the file-level guard
# --------------------------------------------------------------------------- #
def test_scrub_rewrites_a_note(tmp_path):
    md = tmp_path / "note_general.md"
    md.write_text("- 討論內容：他說要先測[00:01:54]。\n")
    assert scrub_note(tmp_path, "note_general") == 1
    assert md.read_text() == "- 討論內容：他說要先測。\n"


@pytest.mark.parametrize("stem", ["note_client", "note_self", "note_partner"])
def test_scrub_covers_every_client_variant(tmp_path, stem):
    (tmp_path / f"{stem}.md").write_text("結論已定[00:10:00]。\n")
    assert scrub_note(tmp_path, stem) == 1
    assert (tmp_path / f"{stem}.md").read_text() == "結論已定。\n"


@pytest.mark.parametrize("stem", sorted(TIMESTAMP_OK_STEMS))
def test_transcript_and_interview_keep_their_timestamps(tmp_path, stem):
    body = "**王總** `[00:00:06]`：你好\n"
    (tmp_path / f"{stem}.md").write_text(body)
    assert scrub_note(tmp_path, stem) == 0
    assert (tmp_path / f"{stem}.md").read_text() == body


def test_scrub_leaves_a_clean_note_byte_identical(tmp_path):
    body = "# 會議記錄\n\n- 討論內容：他說要先測。\n"
    md = tmp_path / "note_general.md"
    md.write_text(body)
    before = md.stat().st_mtime_ns
    assert scrub_note(tmp_path, "note_general") == 0
    assert md.read_text() == body
    assert md.stat().st_mtime_ns == before   # not rewritten at all


def test_scrub_survives_a_missing_file(tmp_path):
    assert scrub_note(tmp_path, "note_general") == 0


# --------------------------------------------------------------------------- #
# the spec that caused it
# --------------------------------------------------------------------------- #
def test_general_depth_contract_no_longer_asks_for_timestamps():
    general = SPEC.split("## general —")[1].split("## client —")[0]
    assert "HH:MM:SS" not in general, "the general spec is asking for timestamps again"
    assert "引文" in general, "the quote requirement should survive; only the mark goes"


def test_spec_states_the_prohibition():
    ban = SPEC.split("## 通用禁則")[1]
    assert "時間戳" in ban and "不得出現" in ban


def test_interview_spec_still_wants_them():
    interview = SPEC.split("## interview —")[1].split("## 通用禁則")[0]
    assert "時間戳" in interview
