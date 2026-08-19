"""Section headings must not carry the spec's writing instructions.

`NOTE_SPECS.md` uses its skeleton for two jobs at once: it declares the required
structure *and* annotates a length limit on one section. The writer copies the
heading verbatim, so `（3 句以內）` — an instruction addressed to the writer —
ended up printed in every delivered meeting note.

The spec now states the limit outside the heading. These tests pin the
guarantee, and pin the boundary: a heading may legitimately carry a
parenthetical, and body prose may legitimately quote a limit someone stated in
the meeting. Neither is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

from agent_note import (  # noqa: E402
    scrub_note,
    strip_heading_instructions,
)

SPEC = (ROOT / "templates" / "NOTE_SPECS.md").read_text()


# --------------------------------------------------------------------------- #
# the spec itself
# --------------------------------------------------------------------------- #
def test_skeleton_heading_is_clean():
    """The exact heading the user reported, at its source."""
    assert "## 一、本次會議重點（3 句以內）" not in SPEC
    assert "## 一、本次會議重點\n" in SPEC


def test_no_skeleton_heading_carries_a_count_instruction():
    """Any heading in the spec's fenced skeletons, not just the reported one."""
    for line in SPEC.split("\n"):
        if line.lstrip().startswith("#"):
            assert "以內" not in line, line


def test_spec_still_states_the_three_sentence_limit():
    """Removing it from the heading must not lose the constraint."""
    assert "3 句以內" in SPEC


# --------------------------------------------------------------------------- #
# what gets stripped
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "heading",
    [
        "## 一、本次會議重點（3 句以內）",
        "## 一、本次會議重點（3句以內）",
        "## 一、本次會議重點(3 句以內)",
        "## 重點摘要（100 字以內）",
        "### 結論（至多 5 條）",
        "## 待辦（不超過 10 項）",
        "## 議題（至少 3 個）",
        "#### 摘要（3-5 句）",
        "## 摘要（3～5 句）" .replace("～", "~"),
    ],
)
def test_instruction_parentheticals_go(heading):
    got, removed = strip_heading_instructions(f"{heading}\n\n內文\n")
    assert removed == 1
    assert "以內" not in got and "至多" not in got
    assert got.split("\n")[0].endswith("點") or "（" not in got.split("\n")[0]


def test_reported_case_exactly():
    text = "# 會議記錄\n\n## 一、本次會議重點（3 句以內）\n\n本次會議確認了報價。\n"
    got, removed = strip_heading_instructions(text)
    assert removed == 1
    assert "## 一、本次會議重點\n" in got
    assert "本次會議確認了報價。" in got


def test_trailing_space_is_tidied():
    got, _ = strip_heading_instructions("## 重點 （3 句以內）\n")
    assert got == "## 重點\n"


# --------------------------------------------------------------------------- #
# what survives — the reason this is shape-based and heading-only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "heading",
    [
        "### 3. 報價調整（Jim 提出）",
        "## 二、議題討論（續）",
        "## 附錄（未討論）",
        "## 三、決議事項（2026-08-19）",
        "### 交期問題（音檔不清楚）",
    ],
)
def test_real_headings_survive(heading):
    assert strip_heading_instructions(f"{heading}\n") == (f"{heading}\n", 0)


def test_body_prose_survives():
    """Someone in the meeting may have said "3 句以內" — that is content."""
    body = "# 會議記錄\n\n- 客戶要求摘要（3 句以內）交付\n- 「報告請壓在 100 字以內」\n"
    assert strip_heading_instructions(body) == (body, 0)


def test_fenced_blocks_survive():
    body = "# 標題\n\n```markdown\n## 一、本次會議重點（3 句以內）\n```\n"
    assert strip_heading_instructions(body) == (body, 0)


def test_untouched_text_is_returned_unchanged():
    body = "# 會議記錄\n\n## 一、本次會議重點\n\n沒有東西要刪。\n"
    assert strip_heading_instructions(body) == (body, 0)


# --------------------------------------------------------------------------- #
# wired into the pipeline
# --------------------------------------------------------------------------- #
def test_scrub_note_rewrites_the_file(tmp_path):
    md = tmp_path / "note_general.md"
    md.write_text("# 會議記錄\n\n## 一、本次會議重點（3 句以內）\n\n報價已確認。\n")
    assert scrub_note(tmp_path, "note_general") == 1
    assert "（3 句以內）" not in md.read_text()


@pytest.mark.parametrize("stem", ["note_client", "note_self", "note_partner", "note_interview"])
def test_every_note_kind_is_scrubbed(tmp_path, stem):
    md = tmp_path / f"{stem}.md"
    md.write_text(f"# 記錄\n\n## 一、重點（3 句以內）\n\n內文。\n")
    assert scrub_note(tmp_path, stem) == 1
    assert "以內" not in md.read_text()


def test_clean_note_is_not_rewritten(tmp_path):
    md = tmp_path / "note_general.md"
    md.write_text("# 會議記錄\n\n## 一、本次會議重點\n\n報價已確認。\n")
    before = md.stat().st_mtime_ns
    assert scrub_note(tmp_path, "note_general") == 0
    assert md.stat().st_mtime_ns == before
