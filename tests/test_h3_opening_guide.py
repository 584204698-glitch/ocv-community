import copy
import unittest
from unittest.mock import Mock, patch

from backend.app.comfyui_bridge import WorkflowMappings, _patch_opening_guide, _patch_voice_references, _require_opening_guide_support
from tests.test_comfyui_video_inputs import profile


class OpeningGuideTests(unittest.TestCase):
    def graph(self):
        preset = profile('composition_reference', 2)
        preset['workflow']['10']['inputs']['vae'] = ['vae', 0]
        preset['workflow']['guider'] = {'class_type': 'BasicGuider', 'inputs': {'conditioning': ['10', 0]}}
        preset['workflow']['sampler'] = {'class_type': 'SamplerCustomAdvanced', 'inputs': {'latent_image': ['10', 1]}}
        return preset['workflow'], WorkflowMappings.model_validate(preset['mappings'])

    def test_guided_core_preserves_role_images_voice_order_and_latent(self):
        graph, mappings = self.graph()
        _patch_voice_references(graph, mappings, ['robot.wav', 'girl.wav'])
        refs = copy.deepcopy(graph['10']['inputs'])
        _patch_opening_guide(graph, mappings, enabled=True)
        self.assertEqual(graph['10']['inputs'], refs)
        self.assertEqual(graph['sampler']['inputs']['latent_image'], ['10', 1])
        self.assertEqual(graph['guider']['inputs']['conditioning'], ['ocv_core_opening_guide', 0])
        guide = graph['ocv_core_opening_guide']['inputs']
        self.assertEqual(guide['image'], ['1', 0])
        self.assertEqual(guide['positive'], ['10', 0])
        self.assertEqual(guide['frame_idx'], 0)

    def test_disabled_option_keeps_legacy_graph_exact(self):
        graph, mappings = self.graph()
        original = copy.deepcopy(graph)
        self.assertIs(_patch_opening_guide(graph, mappings), graph)
        self.assertEqual(graph, original)

    def test_existing_guides_and_invalid_core_connections_cannot_be_overwritten(self):
        graph, mappings = self.graph()
        _patch_opening_guide(graph, mappings, enabled=True)
        with self.assertRaisesRegex(ValueError, '已包含'):
            _patch_opening_guide(graph, mappings, enabled=True)
        graph, mappings = self.graph()
        graph['10']['inputs']['ref_images.ref_image_0'] = ['2', 0]
        with self.assertRaisesRegex(ValueError, '核心图'):
            _patch_opening_guide(graph, mappings, enabled=True)

    def test_first_last_mode_is_not_silently_reinterpreted(self):
        preset = profile('first_last')
        with self.assertRaisesRegex(ValueError, '首尾帧'):
            _patch_opening_guide(preset['workflow'], WorkflowMappings.model_validate(preset['mappings']), enabled=True)

    def test_missing_server_guide_names_recovery_action(self):
        response = Mock()
        response.json.return_value = {}
        with patch('backend.app.comfyui_bridge.requests.get', return_value=response):
            with self.assertRaisesRegex(ValueError, '关闭开场图约束继续制作'):
                _require_opening_guide_support({'base_url': 'http://comfy.test'})
        response.json.return_value = {'MiniMaxH3AddGuide': {'input': {}}}
        with patch('backend.app.comfyui_bridge.requests.get', return_value=response) as get:
            _require_opening_guide_support({'base_url': 'http://comfy.test'})
            self.assertEqual(get.call_args.args[0], 'http://comfy.test/object_info/MiniMaxH3AddGuide')


if __name__ == '__main__':
    unittest.main()
