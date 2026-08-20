"""S0 capability gate: reject a model that cannot do the job, in seconds.

The failure this guards against is not a crash. `unsloth/muse-glimmer-30b`
loads fine, answers fine, and then spends 2,572 seconds producing a 1,272
character note — because it is a BF16 GGUF with `trained_for_tool_use: false`
that was handed a 60-step tool loop. Every fact needed to predict that is in
LM Studio's own model list, so the gate reads it before the work starts.

The fixtures below are the real `/api/v1/models` metadata for those models as
served on 2026-08-20, trimmed to the fields `list_models()` exposes.
"""
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import lmstudio_runtime  # noqa: E402

ACFG = {"backend": "openai_compat", "api": {"base_url": "http://example/v1"}}

MODELS = [
    {"key": "qwen3.8-27b-mtplx", "format": "mlx", "max_context_length": 262144,
     "tool_use": True, "loaded": False, "loaded_instances": []},
    {"key": "unsloth/muse-glimmer-30b", "format": "gguf", "max_context_length": 131072,
     "tool_use": False, "loaded": False, "loaded_instances": []},
    {"key": "gpt-oss-120b", "format": "mlx", "max_context_length": 131072,
     "tool_use": True, "loaded": False, "loaded_instances": []},
    {"key": "deepseek-v4-flash-0731", "format": "gguf", "max_context_length": 1048576,
     "tool_use": False, "loaded": False, "loaded_instances": []},
]


def check(model, *, need_tools=False, need_ctx=lmstudio_runtime.WRITER_MIN_CTX,
          models=None):
    with mock.patch.object(lmstudio_runtime, "list_models",
                           return_value=list(MODELS if models is None else models)):
        return lmstudio_runtime.capability_check(
            model, need_tools=need_tools, need_ctx=need_ctx, acfg=ACFG)


class CapabilityGateTests(unittest.TestCase):
    def test_tool_free_model_is_rejected_when_the_path_needs_tools(self):
        ok, why = check("unsloth/muse-glimmer-30b", need_tools=True)
        self.assertFalse(ok)
        self.assertIn("trained_for_tool_use=false", why)

    def test_rejection_names_a_usable_model_on_the_same_machine(self):
        _, why = check("unsloth/muse-glimmer-30b", need_tools=True)
        self.assertIn("qwen3.8-27b-mtplx", why)
        # Suggesting another tool-free model would be the same dead end.
        self.assertNotIn("deepseek-v4-flash-0731", why)

    def test_target_model_passes_clean(self):
        ok, why = check("qwen3.8-27b-mtplx", need_tools=True)
        self.assertTrue(ok)
        self.assertNotIn(lmstudio_runtime.CAPABILITY_WARN, why)

    def test_evidence_pipeline_needs_no_tools_so_ctx_and_format_decide(self):
        ok, why = check("unsloth/muse-glimmer-30b", need_tools=False)
        self.assertTrue(ok)
        self.assertTrue(why.startswith(lmstudio_runtime.CAPABILITY_WARN))
        self.assertIn("gguf", why)

    def test_short_context_is_rejected(self):
        small = [{"key": "tiny-8k", "format": "mlx", "max_context_length": 8192,
                  "tool_use": True, "loaded": False, "loaded_instances": []}] + MODELS
        ok, why = check("tiny-8k", models=small)
        self.assertFalse(ok)
        self.assertIn("context", why)
        self.assertIn("qwen3.8-27b-mtplx", why)

    def test_unknown_model_lists_what_is_actually_there(self):
        ok, why = check("does-not-exist")
        self.assertFalse(ok)
        self.assertIn("找不到模型", why)
        self.assertIn("qwen3.8-27b-mtplx", why)

    def test_empty_model_key_is_rejected_without_a_network_call(self):
        with mock.patch.object(lmstudio_runtime, "list_models",
                               side_effect=AssertionError("must not be called")):
            ok, why = lmstudio_runtime.capability_check(
                "", need_tools=False, need_ctx=1, acfg=ACFG)
        self.assertFalse(ok)
        self.assertIn("未設定模型", why)

    def test_unreachable_server_reports_the_endpoint(self):
        with mock.patch.object(lmstudio_runtime, "list_models",
                               side_effect=OSError("connection refused")):
            ok, why = lmstudio_runtime.capability_check(
                "qwen3.8-27b-mtplx", need_tools=False, need_ctx=1, acfg=ACFG)
        self.assertFalse(ok)
        self.assertIn("http://example", why)

    def test_mlx_is_offered_before_gguf(self):
        both = [
            {"key": "slow-gguf", "format": "gguf", "max_context_length": 262144,
             "tool_use": True, "loaded": True, "loaded_instances": [{"id": "slow-gguf"}]},
            {"key": "fast-mlx", "format": "mlx", "max_context_length": 262144,
             "tool_use": True, "loaded": False, "loaded_instances": []},
        ]
        names = lmstudio_runtime._alternatives(
            both, need_tools=True, need_ctx=lmstudio_runtime.WRITER_MIN_CTX)
        self.assertEqual(names[0], "fast-mlx")


class StageWriteWiringTests(unittest.TestCase):
    """The gate is worthless if the write stage does not consult it first."""

    def _job(self, tool_loop):
        job = mock.Mock()
        job.backend = "openai_compat"
        job.model = "unsloth/muse-glimmer-30b"
        job.plan = {"want_note": True, "stems": ["note_general"], "formats": ["md"]}
        job.acfg = {"tool_loop": tool_loop}
        job.dir = Path("/tmp/does-not-matter")
        return job

    def test_write_stage_aborts_before_the_model_is_ever_loaded(self):
        import review

        calls = []
        with mock.patch.object(review, "lm_capability_check",
                               side_effect=lambda *a, **k: (calls.append("cap"), (False, "不合格"))[1]), \
             mock.patch.object(review, "lm_preflight",
                               side_effect=lambda *a, **k: calls.append("preflight") or (True, "")):
            with self.assertRaisesRegex(RuntimeError, "不合格"):
                review.stage_write(self._job(True), deliver=False)

        self.assertEqual(calls, ["cap"], "preflight must not run after a capability failure")

    def test_capability_check_is_told_whether_the_path_needs_tools(self):
        import review

        seen = {}

        def fake(model, *, need_tools, need_ctx, acfg):
            seen.update(need_tools=need_tools, need_ctx=need_ctx)
            return False, "stop here"

        with mock.patch.object(review, "lm_capability_check", side_effect=fake):
            for tool_loop in (True, False):
                with self.assertRaises(RuntimeError):
                    review.stage_write(self._job(tool_loop), deliver=False)
                self.assertIs(seen["need_tools"], tool_loop)
                self.assertEqual(seen["need_ctx"], lmstudio_runtime.WRITER_MIN_CTX)


if __name__ == "__main__":
    unittest.main()
