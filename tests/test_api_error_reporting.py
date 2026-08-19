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
import sys
import tempfile
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
