"""Explicit audio continuation owns one child; copies and GETs cannot rebind it."""
import copy
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from backend.app import video_studio as studio, video_sources, video_export, visual_editor
from tests import test_video_cast_creation as fixtures


class AudioTaskBindingTest(unittest.TestCase):
    def setUp(self):
        fixtures.CastCreationImportTest.setUp(self)
        self.storage = Mock()
        self.storage.get.return_value = self.job
        self.storage.update.side_effect = lambda job, **fields: vars(job).update(fields)
        self.stack.enter_context(patch.object(video_sources, 'dependencies',
                                              return_value=(self.storage, Mock(), Mock())))
        self.stack.enter_context(patch.object(studio, 'require_user', return_value={'id': 1}))
        self.stack.enter_context(patch.object(visual_editor, 'OUTPUT_DIR', self.source.parent))

    def enter(self, audio=True):
        return studio.import_project(studio.ImportProject(
            job_id=self.job.id, from_audio_task=audio), None)

    def test_successful_real_import_binds_once_and_retry_reuses_same_assets(self):
        record = self.enter()
        self.assertEqual(self.job.request['_dynamic_video_project_id'], record['id'])
        self.assertEqual(self.job.request['_dynamic_audio_source_dir'], self.source.name)
        self.assertEqual(self.job.request['_step_output_dir'], self.source.name)
        self.assertEqual(len(record['project_cast']['characters']), 2)
        original = copy.deepcopy(self.job.request)
        self.assertEqual(self.enter(), record)
        self.assertEqual(self.job.request, original)
        self.storage.update.assert_called_once()
        self.tts.tts_editor.commit_step_review.assert_called_once()
        self.assertEqual(len(list(studio.ROOT.glob('1/*/record.json'))), 1)

    def test_later_copy_with_more_work_never_replaces_the_adopted_child(self):
        adopted = self.enter()
        clone = studio.import_new_project(studio.ImportProject(
            job_id=self.job.id, from_audio_task=True), {'id': 1})
        clone['shots'] = [{'id': 'saved-work'}]
        studio.save(studio.directory(1, clone['id']), clone)
        self.storage.update.reset_mock()
        self.assertEqual(studio.by_audio_task(self.job.id, None)['project']['id'], adopted['id'])
        self.assertEqual(self.enter()['id'], adopted['id'])
        self.storage.update.assert_not_called()

    def test_ordinary_import_does_not_adopt_source_lifecycle(self):
        self.enter(audio=False)
        self.assertNotIn('_dynamic_video_project_id', self.job.request)
        self.storage.update.assert_not_called()

    def test_failed_audio_commit_leaves_source_unbound_and_no_project(self):
        before = copy.deepcopy(self.job.request)
        self.tts.tts_editor.commit_step_review.side_effect = ValueError('Incomplete audio')
        with self.assertRaises(HTTPException) as caught:
            self.enter()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.job.request, before)
        self.storage.update.assert_not_called()
        self.assertFalse(list(studio.ROOT.glob('1/*/record.json')))

    def test_lookup_has_no_writes_and_explicit_reentry_can_adopt_legacy_completion(self):
        record = studio.import_new_project(studio.ImportProject(
            job_id=self.job.id, from_audio_task=True), {'id': 1})
        record['status'] = 'completed'
        studio.save(studio.directory(1, record['id']), record)
        self.assertEqual(studio.by_audio_task(self.job.id, None)['project']['id'], record['id'])
        self.storage.update.assert_not_called()
        with patch.object(video_export, 'reconcile_source_job') as reconcile:
            self.assertEqual(self.enter()['id'], record['id'])
            self.assertEqual(self.job.request['_dynamic_video_project_id'], record['id'])
            reconcile.assert_called_once()
            self.assertEqual(reconcile.call_args.args[0]['id'], record['id'])

    def test_missing_bound_project_does_not_silently_choose_a_copy(self):
        self.enter(audio=False)
        self.job.request['_dynamic_video_project_id'] = 'f' * 32
        self.assertIsNone(studio.by_audio_task(self.job.id, None)['project'])
        with self.assertRaises(HTTPException) as caught:
            self.enter()
        self.assertEqual(caught.exception.status_code, 409)
        self.storage.update.assert_not_called()

    def test_legacy_completion_recovers_original_audio_workspace_on_explicit_reentry(self):
        record = studio.import_new_project(studio.ImportProject(
            job_id=self.job.id, from_audio_task=True), {'id': 1})
        self.job.request.update(_dynamic_video_project_id=record['id'], _step_output_dir='final-archive')
        self.pipeline.store = self.storage
        self.enter()
        self.assertEqual(self.job.request['_step_output_dir'], self.source.name)
        self.assertEqual(visual_editor.VisualEditor.output_dir(self.job.id, 1), self.source.resolve())
        self.storage.update.reset_mock()
        self.enter()
        self.storage.update.assert_not_called()

    def test_missing_audio_workspace_never_rebinds_or_falls_back_to_an_archive(self):
        record = self.enter()
        self.pipeline.store = self.storage
        self.job.request['_dynamic_audio_source_dir'] = '../foreign'
        with self.assertRaises(FileNotFoundError):
            visual_editor.VisualEditor.output_dir(self.job.id, 1)
        self.job.request['_dynamic_audio_source_dir'] = self.source.name
        with self.assertRaises(FileNotFoundError):
            visual_editor.VisualEditor.output_dir(self.job.id, 2)
        (self.source/'other'/'最终字幕.srt').unlink()
        before = copy.deepcopy(self.job.request)
        with self.assertRaises(HTTPException) as caught:
            self.enter()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.job.request, before)
        self.assertEqual(studio.read(studio.directory(1, record['id']))['id'], record['id'])

    def test_foreign_or_non_dynamic_source_cannot_be_adopted(self):
        for foreign in (True, False):
            self.job.user_id = 2 if foreign else 1
            self.job.request['dynamic_video'] = foreign
            with self.subTest(foreign=foreign), self.assertRaises(HTTPException) as caught:
                self.enter()
            self.assertEqual(caught.exception.status_code, 404 if foreign else 409)
        self.storage.update.assert_not_called()
        self.assertFalse(list(studio.ROOT.glob('1/*/record.json')))

    def test_sync_recovers_unreviewed_storyboard_without_generating_media(self):
        record=self.enter()
        record.update(status='image_review',shots=[{'id':'draft-shot','kind':'static','image_status':'pending'}])
        studio.save(studio.directory(1,record['id']),record)
        editor=Mock();editor.output_dir.return_value=self.source
        with patch.object(video_sources,'dependencies',return_value=(self.storage,editor,Mock())):
            result=studio.sync_edited_audio(record['id'],studio.Review(revision=record['revision']),None)
        self.assertEqual(result['status'],'storyboard_review')
        self.assertEqual(result['shots'][0]['image_status'],'pending')


if __name__ == '__main__':
    unittest.main()
