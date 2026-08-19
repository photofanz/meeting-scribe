#!/usr/bin/env python3
"""Admin LM Studio endpoints: model selection must survive a failed load, and
a foreign workload must not be able to deadlock model switching.

Everything talking to LM Studio is stubbed, so this runs with the inference box
switched off. No config.json on disk is touched: save_private_model is stubbed
too.
"""
import json
import sys
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import upload_server as us  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def http_error(code: int, message: str) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"message": message}}).encode()
    return urllib.error.HTTPError("http://stub/api/v1/models/load", code,
                                  "err", {}, BytesIO(body))


def status_payload(**over) -> dict:
    base = {
        "backend": "openai_compat",
        "model": "target-model",
        "cleanup": {"mode": "idle_eject", "idle_minutes": 15},
        "activity": {},
        "loaded_models": [],
        "loaded_count": 0,
        "available_models": [
            {"key": "target-model", "loaded": False, "loaded_instances": []},
            {"key": "other-model", "loaded": False, "loaded_instances": []},
        ],
        "available_count": 2,
        "selected_model_loaded": False,
        "selected_model_instances": [],
        "foreign_loaded_models": [],
        "foreign_loaded_count": 0,
        "active_private_jobs": [],
        "active_count": 0,
        "can_unload_now": False,
    }
    base.update(over)
    return base


class AdminLMStudioAPITest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(us.app)
        self.k = {"k": us.TOKEN}
        self.saved: list[str] = []
        self.calls: list[tuple] = []
        self._orig = {
            "status": us.lmstudio_status_payload,
            "save": us.save_private_model,
            "load": us.lmstudio_runtime.load_model,
            "unload_now": us.lmstudio_runtime.unload_now,
            "unload_instance": us.lmstudio_runtime.unload_instance,
        }

        def save(model):
            self.saved.append(model)
            return model

        us.save_private_model = save

    def tearDown(self):
        us.lmstudio_status_payload = self._orig["status"]
        us.save_private_model = self._orig["save"]
        us.lmstudio_runtime.load_model = self._orig["load"]
        us.lmstudio_runtime.unload_now = self._orig["unload_now"]
        us.lmstudio_runtime.unload_instance = self._orig["unload_instance"]

    # -- selection persistence ------------------------------------------
    def test_failed_load_still_saves_the_selection(self):
        us.lmstudio_status_payload = lambda: status_payload()

        def boom(model, *a, **kw):
            raise http_error(500, "Model loading was stopped due to "
                                  "insufficient system resources")

        us.lmstudio_runtime.load_model = boom

        r = self.client.post("/api/admin/lmstudio/select-load",
                             params=self.k, json={"model": "other-model"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(self.saved, ["other-model"], "選擇必須先存下來")
        self.assertEqual(body["saved_model"], "other-model")
        self.assertFalse(body["loaded"])
        self.assertIn("insufficient system resources", body["load_error"])
        self.assertIn("HTTP 500", body["load_error"])

    def test_failed_load_points_at_the_foreign_model_holding_memory(self):
        us.lmstudio_status_payload = lambda: status_payload(
            loaded_models=["someone-elses-30b"],
            foreign_loaded_models=["someone-elses-30b"],
            foreign_loaded_count=1,
        )
        us.lmstudio_runtime.load_model = lambda *a, **kw: (_ for _ in ()).throw(
            http_error(500, "insufficient system resources"))

        r = self.client.post("/api/admin/lmstudio/select-load",
                             params=self.k, json={"model": "other-model"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("someone-elses-30b", r.json()["load_error"])
        self.assertIn("釋放外部模型", r.json()["load_error"])

    def test_successful_load_reports_loaded(self):
        us.lmstudio_status_payload = lambda: status_payload()
        us.lmstudio_runtime.load_model = lambda m, *a, **kw: {"ok": True, "instance_id": m}

        r = self.client.post("/api/admin/lmstudio/select-load",
                             params=self.k, json={"model": "other-model"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["loaded"])
        self.assertEqual(r.json()["load_error"], "")
        self.assertEqual(self.saved, ["other-model"])

    def test_unknown_model_is_rejected_and_not_saved(self):
        us.lmstudio_status_payload = lambda: status_payload()
        r = self.client.post("/api/admin/lmstudio/select-load",
                             params=self.k, json={"model": "no-such-model"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.saved, [])

    def test_active_private_job_still_blocks_switching(self):
        us.lmstudio_status_payload = lambda: status_payload(
            active_private_jobs=["job-1"], active_count=1)
        r = self.client.post("/api/admin/lmstudio/select-load",
                             params=self.k, json={"model": "other-model"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.saved, [])

    # -- foreign unload / deadlock --------------------------------------
    def test_foreign_model_can_be_unloaded_when_named_and_confirmed(self):
        us.lmstudio_status_payload = lambda: status_payload(
            loaded_models=["someone-elses-30b"],
            foreign_loaded_models=["someone-elses-30b"],
            foreign_loaded_count=1,
        )

        def unload(instance_id, *a, **kw):
            self.calls.append(("unload_instance", instance_id))
            return {"ok": True, "instance_id": instance_id}

        us.lmstudio_runtime.unload_instance = unload

        r = self.client.post("/api/admin/lmstudio/unload", params=self.k,
                             json={"instance_id": "someone-elses-30b", "confirm": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["forced"])
        self.assertEqual(self.calls, [("unload_instance", "someone-elses-30b")])

    def test_foreign_unload_requires_confirm(self):
        us.lmstudio_status_payload = lambda: status_payload(
            loaded_models=["someone-elses-30b"],
            foreign_loaded_models=["someone-elses-30b"],
            foreign_loaded_count=1,
        )
        us.lmstudio_runtime.unload_instance = lambda *a, **kw: self.calls.append("BAD")
        r = self.client.post("/api/admin/lmstudio/unload", params=self.k,
                             json={"instance_id": "someone-elses-30b"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_foreign_unload_rejects_a_model_that_is_not_loaded(self):
        us.lmstudio_status_payload = lambda: status_payload(
            loaded_models=["someone-elses-30b"],
            foreign_loaded_models=["someone-elses-30b"],
            foreign_loaded_count=1,
        )
        us.lmstudio_runtime.unload_instance = lambda *a, **kw: self.calls.append("BAD")
        r = self.client.post("/api/admin/lmstudio/unload", params=self.k,
                             json={"instance_id": "ghost-model", "confirm": True})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.calls, [])

    def test_active_private_job_blocks_even_a_forced_unload(self):
        us.lmstudio_status_payload = lambda: status_payload(
            loaded_models=["someone-elses-30b"],
            foreign_loaded_models=["someone-elses-30b"],
            foreign_loaded_count=1,
            active_private_jobs=["job-1"], active_count=1,
        )
        us.lmstudio_runtime.unload_instance = lambda *a, **kw: self.calls.append("BAD")
        r = self.client.post("/api/admin/lmstudio/unload", params=self.k,
                             json={"instance_id": "someone-elses-30b", "confirm": True})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.calls, [])

    def test_plain_unload_names_the_foreign_model_in_its_refusal(self):
        us.lmstudio_status_payload = lambda: status_payload(
            loaded_models=["someone-elses-30b"],
            foreign_loaded_models=["someone-elses-30b"],
            foreign_loaded_count=1,
        )
        r = self.client.post("/api/admin/lmstudio/unload", params=self.k)
        self.assertEqual(r.status_code, 409)
        self.assertIn("someone-elses-30b", r.json()["error"])

    def test_plain_unload_still_works_for_our_own_model(self):
        us.lmstudio_status_payload = lambda: status_payload(
            loaded_models=["target-model"],
            selected_model_instances=["target-model"],
            selected_model_loaded=True,
            can_unload_now=True,
        )
        us.lmstudio_runtime.unload_now = lambda *a, **kw: {"ok": True}
        r = self.client.post("/api/admin/lmstudio/unload", params=self.k)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["forced"])


class UnloadInstanceTest(unittest.TestCase):
    """lmstudio_runtime.unload_instance targets exactly what it is told to."""

    def test_posts_the_named_instance_id(self):
        rt = us.lmstudio_runtime
        seen = {}

        class Resp:
            def read(self): return b'{"instance_id": "foo-30b"}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=0):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode())
            return Resp()

        orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            out = rt.unload_instance("foo-30b", {"model": "target-model"}, reason="test")
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(seen["body"], {"instance_id": "foo-30b"})
        self.assertEqual(out["instance_id"], "foo-30b")

    def test_empty_instance_id_is_rejected(self):
        with self.assertRaises(ValueError):
            us.lmstudio_runtime.unload_instance("  ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
