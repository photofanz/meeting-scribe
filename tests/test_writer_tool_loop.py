"""Continuation behaviour of the tool-loop writer.

The writer loop's job is not just to call a model — it is to notice that the
model stopped early and make it carry on. Two ways it can stop early:

  * a deliverable is missing entirely (hard failure after the last round)
  * a deliverable exists but is a summary of a two-hour meeting (soft failure:
    ask it to append, warn if it never complies)

The second case is the one that shipped a 1,272-character note for a 20,709
character transcript, so it gets the most coverage here. The inference
transport is stubbed throughout; nothing needs LM Studio running.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import review  # noqa: E402
from review import Job, thin_notes, write_strategy_block  # noqa: E402


@pytest.fixture()
def job(tmp_path, monkeypatch):
    d = tmp_path / "2026-01-01_test_abc"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"want_note": True}, ensure_ascii=False))
    (d / "status.json").write_text(json.dumps({"result": {}}, ensure_ascii=False))
    (d / "transcript_clean.md").write_text("逐" * 20000)
    monkeypatch.setattr(review, "ROOT", tmp_path)  # keep logs out of the repo
    (tmp_path / "logs").mkdir(exist_ok=True)
    return Job(d, "openai_compat", "m", "", 60, {"tool_loop": True})


def writer_stub(job_dir: Path, script):
    """Stub run_tool_loop; each round writes whatever `script` says, and the
    prompt it was handed is recorded so the test can assert on the nudge."""
    rounds = iter(script)
    seen: list[str] = []

    def _run(message, model, acfg, **kw):
        seen.append(message)
        for name, text in next(rounds).items():
            (job_dir / name).write_text(text)
        return 0, "1.0", {"steps": 3, "tool_calls": 2, "tool_errors": 0,
                          "written": [], "final_text": "done", "max_steps": 60}

    return _run, seen


FULL = {"note_general.md": "內" * 4000,
        "action_items.json": "{}",
        "INDEX.md": "清單"}
THIN = {"note_general.md": "內" * 900,
        "action_items.json": "{}",
        "INDEX.md": "清單"}


# --------------------------------------------------------------------------- #
# thin-note detection
# --------------------------------------------------------------------------- #
def test_a_full_note_is_not_flagged(job):
    (job.dir / "note_general.md").write_text("內" * 4000)
    assert thin_notes(job.dir, ["note_general"], 20000, {}) == []


def test_a_summary_sized_note_is_flagged(job):
    (job.dir / "note_general.md").write_text("內" * 900)
    flagged = thin_notes(job.dir, ["note_general"], 20000, {})
    assert flagged == [("note_general.md", 900, 3000)]


def test_short_meetings_get_an_absolute_floor_not_a_proportional_one(job):
    """0.15 of a tiny transcript would wave through a two-line note."""
    (job.dir / "note_general.md").write_text("內" * 500)
    assert thin_notes(job.dir, ["note_general"], 2000, {}) == [("note_general.md", 500, 2000)]


def test_very_long_meetings_do_not_demand_an_unbounded_note(job):
    (job.dir / "note_general.md").write_text("內" * 15000)
    assert thin_notes(job.dir, ["note_general"], 500000, {}) == []


def test_the_threshold_is_configurable(job):
    (job.dir / "note_general.md").write_text("內" * 4000)
    assert thin_notes(job.dir, ["note_general"], 20000, {"note_min_ratio": 0.5}) == [
        ("note_general.md", 4000, 10000)]


def test_a_missing_note_is_not_reported_as_thin(job):
    """Missing is a different, harder failure — it must not be softened."""
    assert thin_notes(job.dir, ["note_general"], 20000, {}) == []


# --------------------------------------------------------------------------- #
# continuation rounds
# --------------------------------------------------------------------------- #
def test_a_complete_first_round_does_not_retry(job, monkeypatch):
    run, seen = writer_stub(job.dir, [FULL])
    monkeypatch.setattr(review, "run_tool_loop", run)
    review.run_writer_tool_loop(job, "原始任務", ["note_general"], 20000)
    assert len(seen) == 1


def test_a_thin_note_triggers_a_round_that_asks_for_an_append(job, monkeypatch):
    run, seen = writer_stub(job.dir, [THIN, FULL])
    monkeypatch.setattr(review, "run_tool_loop", run)
    review.run_writer_tool_loop(job, "原始任務", ["note_general"], 20000)

    assert len(seen) == 2, "薄的產出應該觸發第二輪"
    nudge = seen[1]
    assert "寫得太短" in nudge
    assert 'mode="append"' in nudge
    assert "不要用 overwrite 重寫整份" in nudge
    assert "900" in nudge and "3,000" in nudge, "要把實際字數與門檻講清楚"
    assert "原始任務" in nudge, "續寫時規格與禁則必須一起帶過去"


def test_a_note_that_stays_thin_is_delivered_with_a_warning_not_an_error(job, monkeypatch, capsys):
    run, _ = writer_stub(job.dir, [THIN, THIN, THIN])
    monkeypatch.setattr(review, "run_tool_loop", run)
    review.run_writer_tool_loop(job, "原始任務", ["note_general"], 20000)
    out = capsys.readouterr().out
    assert "WARNING" in out and "note_general.md" in out
    assert (job.dir / "note_general.md").exists()


def test_a_missing_file_still_fails_hard_after_the_last_round(job, monkeypatch):
    run, _ = writer_stub(job.dir, [{}, {}, {}])
    monkeypatch.setattr(review, "run_tool_loop", run)
    with pytest.raises(RuntimeError, match="仍缺少檔案"):
        review.run_writer_tool_loop(job, "原始任務", ["note_general"], 20000)


def test_thin_checking_is_skipped_when_source_size_is_unknown(job, monkeypatch):
    """Callers that cannot measure the source must not get spurious retries."""
    run, seen = writer_stub(job.dir, [THIN])
    monkeypatch.setattr(review, "run_tool_loop", run)
    review.run_writer_tool_loop(job, "原始任務", ["note_general"], 0)
    assert len(seen) == 1


# --------------------------------------------------------------------------- #
# the prompt half of the fix
# --------------------------------------------------------------------------- #
def test_strategy_block_names_the_real_deliverable():
    block = write_strategy_block(["note_client"], 20709)
    assert 'write_file(path="note_client.md", mode="overwrite")' in block
    assert "20,709" in block


def test_strategy_block_forbids_appending_json():
    assert "JSON 不能 append" in write_strategy_block(["note_general"], 20000)


def test_strategy_block_survives_an_empty_stem_list():
    assert write_strategy_block([], 20000)
