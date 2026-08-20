"""A transport failure must never be reported as a schema failure.

The writer stage used to swallow HTTP errors from the inference service and
then complain that the model's answer "was not a JSON object with files",
which sent debugging in exactly the wrong direction. These tests pin the
distinction: transport errors surface the service's own words, and the
schema check only runs once a real answer came back.
"""
from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import agent_note  # noqa: E402
import lmstudio_runtime  # noqa: E402

ACFG = {"api": {"base_url": "http://stub/v1", "api_key": "k"}}

OVERLOAD = json.dumps({
    "error": {
        "message": 'Failed to load model "gpt-oss-120b". Error: Model loading '
                   "was stopped due to insufficient system resources.",
        "type": "invalid_request_error",
    }
})


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://stub/v1/chat/completions", code, "err", {},
        io.BytesIO(body.encode("utf-8")),
    )


class ApiTransportErrorTests(unittest.TestCase):
    def setUp(self):
        self.log = Path(tempfile.mkdtemp()) / "agent.log"

    def _call(self, side_effect):
        with mock.patch.object(agent_note.urllib.request, "urlopen",
                               side_effect=side_effect):
            return agent_note._run_openai_compat("p", "gpt-oss-120b", self.log, 5, ACFG)

    def test_http_error_reports_the_services_own_message(self):
        rc, _, text = self._call(_http_error(400, OVERLOAD))
        self.assertEqual(rc, 400)
        self.assertEqual(text, "")
        err = agent_note.last_api_error()
        self.assertEqual(err["kind"], "http_error")
        self.assertEqual(err["code"], 400)
        self.assertIn("insufficient system resources", err["detail"])
        self.assertIn("insufficient system resources",
                      agent_note.describe_api_error(err))

    def test_unreachable_service_is_distinguished_from_bad_output(self):
        rc, _, _ = self._call(urllib.error.URLError("Connection refused"))
        self.assertEqual(rc, 1)
        self.assertEqual(agent_note.last_api_error()["kind"], "unreachable")

    def test_non_json_body_is_distinguished_from_missing_files_key(self):
        resp = mock.MagicMock()
        resp.read.return_value = b"<html>502 Bad Gateway</html>"
        resp.status = 200
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda *a: False
        with mock.patch.object(agent_note.urllib.request, "urlopen", return_value=resp):
            agent_note._run_openai_compat("p", "m", self.log, 5, ACFG)
        self.assertEqual(agent_note.last_api_error()["kind"], "not_json")

    def test_successful_call_clears_the_error(self):
        body = json.dumps({"choices": [{"message": {"content": "hello"}}]})
        resp = mock.MagicMock()
        resp.read.return_value = body.encode("utf-8")
        resp.status = 200
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda *a: False
        with mock.patch.object(agent_note.urllib.request, "urlopen", return_value=resp):
            rc, _, text = agent_note._run_openai_compat("p", "m", self.log, 5, ACFG)
        self.assertEqual(rc, 200)
        self.assertEqual(text, "hello")
        self.assertIsNone(agent_note.last_api_error())


class PreflightTests(unittest.TestCase):
    def test_missing_model_lists_what_is_available(self):
        models = [{"key": "gemma-4-31b-it-mlx", "loaded": False}]
        with mock.patch.object(lmstudio_runtime, "list_models", return_value=models):
            ok, why = lmstudio_runtime.preflight("gpt-oss-120b", ACFG)
        self.assertFalse(ok)
        self.assertIn("找不到模型", why)
        self.assertIn("gemma-4-31b-it-mlx", why)

    def test_load_failure_quotes_lm_studio(self):
        models = [{"key": "gpt-oss-120b", "loaded": False}]
        with mock.patch.object(lmstudio_runtime, "list_models", return_value=models), \
             mock.patch.object(lmstudio_runtime, "load_model",
                               side_effect=_http_error(500, OVERLOAD)):
            ok, why = lmstudio_runtime.preflight("gpt-oss-120b", ACFG)
        self.assertFalse(ok)
        self.assertIn("insufficient system resources", why)

    def test_already_loaded_model_passes_without_reloading(self):
        models = [{"key": "gemma-4-31b-it-mlx", "loaded": True}]
        with mock.patch.object(lmstudio_runtime, "list_models", return_value=models), \
             mock.patch.object(lmstudio_runtime, "load_model") as loader:
            ok, why = lmstudio_runtime.preflight("gemma-4-31b-it-mlx", ACFG)
        self.assertTrue(ok)
        loader.assert_not_called()

    def test_unreachable_service_is_named(self):
        with mock.patch.object(lmstudio_runtime, "list_models",
                               side_effect=urllib.error.URLError("refused")):
            ok, why = lmstudio_runtime.preflight("any", ACFG)
        self.assertFalse(ok)
        self.assertIn("連不上 LM Studio", why)

    def test_empty_model_key_is_rejected_before_any_network_call(self):
        with mock.patch.object(lmstudio_runtime, "list_models") as lister:
            ok, why = lmstudio_runtime.preflight("", ACFG)
        self.assertFalse(ok)
        lister.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ConcurrentErrorReportingTests(unittest.TestCase):
    """Each thread must read back its own failure, not a neighbour's.

    The evidence pipeline runs S2 chunks and S4 sections in parallel, and every
    one of those calls reports failures through the same `last_api_error()`.
    While that detail lived in a module global, a second thread entering
    `_run_openai_compat` cleared it before the first thread had read it — so a
    failed chunk printed either a blank reason or somebody else's HTTP error,
    and the job that stopped told you the wrong thing about why. Raising
    max_parallel widens that window on every call.
    """

    def setUp(self):
        self.log = Path(tempfile.mkdtemp()) / "agent.log"

    def _run(self, count: int, fails: set[int]) -> dict[int, dict | None]:
        entered = threading.Barrier(count)
        recorded = threading.Barrier(count)
        marker_of: dict[int, int] = {}
        seen: dict[int, dict | None] = {}

        def fake_urlopen(req, timeout=None):
            slot = marker_of[threading.get_ident()]
            entered.wait(10)          # every thread is now inside the call
            if slot in fails:
                raise _http_error(400, json.dumps({"error": {"message": f"boom-{slot}"}}))
            body = json.dumps({"choices": [{"message": {"content": f"ok-{slot}"}}]})
            resp = mock.MagicMock()
            resp.read.return_value = body.encode("utf-8")
            resp.status = 200
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda *a: False
            return resp

        def worker(slot: int) -> None:
            marker_of[threading.get_ident()] = slot
            agent_note._run_openai_compat("p", "m", self.log, 5, ACFG)
            recorded.wait(10)         # every thread has now recorded its outcome
            seen[slot] = agent_note.last_api_error()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        # marker_of must be populated before any fake_urlopen runs, and worker
        # does that as its first statement, before the barrier lets anyone in.
        with mock.patch.object(agent_note.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(len(seen), count, "a worker thread did not finish")
        return seen

    def test_every_failing_thread_reads_back_its_own_error(self):
        count = 16
        seen = self._run(count, fails=set(range(count)))
        for slot, err in sorted(seen.items()):
            self.assertIsNotNone(err, f"slot {slot} lost its error entirely")
            self.assertIn(f"boom-{slot}", err["detail"],
                          f"slot {slot} read back {err['detail']!r}")

    def test_a_successful_thread_does_not_inherit_a_neighbours_failure(self):
        count = 16
        fails = {i for i in range(count) if i % 2}
        seen = self._run(count, fails=fails)
        for slot, err in sorted(seen.items()):
            if slot in fails:
                self.assertIn(f"boom-{slot}", (err or {}).get("detail", ""))
            else:
                self.assertIsNone(err, f"slot {slot} inherited {err!r}")


class ConcurrentLogAttributionTests(unittest.TestCase):
    """Two calls in flight at once must still be readable apart in the log.

    When S2 gives up it tells the operator to go read this log, and that is the
    only place the service's own words survive. But every call in a stage
    appends to the same file, with the same model name, and the request header
    is flushed at the start while the outcome is written at the end — so with
    workers in flight the outcome of one call lands under the header of
    another. The log stays plausible and stops being true, which is the worst
    way for it to fail. Each call carries an id so its lines can be gathered
    again.
    """

    def setUp(self):
        self.log = Path(tempfile.mkdtemp()) / "agent.log"

    def test_each_calls_lines_can_be_traced_back_to_that_call(self):
        count = 8
        entered = threading.Barrier(count)
        reply_of: dict[int, str] = {}

        def fake_urlopen(req, timeout=None):
            entered.wait(10)   # hold every call open at once, headers written
            body = json.loads(req.data.decode("utf-8"))
            # The prompt is the only thing that differs — same model, as in S4.
            slot = int(body["messages"][-1]["content"])
            text = "x" * (100 + slot)
            reply_of[slot] = text
            resp = mock.MagicMock()
            resp.read.return_value = json.dumps(
                {"choices": [{"message": {"content": text}}]}).encode("utf-8")
            resp.status = 200
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda *a: False
            return resp

        def worker(slot: int) -> None:
            agent_note._run_openai_compat(str(slot), "m", self.log, 5, ACFG)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        with mock.patch.object(agent_note.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)

        lines = self.log.read_text().splitlines()
        ids = re.compile(r"\[agent ([^\]]+)\]")
        by_call: dict[str, list[str]] = {}
        for line in lines:
            found = ids.search(line)
            if found:
                by_call.setdefault(found.group(1), []).append(line)
        self.assertEqual(len(by_call), count,
                         f"expected {count} distinguishable calls, got {sorted(by_call)}")
        sizes = set()
        for cid, own in by_call.items():
            got = [ln for ln in own if "received" in ln]
            self.assertEqual(len(got), 1, f"call {cid} has {len(got)} outcomes: {own}")
            sizes.add(int(re.search(r"received (\d+) chars", got[0]).group(1)))
        self.assertEqual(sizes, {len(t) for t in reply_of.values()},
                         "an outcome was recorded against the wrong call")
