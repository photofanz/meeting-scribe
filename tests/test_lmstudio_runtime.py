from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import lmstudio_runtime  # noqa: E402


class LMStudioRuntimeStatusTests(unittest.TestCase):
    def test_status_blocks_unload_when_only_foreign_model_is_loaded(self):
        acfg = {'backend': 'openai_compat', 'model': 'gpt-oss-120b', 'api': {'base_url': 'http://example/v1'}}
        fake_models = [
            {'key': 'gpt-oss-120b', 'loaded_instances': []},
            {'key': 'qwen3.6-27b-mlx', 'loaded_instances': [{'id': 'qwen3.6-27b-mlx'}]},
        ]
        with mock.patch.object(lmstudio_runtime, 'cleanup_cfg', return_value={'mode': 'idle_eject', 'idle_minutes': 15, 'unload_endpoint': '/api/v1/models/unload', 'allow_unload_all': False}), \
             mock.patch.object(lmstudio_runtime, 'read_activity', return_value={}), \
             mock.patch.object(lmstudio_runtime, '_active_private_jobs', return_value=[]), \
             mock.patch.object(lmstudio_runtime, 'list_models', return_value=fake_models), \
             mock.patch.object(lmstudio_runtime, '_loaded_model_ids', return_value=['qwen3.6-27b-mlx']):
            st = lmstudio_runtime.status(acfg)

        self.assertFalse(st['selected_model_loaded'])
        self.assertEqual(st['foreign_loaded_models'], ['qwen3.6-27b-mlx'])
        self.assertFalse(st['can_unload_now'])

    def test_unload_now_refuses_to_unload_foreign_loaded_model(self):
        acfg = {'backend': 'openai_compat', 'model': 'gpt-oss-120b', 'api': {'base_url': 'http://example/v1'}}
        with mock.patch.object(lmstudio_runtime, 'cleanup_cfg', return_value={'mode': 'idle_eject', 'idle_minutes': 15, 'unload_endpoint': '/api/v1/models/unload', 'allow_unload_all': False}), \
             mock.patch.object(lmstudio_runtime, '_model_instances', return_value=[]), \
             mock.patch.object(lmstudio_runtime, '_loaded_model_ids', return_value=['qwen3.6-27b-mlx']):
            with self.assertRaisesRegex(RuntimeError, 'configured model is not currently loaded'):
                lmstudio_runtime.unload_now(acfg, reason='test')


if __name__ == '__main__':
    unittest.main()
