import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from backend.app import one_click as auto, video_dialogue as dialogue
from backend.app import video_external_dialogue as exact, video_inputs as inputs, video_studio as studio
from tests import test_video_dialogue as fixtures


class SilentExternalEngineTests(unittest.TestCase):
    read = fixtures.ProjectVoiceApiTest.read
    save_cast = fixtures.ProjectVoiceApiTest.save_cast
    bind = fixtures.ProjectVoiceApiTest.bind
    prepared = fixtures.ProjectVoiceApiTest.prepared

    def setUp(self):
        fixtures.ProjectVoiceApiTest.setUp(self)
        _, self.record, self.shot, self.images = self.prepared()
        self.selection = {'input_mode': 'composition_reference'}
        self.record.update(status='video_review', native_story={'text': 'story'}, native_owner_id=1)
        self.record['shots'] = [self.shot]
        self.record['creation_parameters'].update(speech_mode='dialogue', dialogue_route='external_tts',
                                                   dialogue_tts_engine='qwen3')
        self.shot['native_dialogue']['turns'] = []
        guide = self.path / 'silent.wav'
        guide.write_bytes(fixtures.sample_voice(0))
        self.guide = dict(file=guide.name, sha256=hashlib.sha256(guide.read_bytes()).hexdigest(), duration=4)
        def make_track(path, folder, *args):
            folder.mkdir(parents=True, exist_ok=True)
            return self.guide
        with patch.object(exact, 'make_track', side_effect=make_track):
            compiled = dialogue.prepare_dialogue(1, self.path, self.record, self.shot,
                                                 self.selection, self.images, prepare_narration=True)
        self.shot.update(video_status='completed', video='original.mp4', dialogue_stale=True,
                         video_request=dict(dialogue=compiled, external_dialogue=compiled['external_dialogue'],
                                            audio_policy={'mode': 'external_dialogue'}))
        (self.path / 'original.mp4').write_bytes(b'existing-video')
        self.record['creation_parameters']['dialogue_tts_engine'] = 'indextts25'
        for target, name, value in [(inputs, 'resolve_video_inputs', (self.selection, self.images))]:
            handle = patch.object(target, name, return_value=value)
            handle.start(); self.addCleanup(handle.stop)
        handle = patch.object(auto, 'ROOT', self.root / 'runs')
        handle.start(); self.addCleanup(handle.stop)

    def equivalent(self):
        return dialogue.silent_external_engine_equivalent(1, self.path, self.record, self.shot)

    def test_real_compiler_reuses_silent_guide_and_export_without_synthesis_or_mutation(self):
        before = copy.deepcopy(self.record)
        with patch.object(exact, 'synthesize') as synth, patch.object(exact, 'synthesize_qwen') as qwen, \
             patch.object(exact, 'make_track') as track:
            self.assertTrue(self.equivalent())
            self.assertEqual(dialogue.assert_native_export_current(1, self.path, self.record), [])
            synth.assert_not_called(); qwen.assert_not_called(); track.assert_not_called()
        self.assertEqual(self.record, before)

    def test_changed_plan_identity_image_guide_or_spoken_turn_never_equivalent(self):
        original = copy.deepcopy(self.record)
        changes = [
            lambda: self.shot['native_dialogue'].update(scene='A changed scene.'),
            lambda: self.shot['native_dialogue'].update(turns=[dict(character_id=self.shot['native_dialogue']['speakers'][0]['character_id'], text='Hello', start=1, end=2)]),
            lambda: self.shot['native_dialogue']['speakers'][0].update(kind='narrator', on_screen=False),
            lambda: self.shot.update(video_status='pending'),
            lambda: self.shot['video_request']['external_dialogue']['guide'].update(sha256='changed'),
            lambda: self.shot['video_request']['dialogue'].update(fingerprint='tampered'),
            lambda: self.record['creation_parameters'].update(dialogue_route='h3_native'),
            lambda: self.record['project_cast']['characters'][0].update(identity_sha256='changed'),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                self.record = copy.deepcopy(original); self.shot = self.record['shots'][0]
                change()
                self.assertFalse(self.equivalent())
        self.record = copy.deepcopy(original); self.shot = self.record['shots'][0]
        for filename in ('core.png', 'silent.wav'):
            with self.subTest(file=filename):
                file = self.path / filename; original_bytes = file.read_bytes()
                file.write_bytes(b'changed')
                self.assertFalse(self.equivalent())
                file.write_bytes(original_bytes)

    def test_frozen_spoken_or_narrated_basis_is_not_silent_even_if_current_is_silent(self):
        frozen = self.shot['video_request']['dialogue']
        for kind in ('character', 'narrator'):
            with self.subTest(kind=kind):
                basis = copy.deepcopy(frozen['basis'])
                basis['plan']['turns'] = [{'character_id': 'spoken', 'text': 'Old speech'}]
                basis['plan']['speakers'][0]['kind'] = kind
                self.shot['video_request']['dialogue'] = dict(basis=basis, fingerprint=hashlib.sha256(
                    json.dumps(basis, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
                self.assertFalse(self.equivalent())

    def test_resume_saves_only_equivalent_stale_clear_under_existing_lock(self):
        studio.save(self.path, self.record)
        state = auto.register_native(1, self.record); auto._pause(state, 'paused')
        original_request = copy.deepcopy(self.shot['video_request'])
        original_save = studio.save
        def locked_save(path, record):
            self.assertTrue(studio.LOCK._is_owned())
            return original_save(path, record)
        with patch.object(auto, '_owned_job', return_value=({'id': 1}, None)), \
             patch.object(studio, 'save', side_effect=locked_save):
            result = auto.control(self.record['id'], auto.Control(action='resume'), None)
        self.assertEqual(result['state'], 'running')
        saved = self.read()
        self.assertNotIn('dialogue_stale', saved['shots'][0])
        self.assertEqual(saved['shots'][0]['video_request'], original_request)
        self.assertEqual(saved['revision'], self.record['revision'] + 1)

    def test_resume_does_not_save_if_another_stale_completed_shot_blocks(self):
        spoken = copy.deepcopy(self.shot); spoken['id'] = 'spoken'
        spoken['native_dialogue']['turns'] = [{'character_id': 'spoken', 'text': 'Hello'}]
        self.record['shots'].append(spoken)
        studio.save(self.path, self.record)
        state = auto.register_native(1, self.record); auto._pause(state, 'paused')
        before = (self.path / 'record.json').read_bytes()
        with patch.object(auto, '_owned_job', return_value=({'id': 1}, None)):
            with self.assertRaises(HTTPException) as caught:
                auto.control(self.record['id'], auto.Control(action='resume'), None)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual((self.path / 'record.json').read_bytes(), before)

    def test_route_switch_keeps_only_exact_completed_silent_shot_current(self):
        self.record['creation_parameters']['dialogue_tts_engine'] = 'qwen3'
        pending = copy.deepcopy(self.shot); pending.update(id='pending', video_status='pending')
        self.record['shots'].append(pending)
        studio.save(self.path, self.record)
        result = studio.set_dialogue_route(self.record['id'], studio.NarrationRoute(
            revision=self.record['revision'], route='external_tts', tts_engine='indextts25'), None)
        self.assertNotIn('dialogue_stale', result['shots'][0])
        self.assertTrue(result['shots'][1]['dialogue_stale'])


if __name__ == '__main__':
    unittest.main()
