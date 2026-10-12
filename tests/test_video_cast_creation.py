"""Creation and draft uploads bind assets before planning without generating media."""
import ast
import copy
import hashlib
import io
import sys
import tempfile
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Literal
from unittest.mock import Mock, patch

from fastapi import HTTPException
from PIL import Image
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from backend.app import story_workspace, video_studio as studio, video_generation as generation
from backend.app.video_cast_planning import CreationCastBinding, creation_cast_bindings, saved_planning_cast
from backend.app.video_render_options import VideoRenderOptions
from backend.app.cloud_image_channel import ImageSize
from tests import test_video_character_coverage as fixtures


def request_model():
    """Load the actual schema without starting the application or importing media runtimes."""
    path = Path(__file__).resolve().parents[1] / 'backend' / 'app' / 'main.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)
             and node.name in {'BgmTrackRequest', 'GenerateRequest'}]
    namespace = {'__name__': __name__, 'BaseModel': BaseModel, 'Field': Field,
        'VideoRenderOptions': VideoRenderOptions,
        'ImageSize': ImageSize,
        'field_validator': field_validator, 'model_validator': model_validator,
        'Any': Any, 'Literal': Literal, 'StorySource': story_workspace.StorySource,
        'DEFAULT_QWEN_VOICE': 'Cherry', 'CreationCastBinding': CreationCastBinding,
        'creation_cast_bindings': creation_cast_bindings}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), 'exec'), namespace)
    return namespace['GenerateRequest']


class CastCreationRequestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Request = request_model()

    def test_request_roundtrips_explicit_binding_and_accepts_legacy_empty_list(self):
        payload = self.Request(dynamic_video=True, reference_image_ids=['uploaded-a'],
            project_cast_bindings=[{'asset_id': 'uploaded-a', 'name': 'A', 'constraints': 'No visible ears.'}])
        self.assertEqual(payload.model_dump()['project_cast_bindings'],
                         [{'asset_id': 'uploaded-a', 'name': 'A', 'constraints': 'No visible ears.'}])
        self.assertEqual(self.Request().project_cast_bindings, [])

    def test_schema_rejects_foreign_duplicate_or_non_dynamic_bindings(self):
        binding = {'asset_id': 'uploaded-a', 'name': 'A'}
        cases = [dict(dynamic_video=False, reference_image_ids=['uploaded-a'], project_cast_bindings=[binding]),
                 dict(dynamic_video=True, reference_image_ids=[], project_cast_bindings=[binding]),
                 dict(dynamic_video=True, reference_image_ids=['uploaded-a'], project_cast_bindings=[binding] * 2),
                 dict(dynamic_video=True, reference_image_ids=['uploaded-a'],
                      project_cast_bindings=[{**binding, 'confirmed': True}])]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.Request(**kwargs)


class CastCreationImportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(studio, 'ROOT', self.root / 'videos'))
        self.source = self.root / 'audio-output'
        (self.source / 'input').mkdir(parents=True)
        (self.source / 'other').mkdir()
        with wave.open(str(self.source / 'input' / '配音.wav'), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(800)
            audio.writeframes(b'\0\0' * 3200)
        (self.source / 'other' / '最终字幕.srt').write_text(
            '1\n00:00:00,000 --> 00:00:04,000\nA waits; B enters.\n', encoding='utf-8')
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir()
        for identity, color in [('upload-a', 'red'), ('upload-b', 'blue')]:
            Image.new('RGB', (18, 16), color).save(self.uploads / (identity + '.png'))
        self.pipeline = ModuleType('backend.app.pipeline')
        self.pipeline.validate_step_audio_snapshot = Mock()
        self.pipeline.user_reference_image_path = Mock(side_effect=lambda owner, identity: self.uploads / (identity + '.png'))
        self.tts = ModuleType('backend.app.tts_editor')
        self.tts.tts_editor = SimpleNamespace(status=Mock(return_value={'status': 'idle'}), commit_step_review=Mock(), _project_dir=Mock(return_value=self.source))
        self.stack.enter_context(patch.dict(sys.modules, {'backend.app.pipeline': self.pipeline,
                                                        'backend.app.tts_editor': self.tts}))
        self.job = SimpleNamespace(id='fixture-audio-job', user_id=1, request={
            'dynamic_video': True, 'script': 'A waits; B enters.', 'project_name': 'Bound creation',
            'reference_image_ids': ['upload-b', 'upload-a'],
            'reference_image_labels': {'upload-a': '图1', 'upload-b': '图2'},
            'reference_image_notes': {'upload-a': 'A identity', 'upload-b': 'B identity'},
            'project_cast_bindings': [{'asset_id': 'upload-a', 'name': 'A', 'constraints': 'No visible ears.'},
                                      {'asset_id': 'upload-b', 'name': 'B', 'constraints': 'Green dress.'}],
        })
        self.stack.enter_context(patch.object(studio, 'source_project', return_value=(self.job, self.source)))

    def run_import(self, from_audio=True):
        return studio.import_new_project(studio.ImportProject(job_id=self.job.id, from_audio_task=from_audio), {'id': 1})

    def test_audio_task_creates_owned_identity_by_asset_id_and_copied_bytes(self):
        record = self.run_import()
        snapshot = saved_planning_cast(record)
        self.assertEqual(record['status'], 'draft')
        self.assertEqual(record['shots'], [])
        self.assertEqual(snapshot['project_id'], record['id'])
        first, second = snapshot['characters']
        # Catalog order and display labels deliberately disagree with binding order.
        self.assertEqual((first['name'], first['reference_id']), ('A', 'ref_02'))
        self.assertEqual((second['name'], second['reference_id']), ('B', 'ref_01'))
        self.assertEqual(first['reference_sha256'], hashlib.sha256((self.uploads / 'upload-a.png').read_bytes()).hexdigest())
        self.assertEqual(studio.verify_project_cast(1, studio.directory(1, record['id']), record), snapshot)
        self.assertFalse(any('video_character_review' in shot for shot in record['shots']))
        self.tts.tts_editor.commit_step_review.assert_called_once_with(job=self.job, user_id=1)
        self.pipeline.validate_step_audio_snapshot.assert_called_once_with(self.job)

    def test_same_uploaded_asset_in_two_projects_receives_distinct_project_identity(self):
        first, second = self.run_import(), self.run_import()
        a, b = saved_planning_cast(first), saved_planning_cast(second)
        self.assertNotEqual(a['project_id'], b['project_id'])
        self.assertNotEqual(a['characters'][0]['project_character_id'], b['characters'][0]['project_character_id'])
        self.assertEqual(a['characters'][0]['reference_sha256'], b['characters'][0]['reference_sha256'])

    def test_ordinary_audio_source_never_inherits_its_fixed_roles_or_reference_images(self):
        self.job.request['project_cast_bindings'] = 'damaged old record must be ignored'
        record = self.run_import(from_audio=False)
        self.assertNotIn('project_cast', record)
        self.assertNotIn('project_cast_bindings', record['creation_parameters'])
        self.assertEqual(record['references'], [])
        self.tts.tts_editor.commit_step_review.assert_not_called()
        self.pipeline.user_reference_image_path.assert_not_called()

    def test_invalid_binding_stops_before_audio_commit_or_asset_copy(self):
        original = copy.deepcopy(self.job.request)
        cases = [dict(project_cast_bindings=[{'asset_id': 'foreign', 'name': 'A'}]),
                 dict(project_cast_bindings=original['project_cast_bindings'] * 2),
                 dict(project_cast_bindings=[{'asset_id': 'upload-a', 'name': 'A', 'confirmed': True}]),
                 dict(reference_image_ids=None),
                 dict(reference_image_ids=[*map(str, range(6)), 'upload-a'],
                      project_cast_bindings=[{'asset_id': 'upload-a', 'name': 'A'}])]
        for changes in cases:
            with self.subTest(changes=changes):
                self.job.request = {**copy.deepcopy(original), **changes}
                self.tts.tts_editor.status.reset_mock()
                self.tts.tts_editor.commit_step_review.reset_mock()
                self.pipeline.validate_step_audio_snapshot.reset_mock()
                self.pipeline.user_reference_image_path.reset_mock()
                with patch.object(studio, 'copy_assets') as copy_assets:
                    with self.assertRaises(HTTPException) as caught:
                        self.run_import()
                    self.assertEqual(caught.exception.status_code, 409)
                    copy_assets.assert_not_called()
                self.tts.tts_editor.status.assert_not_called()
                self.tts.tts_editor.commit_step_review.assert_not_called()
                self.pipeline.validate_step_audio_snapshot.assert_not_called()
                self.pipeline.user_reference_image_path.assert_not_called()
                self.assertFalse((self.root / 'videos').exists())

    def test_missing_or_undecodable_bound_upload_fails_before_audio_commit(self):
        original = (self.uploads / 'upload-a.png').read_bytes()
        for cause in ('missing', 'invalid'):
            with self.subTest(cause=cause):
                target = self.uploads / 'upload-a.png'
                if cause == 'missing':
                    target.unlink()
                else:
                    target.write_bytes(b'not a picture')
                with patch.object(studio, 'copy_assets') as copy_assets:
                    with self.assertRaises(HTTPException) as caught:
                        self.run_import()
                    self.assertEqual(caught.exception.status_code, 409)
                    copy_assets.assert_not_called()
                self.tts.tts_editor.status.assert_not_called()
                self.tts.tts_editor.commit_step_review.assert_not_called()
                self.pipeline.validate_step_audio_snapshot.assert_not_called()
                self.assertFalse((self.root / 'videos').exists())
                target.write_bytes(original)

    def test_upload_replaced_during_audio_commit_cannot_be_silently_bound(self):
        self.tts.tts_editor.commit_step_review.side_effect = lambda **kwargs: Image.new(
            'RGB', (18, 16), 'yellow').save(self.uploads / 'upload-a.png')
        with self.assertRaises(HTTPException) as caught:
            self.run_import()
        self.assertEqual(caught.exception.status_code, 400)
        self.tts.tts_editor.commit_step_review.assert_called_once()
        self.assertEqual(list((self.root / 'videos').rglob('record.json')), [])


class CastDraftAssetUploadTest(unittest.TestCase):
    setUp = fixtures.VideoCharacterCoverageTest.setUp
    read = fixtures.VideoCharacterCoverageTest.read

    @staticmethod
    def png():
        stream = io.BytesIO()
        Image.new('RGB', (18, 16), 'orange').save(stream, format='PNG')
        return stream.getvalue()

    def draft(self):
        record = self.read()
        record.update(status='draft', shots=[])
        record['creation_parameters']['video_generation_backend'] = 'api'
        studio.save(self.path, record)
        return self.read()

    def test_draft_asset_upload_is_owned_and_does_not_adopt_or_generate(self):
        before = self.draft()
        with patch.object(studio, 'plan_storyboard') as planner, \
                patch.object(generation, '_start_local_worker') as media:
            response = self.client.post(self.base + '/video-input-assets',
                data={'revision': before['revision']}, files={'file': ('identity.png', self.png(), 'image/png')})
            self.assertEqual(response.status_code, 200, response.text)
            planner.assert_not_called()
            media.assert_not_called()
        result = response.json()
        record = self.read()
        self.assertEqual(record['status'], 'draft')
        self.assertEqual(record['shots'], [])
        self.assertEqual(record['references'], before['references'])
        self.assertNotIn('project_cast', record)
        self.assertEqual(record['revision'], before['revision'] + 1)
        self.assertEqual(len(record['video_input_assets']), 1)
        self.assertEqual((self.path / record['video_input_assets'][0]['file']).read_bytes(), self.png())
        asset_id = result['asset']['id']
        self.assertTrue(asset_id.startswith('vi_'))
        response = self.client.put(self.base + '/project-cast', json={'revision': record['revision'],
            'characters': [{'name': 'A', 'reference_id': asset_id, 'constraints': 'Keep orange.'}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['characters'][0]['status'], 'current')
        self.assertEqual(self.read()['shots'], [])

    def test_draft_upload_rejects_stale_revision_other_owner_and_invalid_bytes(self):
        before = self.draft()
        response = self.client.post(self.base + '/video-input-assets',
            data={'revision': before['revision'] - 1}, files={'file': ('identity.png', self.png(), 'image/png')})
        self.assertEqual(response.status_code, 409, response.text)
        with patch.object(studio, 'require_user', lambda request: {'id': 2}):
            response = self.client.post(self.base + '/video-input-assets',
                data={'revision': before['revision']}, files={'file': ('identity.png', self.png(), 'image/png')})
            self.assertEqual(response.status_code, 404, response.text)
        response = self.client.post(self.base + '/video-input-assets',
            data={'revision': before['revision']}, files={'file': ('identity.png', b'not an image', 'image/png')})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.read(), before)
        self.assertEqual(list((self.path / 'assets' / 'video_inputs').glob('*')), [])


if __name__ == '__main__':
    unittest.main()
