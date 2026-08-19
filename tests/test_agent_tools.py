"""Tool-loop sandbox and protocol tests.

These never touch a real inference server: the transport is stubbed, so the
suite runs with LM Studio switched off.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import agent_tools  # noqa: E402
from agent_tools import ToolLoopError, _dispatch, _parse_tool_calls, run_tool_loop  # noqa: E402


@pytest.fixture()
def job(tmp_path):
    d = tmp_path / "job"
    d.mkdir()
    (d / "transcript_clean.md").write_text("\n".join(f"第 {i} 行內容" for i in range(1, 1001)))
    (d / "meta.json").write_text(json.dumps({"want_note": True}, ensure_ascii=False))
    return d


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #
def test_read_outside_sandbox_is_refused(job, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    out, ok = _dispatch("read_file", {"path": str(secret)}, job, [], [])
    assert not ok
    assert "不在允許範圍內" in out


def test_write_outside_sandbox_is_refused(job, tmp_path):
    target = tmp_path / "escape.md"
    out, ok = _dispatch("write_file", {"path": str(target), "content": "x"}, job, [], [])
    assert not ok
    assert not target.exists()


def test_parent_traversal_is_refused(job):
    out, ok = _dispatch("write_file", {"path": "../escape.md", "content": "x"}, job, [], [])
    assert not ok


def test_template_dir_is_readable_but_not_writable(job):
    templates = ROOT / "templates"
    out, ok = _dispatch("read_file", {"path": str(templates / "NOTE_SPECS.md")}, job, [templates], [])
    assert ok
    out, ok = _dispatch("write_file", {"path": str(templates / "pwn.md"), "content": "x"}, job, [templates], [])
    assert not ok
    assert not (templates / "pwn.md").exists()


# --------------------------------------------------------------------------- #
# read semantics
# --------------------------------------------------------------------------- #
def test_read_reports_remaining_lines(job):
    out, ok = _dispatch("read_file", {"path": "transcript_clean.md", "offset": 1, "limit": 100}, job, [], [])
    assert ok
    assert "全檔共 1000 行" in out
    assert "還有 900 行未讀" in out
    assert "offset=101" in out
    assert "第 100 行內容" in out
    assert "第 101 行內容" not in out


def test_read_to_end_says_so(job):
    out, ok = _dispatch("read_file", {"path": "transcript_clean.md", "offset": 950, "limit": 200}, job, [], [])
    assert ok
    assert "已讀到檔尾" in out


def test_read_missing_file_is_an_error_not_a_crash(job):
    out, ok = _dispatch("read_file", {"path": "nope.md"}, job, [], [])
    assert not ok
    assert "檔案不存在" in out


# --------------------------------------------------------------------------- #
# write semantics
# --------------------------------------------------------------------------- #
def test_write_records_filename(job):
    written = []
    out, ok = _dispatch("write_file", {"path": "note_general.md", "content": "# 標題\n內容"}, job, [], written)
    assert ok
    assert written == ["note_general.md"]
    assert (job / "note_general.md").read_text() == "# 標題\n內容"


def test_bad_json_is_rejected_so_the_model_can_retry(job):
    out, ok = _dispatch("write_file", {"path": "action_items.json", "content": "{oops"}, job, [], [])
    assert not ok
    assert "不是合法 JSON" in out
    assert not (job / "action_items.json").exists()


def test_json_handed_back_as_an_object_is_accepted(job):
    out, ok = _dispatch("write_file", {"path": "action_items.json", "content": {"actions": []}}, job, [], [])
    assert ok
    assert json.loads((job / "action_items.json").read_text()) == {"actions": []}


# --------------------------------------------------------------------------- #
# append semantics
# --------------------------------------------------------------------------- #
def test_append_extends_instead_of_replacing(job):
    written = []
    _dispatch("write_file", {"path": "note_general.md", "content": "# 會議記錄\n\n## 一、重點"},
              job, [], written)
    out, ok = _dispatch("write_file",
                        {"path": "note_general.md", "content": "## 二、議題討論", "mode": "append"},
                        job, [], written)
    assert ok
    assert (job / "note_general.md").read_text() == "# 會議記錄\n\n## 一、重點\n\n## 二、議題討論"
    assert written == ["note_general.md"], "同一個檔案不該被記錄兩次"


def test_append_reports_running_total_so_the_model_can_track_progress(job):
    _dispatch("write_file", {"path": "note_general.md", "content": "a" * 100}, job, [], [])
    out, ok = _dispatch("write_file",
                        {"path": "note_general.md", "content": "b" * 50, "mode": "append"},
                        job, [], [])
    assert ok
    assert "本次 50 字元" in out
    assert "全檔 152 字元" in out  # 100 + "\n\n" + 50
    assert "append" in out


def test_append_to_a_new_file_just_creates_it(job):
    out, ok = _dispatch("write_file", {"path": "note_general.md", "content": "開頭", "mode": "append"},
                        job, [], [])
    assert ok
    assert (job / "note_general.md").read_text() == "開頭"


def test_overwrite_is_still_the_default(job):
    _dispatch("write_file", {"path": "note_general.md", "content": "舊的"}, job, [], [])
    _dispatch("write_file", {"path": "note_general.md", "content": "新的"}, job, [], [])
    assert (job / "note_general.md").read_text() == "新的"


def test_json_cannot_be_appended_because_half_a_json_file_is_worthless(job):
    _dispatch("write_file", {"path": "action_items.json", "content": '{"actions": []}'}, job, [], [])
    out, ok = _dispatch("write_file",
                        {"path": "action_items.json", "content": '{"more": 1}', "mode": "append"},
                        job, [], [])
    assert not ok
    assert "不能用 append" in out
    assert json.loads((job / "action_items.json").read_text()) == {"actions": []}


def test_unknown_mode_is_refused_rather_than_silently_overwriting(job):
    (job / "note_general.md").write_text("原本的內容")
    out, ok = _dispatch("write_file",
                        {"path": "note_general.md", "content": "x", "mode": "replace"},
                        job, [], [])
    assert not ok
    assert (job / "note_general.md").read_text() == "原本的內容"


def test_overwrite_result_nudges_towards_appending_the_rest(job):
    out, ok = _dispatch("write_file", {"path": "note_general.md", "content": "# 標題"}, job, [], [])
    assert ok
    assert 'mode="append"' in out


def test_json_write_does_not_nudge_towards_appending(job):
    out, ok = _dispatch("write_file", {"path": "action_items.json", "content": "{}"}, job, [], [])
    assert ok
    assert "append" not in out


def test_unknown_tool_is_reported_to_the_model(job):
    out, ok = _dispatch("delete_everything", {}, job, [], [])
    assert not ok
    assert "沒有名為" in out


# --------------------------------------------------------------------------- #
# protocol
# --------------------------------------------------------------------------- #
def test_tool_call_arguments_parse_from_string_or_object():
    calls = _parse_tool_calls({"tool_calls": [
        {"id": "a", "function": {"name": "read_file", "arguments": '{"path":"x.md"}'}},
        {"id": "b", "function": {"name": "read_file", "arguments": {"path": "y.md"}}},
    ]})
    assert [c["args"]["path"] for c in calls] == ["x.md", "y.md"]


def _fake_server(script, seen=None):
    """Return a _post stub that replays `script` and records request bodies."""
    steps = iter(script)

    def _post(url, api_key, body, timeout):
        if seen is not None:
            seen.append(body)
        return next(steps)

    return _post


def _msg(content=None, tool_calls=None, finish="stop"):
    return {"choices": [{"finish_reason": finish,
                         "message": {"content": content, "tool_calls": tool_calls or []}}]}


def _call(cid, name, args):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def test_loop_executes_tools_then_stops(job, monkeypatch, tmp_path):
    monkeypatch.setattr(agent_tools, "_post", _fake_server([
        _msg(tool_calls=[_call("1", "read_file", {"path": "transcript_clean.md", "limit": 10})],
             finish="tool_calls"),
        _msg(tool_calls=[_call("2", "write_file", {"path": "note_general.md", "content": "# 會議記錄"})],
             finish="tool_calls"),
        _msg(content="完成"),
    ]))
    rc, elapsed, summary = run_tool_loop(
        "任務", "m", {"api": {}}, job_dir=job, read_roots=[],
        log_path=tmp_path / "log.txt", timeout=10, max_steps=10,
    )
    assert rc == 0
    assert summary["steps"] == 3
    assert summary["tool_calls"] == 2
    assert summary["written"] == ["note_general.md"]
    assert summary["final_text"] == "完成"
    assert (job / "note_general.md").exists()


def test_loop_feeds_tool_results_back_as_tool_messages(job, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(agent_tools, "_post", _fake_server([
        _msg(tool_calls=[_call("abc", "read_file", {"path": "transcript_clean.md", "limit": 5})],
             finish="tool_calls"),
        _msg(content="done"),
    ], seen))
    run_tool_loop("任務", "m", {"api": {}}, job_dir=job, read_roots=[],
                  log_path=tmp_path / "log.txt", timeout=10, max_steps=10)
    second = seen[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert second[-1]["role"] == "tool"
    assert second[-1]["tool_call_id"] == "abc"
    assert "第 1 行內容" in second[-1]["content"]


def test_tool_errors_are_returned_to_the_model_not_raised(job, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(agent_tools, "_post", _fake_server([
        _msg(tool_calls=[_call("1", "read_file", {"path": "/etc/passwd"})], finish="tool_calls"),
        _msg(content="ok"),
    ], seen))
    rc, _, summary = run_tool_loop("任務", "m", {"api": {}}, job_dir=job, read_roots=[],
                                   log_path=tmp_path / "log.txt", timeout=10, max_steps=10)
    assert rc == 0
    assert summary["tool_errors"] == 1
    assert "錯誤" in seen[1]["messages"][-1]["content"]


def test_max_steps_returns_124_rather_than_looping_forever(job, monkeypatch, tmp_path):
    def _post(url, api_key, body, timeout):
        return _msg(tool_calls=[_call("1", "list_files", {})], finish="tool_calls")

    monkeypatch.setattr(agent_tools, "_post", _post)
    rc, _, summary = run_tool_loop("任務", "m", {"api": {}}, job_dir=job, read_roots=[],
                                   log_path=tmp_path / "log.txt", timeout=10, max_steps=4)
    assert rc == 124
    assert summary["steps"] == 4


def test_tools_are_advertised_on_every_request(job, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(agent_tools, "_post", _fake_server([_msg(content="done")], seen))
    run_tool_loop("任務", "m", {"api": {}}, job_dir=job, read_roots=[],
                  log_path=tmp_path / "log.txt", timeout=10, max_steps=5)
    names = {t["function"]["name"] for t in seen[0]["tools"]}
    assert names == {"read_file", "write_file", "list_files"}


def test_write_file_advertises_append_mode(job, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(agent_tools, "_post", _fake_server([_msg(content="done")], seen))
    run_tool_loop("任務", "m", {"api": {}}, job_dir=job, read_roots=[],
                  log_path=tmp_path / "log.txt", timeout=10, max_steps=5)
    write = next(t for t in seen[0]["tools"] if t["function"]["name"] == "write_file")
    mode = write["function"]["parameters"]["properties"]["mode"]
    assert mode["enum"] == ["overwrite", "append"]
    assert "mode" not in write["function"]["parameters"]["required"]


def test_loop_can_build_one_document_across_several_appends(job, monkeypatch, tmp_path):
    """The whole point of mode=append: a document longer than one reply."""
    monkeypatch.setattr(agent_tools, "_post", _fake_server([
        _msg(tool_calls=[_call("1", "write_file",
                               {"path": "note_general.md", "content": "## 一、重點"})],
             finish="tool_calls"),
        _msg(tool_calls=[_call("2", "write_file",
                               {"path": "note_general.md", "content": "## 二、議題", "mode": "append"})],
             finish="tool_calls"),
        _msg(tool_calls=[_call("3", "write_file",
                               {"path": "note_general.md", "content": "## 三、決議", "mode": "append"})],
             finish="tool_calls"),
        _msg(content="完成"),
    ]))
    rc, _, summary = run_tool_loop("任務", "m", {"api": {}}, job_dir=job, read_roots=[],
                                   log_path=tmp_path / "log.txt", timeout=10, max_steps=10)
    assert rc == 0
    assert summary["written"] == ["note_general.md"]
    assert (job / "note_general.md").read_text() == "## 一、重點\n\n## 二、議題\n\n## 三、決議"


def test_transport_failure_raises_instead_of_looking_like_a_bad_answer(job, monkeypatch, tmp_path):
    def _post(url, api_key, body, timeout):
        raise ToolLoopError("推論服務回應 HTTP 500：insufficient system resources")

    monkeypatch.setattr(agent_tools, "_post", _post)
    with pytest.raises(ToolLoopError, match="insufficient system resources"):
        run_tool_loop("任務", "m", {"api": {}}, job_dir=job, read_roots=[],
                      log_path=tmp_path / "log.txt", timeout=10, max_steps=5)
