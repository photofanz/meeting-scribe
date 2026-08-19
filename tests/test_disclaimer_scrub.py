"""Notes must not explain how the pipeline works.

A meeting note is handed to attendees and sent to clients. How the diarisation
clustered, how speaker attribution was inferred, what the system could not
resolve — that is our internal limitation, and the reader neither needs it nor
should see it. It already reaches the user through `agent_report.json`'s
`uncertain`, which the completion notice surfaces.

The regression that prompted this: a note opened with a blockquote explaining
that "逐字稿的聲紋分群把多人對話併入同一個標籤，因此……逐句歸屬係依內容推斷".
The prompts asked for exactly that, so these tests pin both halves — the
prompts no longer ask, and the writer no longer can.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import agent_note  # noqa: E402
import review  # noqa: E402
from agent_note import (  # noqa: E402
    DISCLAIMER_OK_STEMS,
    scrub_note,
    strip_process_disclaimers,
)
from review import Job  # noqa: E402

SPEC = (ROOT / "templates" / "NOTE_SPECS.md").read_text()
TASK = (ROOT / "templates" / "AGENT_TASK.md").read_text()

# Verbatim from archive/2026-08-19_群曜_65de08/note_general.md, line 3.
REPORTED = (
    "> 主持：Celine（副總）　·　**講者對應為推測**：使用者已確認「顧問＝Jerry」、"
    "兩個聲紋群主要為 Celine；但逐字稿的聲紋分群把多人對話併入同一個標籤，"
    "因此本記錄中「艾薇說」「Jim 說」「Jerry 說」等逐句歸屬係依內容推斷，"
    "判不出來的句子不指派給特定人。"
)


# --------------------------------------------------------------------------- #
# the stripper
# --------------------------------------------------------------------------- #
def test_removes_the_reported_disclaimer():
    text = f"# 會議記錄\n\n{REPORTED}\n\n## 一、本次會議重點\n"
    got, removed = strip_process_disclaimers(text)
    assert removed == 1
    assert got == "# 會議記錄\n\n## 一、本次會議重點\n"


@pytest.mark.parametrize("block", [
    "> **講者對應為推測**：以下逐句歸屬依內容判斷。",
    "> 註：本文件未經使用者確認關鍵資訊。",
    "> 系統的聲紋分群把多人對話併入同一個標籤。",
    "> 逐字稿的講者標籤不可信，姓名依內容推斷。",
    # a block wrapped over several lines still goes as one unit
    "> 說明：聲紋分群把多人對話併入同一標籤，\n> 因此逐句歸屬係依內容推斷。",
])
def test_removes_process_talk(block):
    got, removed = strip_process_disclaimers(f"# 標題\n\n{block}\n\n內文\n")
    assert removed == 1
    assert got == "# 標題\n\n內文\n"


@pytest.mark.parametrize("block", [
    # an ordinary pull quote of what someone said
    "> 「我們下個月再決定要不要續約」——王總",
    "> 本次會議由行銷部提案，法務部列席。",
    "> 詳見 [附件一](./annex.md)。",
    # the technical vocabulary is real: a consultant demoing this very tool
    # says it out loud, and a verbatim quote of that is content, not a
    # disclaimer. Quoted, it stays.
    "> 「聲紋分群這塊我們自己做不了」——Jim",
])
def test_leaves_ordinary_blockquotes_alone(block):
    body = f"# 標題\n\n{block}\n\n內文\n"
    got, removed = strip_process_disclaimers(body)
    assert removed == 0
    assert got == body


def test_only_blockquotes_are_in_scope():
    """Deliberately narrow: body prose is where the meeting lives, and no
    keyword is worth silently deleting a discussion bullet over."""
    body = "- **討論內容**：Jim 提到聲紋分群的辨識率問題。\n"
    assert strip_process_disclaimers(body) == (body, 0)


def test_skips_fenced_code_blocks():
    """The spec itself is quoted into prompts and shows skeleton blockquotes."""
    body = "```markdown\n> 主持：{姓名}　·　講者對應為推測\n```\n"
    assert strip_process_disclaimers(body) == (body, 0)


def test_leaves_no_orphan_blank_lines():
    text = f"# 會議記錄\n\n{REPORTED}\n\n\n## 一、重點\n\n內文\n"
    got, _ = strip_process_disclaimers(text)
    assert "\n\n\n" not in got
    assert not got.startswith("\n")


def test_does_not_start_the_file_with_a_blank_line():
    got, removed = strip_process_disclaimers(f"{REPORTED}\n\n# 會議記錄\n")
    assert removed == 1
    assert got == "# 會議記錄\n"


# --------------------------------------------------------------------------- #
# the file-level guard
# --------------------------------------------------------------------------- #
def test_scrub_rewrites_a_note(tmp_path):
    md = tmp_path / "note_general.md"
    md.write_text(f"# 會議記錄\n\n{REPORTED}\n\n## 一、重點\n")
    assert scrub_note(tmp_path, "note_general") == 1
    assert md.read_text() == "# 會議記錄\n\n## 一、重點\n"


@pytest.mark.parametrize("stem", ["note_client", "note_self", "note_partner"])
def test_scrub_covers_every_client_variant(tmp_path, stem):
    (tmp_path / f"{stem}.md").write_text(f"# 記錄\n\n{REPORTED}\n")
    assert scrub_note(tmp_path, stem) == 1
    assert (tmp_path / f"{stem}.md").read_text() == "# 記錄\n"


def test_transcript_clean_keeps_everything(tmp_path):
    """It is a transcript, not a deliverable: its own spec asks for the note."""
    body = f"# 逐字稿\n\n{REPORTED}\n\n**王總** `[00:00:06]`：你好\n"
    (tmp_path / "transcript_clean.md").write_text(body)
    assert scrub_note(tmp_path, "transcript_clean") == 0
    assert (tmp_path / "transcript_clean.md").read_text() == body


def test_interview_keeps_timestamps_but_loses_disclaimers(tmp_path):
    """Two separate exemptions. The quotes need their marks; the reader of an
    interview note still does not need to hear about diarisation."""
    md = tmp_path / "note_interview.md"
    md.write_text(f"# 訪談\n\n{REPORTED}\n\n## 重點語錄\n- 「先做再說」`[00:12:34]`\n")
    assert scrub_note(tmp_path, "note_interview") == 1
    got = md.read_text()
    assert "[00:12:34]" in got
    assert "聲紋分群" not in got


def test_scrub_counts_both_kinds(tmp_path):
    md = tmp_path / "note_general.md"
    md.write_text(f"{REPORTED}\n\n- 他說要先測[00:01:54]。\n")
    assert scrub_note(tmp_path, "note_general") == 2
    assert md.read_text() == "- 他說要先測。\n"


def test_scrub_leaves_a_clean_note_byte_identical(tmp_path):
    body = "# 會議記錄\n\n> 「下個月再談」——王總\n\n- 討論內容：他說要先測。\n"
    md = tmp_path / "note_general.md"
    md.write_text(body)
    before = md.stat().st_mtime_ns
    assert scrub_note(tmp_path, "note_general") == 0
    assert md.read_text() == body
    assert md.stat().st_mtime_ns == before   # not rewritten at all


def test_only_the_transcript_is_exempt():
    assert DISCLAIMER_OK_STEMS == {"transcript_clean"}


# --------------------------------------------------------------------------- #
# the prompts that asked for it
# --------------------------------------------------------------------------- #
def test_spec_bans_process_talk():
    ban = SPEC.split("## 通用禁則")[1]
    assert "不得在會議記錄裡寫任何關於處理流程的說明" in ban
    assert "agent_report.json" in ban


def test_general_skeleton_has_no_disclaimer_header():
    general = SPEC.split("## general —")[1].split("## client —")[0]
    assert "講者對應為推測" not in general
    assert "主持：{姓名}" not in general


def test_transcript_spec_is_untouched():
    """That file is a working document; its header note is legitimate."""
    transcript = SPEC.split("## transcript_clean.md")[1].split("## general —")[0]
    assert "講者對應為推測" in transcript


def test_task_template_points_at_the_report_instead():
    assert "在文件開頭註明" not in TASK
    assert "agent_report.json" in TASK


@pytest.fixture()
def job_dir(tmp_path, monkeypatch):
    d = tmp_path / "2026-01-01_test_abc"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps(
        {"want_note": True, "meeting_type": "general", "formats": ["md"],
         "num_speakers": 3}, ensure_ascii=False))
    (d / "status.json").write_text(json.dumps(
        {"result": {"num_speakers": 112}}, ensure_ascii=False))
    (d / "transcript.md").write_text("逐" * 500)
    monkeypatch.setattr(review, "ROOT", tmp_path)  # keep logs out of the repo
    (tmp_path / "logs").mkdir(exist_ok=True)
    return d


def without_spec(prompt: str) -> str:
    """Drop the embedded spec before asserting on the instructions.

    The single-envelope API path inlines NOTE_SPECS.md whole, and the spec
    still tells the *transcript* writer to note a guessed speaker mapping —
    legitimately, because that file is a working document. Only the
    instructions wrapped around the spec are under test here.
    """
    return prompt.replace(SPEC, "《規格全文》")


def written_prompt(job_dir: Path, backend: str, acfg: dict, monkeypatch) -> str:
    monkeypatch.setattr(review, "invoke_backend",
                        lambda *a, **kw: (0, "1.0", '{"files": {}}'))
    monkeypatch.setattr(review, "last_api_error", lambda: None)
    monkeypatch.setattr(review, "persist_api_writer_result", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(review, "run_writer_tool_loop", lambda *a, **kw: None)
    job = Job(job_dir, backend, "m", "", 60, acfg)
    res = {"speaker_map": {}, "confirmed": [], "skipped": True,
           "guessed": ["講者3 ≈ 王總"]}
    review.run_writer(job, res, job_dir / "transcript.md", "定稿逐字稿")
    return (job_dir / ".review" / "write_prompt.md").read_text()


@pytest.mark.parametrize("backend,acfg", [
    ("claude", {}),
    ("openai_compat", {"tool_loop": True}),
    ("openai_compat", {"tool_loop": False}),
])
def test_no_writer_path_asks_for_a_disclaimer(job_dir, monkeypatch, backend, acfg):
    prompt = without_spec(written_prompt(job_dir, backend, acfg, monkeypatch))
    assert "在文件開頭註明" not in prompt
    assert "文件開頭必須註明" not in prompt
    assert "文件開頭請註明" not in prompt


@pytest.mark.parametrize("backend,acfg", [
    ("claude", {}),
    ("openai_compat", {"tool_loop": True}),
    ("openai_compat", {"tool_loop": False}),
])
def test_every_writer_path_is_sent_to_the_report(job_dir, monkeypatch, backend, acfg):
    """The information must not be lost, only relocated."""
    prompt = written_prompt(job_dir, backend, acfg, monkeypatch)
    assert "聲紋分群不可靠" in prompt          # the warning still fires
    assert "uncertain" in prompt


def test_direct_agent_note_entry_point_asks_for_no_disclaimer(job_dir):
    """`agent_note.py` run on its own skips review entirely."""
    meta = json.loads((job_dir / "meta.json").read_text())
    result = {"num_speakers": 112}
    prompt = agent_note.build_prompt(job_dir, meta, result,
                                     agent_note.build_plan(meta))
    assert "在文件開頭註明" not in prompt
    assert "聲紋分群不可靠" in prompt
    assert "uncertain" in prompt


def test_direct_agent_note_api_prompt_asks_for_no_disclaimer(job_dir):
    meta = json.loads((job_dir / "meta.json").read_text())
    plan = agent_note.build_plan(meta)
    prompt = agent_note.build_api_writer_prompt(
        job_dir, meta, {"num_speakers": 112}, plan,
        job_dir / "transcript.md", "逐字稿（未經清稿）",
        "## 已確認事實\n\n（本次未經使用者確認流程。）\n",
    )
    assert "在文件開頭註明" not in without_spec(prompt)
