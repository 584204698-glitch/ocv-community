import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app import comfyui_bridge as bridge
from backend.app.video_render_options import apply, snapshot
from test_comfyui_video_inputs import audio_profile


class RenderOptionsTests(unittest.TestCase):
    def profile(self):
        profile = audio_profile(direct=True)
        profile['workflow']['sampler'] = {'class_type': 'BasicScheduler', 'inputs': {'steps': 20}}
        profile['mappings'].update(width={'node_id': '10', 'input_name': 'width'},
                                   height={'node_id': '10', 'input_name': 'height'})
        return profile

    def test_options_change_submitted_graph_not_saved_profile(self):
        profile = self.profile()
        original = copy.deepcopy(profile)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / 'image.png'
            image.write_bytes(b'image')
            audio = root / 'audio.wav'
            audio.write_bytes(b'audio')
            history = {'remote': {'outputs': {'20': {'videos': [{'filename': 'result.mp4'}]}}}}
            with patch.object(bridge, 'video_profile', return_value=profile), \
                    patch.object(bridge, '_connection', return_value=bridge.DEFAULT_CONNECTION), \
                    patch.object(bridge, 'recover_guard_submission', return_value=None), \
                    patch.object(bridge, '_upload_reference', side_effect=['image.png', 'audio.wav']), \
                    patch.object(bridge, 'submit_prompt', return_value='remote') as submit, \
                    patch.object(bridge, '_history_payload', return_value=history), \
                    patch.object(bridge.requests, 'get', return_value=Mock(content=b'video')):
                bridge.run_video_profile(7, 'preset', prompt='final', image_path=image,
                                         audio_path=audio, duration=5, ratio='16:9', output_path=root / 'out.mp4',
                                         render_options={'comfyui_steps': 8, 'comfyui_width': 1360, 'comfyui_height': 768})
            graph = submit.call_args.args[1]
        self.assertEqual(graph['sampler']['inputs']['steps'], 8)
        self.assertEqual(graph['10']['inputs']['width'], 1360)
        self.assertEqual(graph['10']['inputs']['height'], 768)
        self.assertEqual(graph['10']['inputs']['audio'], ['4', 0])
        self.assertEqual(graph['4']['inputs']['audio'], 'audio.wav')
        self.assertEqual(profile, original)

    def test_dimensions_require_valid_mappings(self):
        for binding in (None, {'node_id': 'missing', 'input_name': 'width'},
                        {'node_id': '10', 'input_name': 'missing'}):
            profile = self.profile()
            profile['mappings']['width'] = binding
            with self.subTest(binding=binding), self.assertRaisesRegex(ValueError, '映射无效'):
                apply(profile, {'comfyui_width': 1360, 'comfyui_height': 768})

    def test_wrapped_api_graph_supports_overrides(self):
        profile = self.profile()
        profile['workflow'] = {'prompt': profile['workflow']}
        changed = apply(profile, {'comfyui_steps': 8})
        self.assertEqual(changed['workflow']['prompt']['sampler']['inputs']['steps'], 8)

    def test_old_project_preserves_its_existing_workflow(self):
        profile = self.profile()
        self.assertEqual(apply(profile, {}), profile)

    def test_ambiguous_schedulers_need_explicit_mapping(self):
        profile = self.profile()
        profile['workflow']['other'] = copy.deepcopy(profile['workflow']['sampler'])
        with self.assertRaisesRegex(ValueError, '无法唯一'):
            apply(profile, {'comfyui_steps': 8})
        profile['mappings']['steps'] = {'node_id': 'sampler', 'input_name': 'steps'}
        result = apply(profile, {'comfyui_steps': 12})
        self.assertEqual(result['workflow']['sampler']['inputs']['steps'], 12)
        self.assertEqual(result['workflow']['other']['inputs']['steps'], 20)

    def test_invalid_values_rejected(self):
        for values in ({'comfyui_steps': True}, {'comfyui_steps': 0}, {'comfyui_steps': 1.5},
                       {'comfyui_width': 768}, {'comfyui_width': 769, 'comfyui_height': 512}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                snapshot(values)
