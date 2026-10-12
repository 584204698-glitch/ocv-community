"""Ordinary production is independent of optional model quality verdicts."""
import copy
import unittest
from unittest.mock import patch
from backend.app import one_click, video_studio, video_generation, video_export
from backend.app import video_core_references as refs
from tests.test_video_core_reference_workflow import CoreReferenceWorkflowTests


class OrdinaryAutomationTests(unittest.TestCase):
    setUp=CoreReferenceWorkflowTests.setUp
    generate=CoreReferenceWorkflowTests.generate

    def test_normal_image_worker_reuses_sources_without_paid_quality_gates(self):
        record=self.f.read();record['creation_parameters'].pop('quality_review_policy')
        video_studio.save(self.f.path,record)
        with patch('backend.app.video_scene_quality.check_native_scene_images',side_effect=AssertionError('No automatic scene reviewer')), \
             patch.object(refs,'screen_group',side_effect=AssertionError('No automatic source reviewer')):
            after=self.generate()
        self.assertEqual(after['error'],'')
        self.assertEqual(self.f.renderer.call_count,3)
        for shot in after['shots'][1:]:
            self.assertEqual(shot['core_reference_used']['review_status'],'not_requested')
            self.assertEqual(refs.dependency_error(self.f.path,after,shot),'')
        source=after['shots'][0]
        self.f.write_image(source['image'],'orange')
        self.assertTrue(refs.dependency_error(self.f.path,after,after['shots'][1]))

    def test_normal_confirm_video_export_do_not_invoke_model_reviews_or_repair(self):
        record=self.f.read();record['creation_parameters'].pop('quality_review_policy')
        record['shots']=[dict(id='one',kind='video',video_status='completed'),
                         dict(id='two',kind='video',video_status='pending')]
        before=copy.deepcopy(record)
        with patch.object(video_studio,'storyboard_review_error',return_value=''), \
             patch('backend.app.video_native_image_quality.check_native_core_images',side_effect=AssertionError('No image gate')), \
             patch('backend.app.video_dialogue_quality.check_native_speech',side_effect=AssertionError('No ASR gate')), \
             patch('backend.app.video_quality.check_automatic_export',side_effect=AssertionError('No visual gate')), \
             patch.object(video_studio,'confirm_storyboard_images_for_user') as confirm, \
             patch.object(video_generation,'generate_for_user') as generate, \
             patch.object(video_export,'start_export_for_user') as export:
            for action in ('confirm_images','videos','export'):
                one_click.dispatch(action,{'id':1},record)
        confirm.assert_called_once()
        self.assertEqual(generate.call_args.args[1].shot_ids,['two'])
        self.assertFalse(export.call_args.kwargs['review_generated'])
        self.assertTrue(export.call_args.args[1].use_video_audio)
        self.assertEqual(record,before)

    def test_missing_assets_still_prevent_next_stage(self):
        record=self.f.read();record['creation_parameters'].pop('quality_review_policy')
        with patch.object(video_studio,'storyboard_review_error',return_value='Missing image'), \
             patch.object(video_generation,'generate_for_user') as generate:
            with self.assertRaisesRegex(ValueError,'Missing image'):
                one_click.dispatch('videos',{'id':1},record)
        generate.assert_not_called()
