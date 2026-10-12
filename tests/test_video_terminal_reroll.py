import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from backend.app import video_generation as generation


class TerminalRerollTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.record=dict(settings=dict(ratio='16:9'),logs=[])
        self.shot=dict(id='first',video='attempt/clip.mp4',video_status='stopped',generation_duration=8,
                       video_request=dict(profile_id='frozen-profile',prompt='frozen prompt',use_duration=7,output_node_id='save'))

    def test_completed_remote_result_is_archived_without_new_submission(self):
        def retrieve(*args,**kwargs):
            self.assertEqual(kwargs['existing_prompt_id'],'old-id')
            self.assertEqual(kwargs['prompt'],'frozen prompt')
            self.assertEqual(kwargs['duration'],7)
            self.assertEqual(kwargs['existing_output_node_id'],'save')
            output=kwargs['output_path'];output.parent.mkdir();output.write_bytes(b'original-paid-video')
        with patch.object(generation,'video_task_state',return_value='completed'), \
             patch('backend.app.comfyui_guard.assert_submission_released') as released, \
             patch.object(generation,'run_video_profile',side_effect=retrieve) as download:
            generation._reconcile_terminal_reroll(1,self.path,self.record,self.shot,'old-id')
        released.assert_called_once();download.assert_called_once()
        self.assertEqual(self.shot['video_status'],'completed')
        self.assertEqual((self.path/self.shot['video']).read_bytes(),b'original-paid-video')

    def test_downloaded_result_is_not_downloaded_again(self):
        output=self.path/self.shot['video'];output.parent.mkdir();output.write_bytes(b'raw')
        with patch.object(generation,'video_task_state',return_value='completed'),patch('backend.app.comfyui_guard.assert_submission_released'),patch.object(generation,'run_video_profile') as download:
            generation._reconcile_terminal_reroll(1,self.path,self.record,self.shot,'old-id')
        download.assert_not_called()

    def test_live_uncertain_or_missing_tasks_cannot_be_replaced(self):
        original=copy.deepcopy(self.shot)
        for state in ('running','pending','unknown','missing','history'):
            with self.subTest(state=state),patch.object(generation,'video_task_state',return_value=state),patch.object(generation,'run_video_profile') as download:
                with self.assertRaises(HTTPException):generation._reconcile_terminal_reroll(1,self.path,self.record,self.shot,'old-id')
                download.assert_not_called()
        self.assertEqual(self.shot,original)

    def test_guard_still_alive_prevents_download_and_replacement(self):
        with patch.object(generation,'video_task_state',return_value='completed'),patch('backend.app.comfyui_guard.assert_submission_released',side_effect=ValueError('守锁收尾')),patch.object(generation,'run_video_profile') as download:
            with self.assertRaises(HTTPException):generation._reconcile_terminal_reroll(1,self.path,self.record,self.shot,'old-id')
        download.assert_not_called()

    def test_failed_remote_task_can_be_archived_without_fabricating_video(self):
        with patch.object(generation,'video_task_state',return_value='failed'),patch('backend.app.comfyui_guard.assert_submission_released'),patch.object(generation,'run_video_profile') as download:
            generation._reconcile_terminal_reroll(1,self.path,self.record,self.shot,'old-id')
        download.assert_not_called();self.assertEqual(self.shot['video_status'],'failed')
        self.assertFalse((self.path/self.shot['video']).exists())
