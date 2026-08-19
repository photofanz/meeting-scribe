"""The length requirement has to reach every writer path, not just one.

There are three ways a note gets written — a CLI agent (Claude/Codex), an API
model driven through the tool loop, and an API model answering with a single
JSON envelope — and until now only the middle one was ever told how much to
write. The other two inherited a prompt that is six prohibitions and no floor,
where "每個議題兩行、結論全填待定" violates nothing.

These tests pin the contract to the rendered prompt of each path, because that
is the only place the model actually reads it, and a placeholder that silently
stops being substituted would leave no other trace.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import agent_note  # noqa: E402
import review  # noqa: E402
from agent_note import scale_contract_block  # noqa: E402
from review import Job  # noqa: E402

MARKER = "會議記錄的份量要與會議實際討論相稱"


@pytest.fixture()
def job_dir(tmp_path, monkeypatch):
    d = tmp_path / "2026-01-01_test_abc"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps(
        {"want_note": True, "want_transcript": True, "meeting_type": "general",
         "formats": ["md"]}, ensure_ascii=False))
    (d / "status.json").write_text(json.dumps({"result": {}}, ensure_ascii=False))
    (d / "transcript.md").write_text("逐" * 20709)
    monkeypatch.setattr(review, "ROOT", tmp_path)  # keep logs out of the repo
    (tmp_path / "logs").mkdir(exist_ok=True)
    return d


def written_prompt(job_dir: Path, backend: str, acfg: dict, monkeypatch) -> str:
    """Run the writer far enough to render its prompt, then stop.

    Every path persists the prompt to .review/write_prompt.md before doing
    anything expensive, so stubbing the transport is enough to read back
    exactly what the model would have seen.
    """
    monkeypatch.setattr(review, "invoke_backend",
                        lambda *a, **kw: (0, "1.0", '{"files": {}}'))
    monkeypatch.setattr(review, "last_api_error", lambda: None)
    monkeypatch.setattr(review, "persist_api_writer_result", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(review, "run_writer_tool_loop", lambda *a, **kw: None)
    job = Job(job_dir, backend, "m", "", 60, acfg)
    res = {"speaker_map": {}, "confirmed": [], "guessed": [], "skipped": False}
    review.run_writer(job, res, job_dir / "transcript.md", "定稿逐字稿")
    return (job_dir / ".review" / "write_prompt.md").read_text()


# --------------------------------------------------------------------------- #
# the block itself
# --------------------------------------------------------------------------- #
def test_the_contract_states_the_source_size():
    assert "20,709" in scale_contract_block(20709)


def test_the_contract_forbids_dropping_topics_to_save_space():
    block = scale_contract_block(20000)
    assert "有幾個就寫幾個" in block
    assert "濃縮成摘要" in block


def test_the_contract_does_not_license_invention():
    """A floor sitting next to six prohibitions reads as permission to fill
    gaps unless it says otherwise; that misread is worse than a thin note."""
    assert "不要編造" in scale_contract_block(20000)


# --------------------------------------------------------------------------- #
# all three paths
# --------------------------------------------------------------------------- #
def test_cli_path_gets_the_contract(job_dir, monkeypatch):
    prompt = written_prompt(job_dir, "claude", {}, monkeypatch)
    assert MARKER in prompt


def test_cli_path_does_not_get_append_instructions_it_cannot_follow(job_dir, monkeypatch):
    """The Write tool of a CLI agent has no append mode."""
    prompt = written_prompt(job_dir, "claude", {}, monkeypatch)
    assert 'mode="append"' not in prompt


def test_tool_loop_path_gets_both_the_contract_and_the_mechanics(job_dir, monkeypatch):
    prompt = written_prompt(job_dir, "openai_compat", {"tool_loop": True}, monkeypatch)
    assert MARKER in prompt
    assert 'mode="append"' in prompt


def test_single_envelope_api_path_gets_the_contract(job_dir, monkeypatch):
    prompt = written_prompt(job_dir, "openai_compat", {"tool_loop": False}, monkeypatch)
    assert MARKER in prompt


def test_single_envelope_api_path_is_told_to_keep_the_json_closed(job_dir, monkeypatch):
    """It cannot append, and a reply truncated mid-object parses as nothing —
    so the length requirement must never cost the entire output."""
    prompt = written_prompt(job_dir, "openai_compat", {"tool_loop": False}, monkeypatch)
    assert "整個 JSON 必須完整閉合" in prompt


def test_direct_agent_note_entry_point_gets_the_contract(job_dir):
    """`agent_note.py` run on its own skips review entirely."""
    meta = json.loads((job_dir / "meta.json").read_text())
    prompt = agent_note.build_prompt(job_dir, meta, {}, agent_note.build_plan(meta))
    assert MARKER in prompt
    assert "20,709" in prompt


# --------------------------------------------------------------------------- #
# the placeholder must never leak
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend,acfg", [
    ("claude", {}),
    ("openai_compat", {"tool_loop": True}),
])
def test_no_placeholder_survives_into_the_prompt(job_dir, monkeypatch, backend, acfg):
    assert "{{" not in written_prompt(job_dir, backend, acfg, monkeypatch)


def test_direct_entry_point_leaves_no_placeholder(job_dir):
    meta = json.loads((job_dir / "meta.json").read_text())
    prompt = agent_note.build_prompt(job_dir, meta, {}, agent_note.build_plan(meta))
    assert "{{" not in prompt
