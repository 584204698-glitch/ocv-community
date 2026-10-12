import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from backend.app.video_manual_h3 import validate, adopted, retire_after_action_replan
from backend.app import video_generation as generation


PROMPT='''subject_definitions:
<Picture 1> is the scene.
<Subject 1> is the character in <Picture 2>.
S1 is the voice for <Subject 1>.
summary:
One continuous shot.
detailed_description:
<Subject 1> (S1) says <d>[Chinese] 小林！</d>.
overall_soundscape:
Quiet courtyard.
non_diegetic_music:
N/A'''


class ManualH3Tests(unittest.TestCase):
    def test_action_replan_retires_old_manual_without_deleting_its_text_or_assets(self):
        manual=dict(prompt=PROMPT,fingerprint='old')
        shot=dict(manual_h3_prompt=manual,video='original.mp4',native_dialogue={'new':True})
        self.assertTrue(retire_after_action_replan(shot))
        self.assertNotIn('manual_h3_prompt',shot)
        self.assertEqual(shot['manual_h3_history'][-1]['prompt'],PROMPT)
        self.assertEqual(shot['video'],'original.mp4')
        manual['prompt']='changed elsewhere'
        self.assertEqual(shot['manual_h3_history'][-1]['prompt'],PROMPT)
        self.assertFalse(retire_after_action_replan(shot))

    def test_retired_manual_history_is_bounded(self):
        shot={'manual_h3_history':[{'prompt':str(i)} for i in range(8)],
              'manual_h3_prompt':{'prompt':'last'}}
        retire_after_action_replan(shot)
        self.assertEqual(len(shot['manual_h3_history']),5)
        self.assertEqual(shot['manual_h3_history'][-1]['prompt'],'last')

    def setUp(self):
        self.compiled=dict(prompt=PROMPT,fingerprint='a'*64,external_dialogue={'key':'cached'})

    def test_user_can_simplify_actions_without_changing_inputs_or_words(self):
        prompt=PROMPT.replace('One continuous shot.','Fixed camera.').replace('Quiet courtyard.','Footsteps.')
        self.assertEqual(validate(prompt,self.compiled),prompt)
        self.assertEqual(adopted(dict(manual_h3_prompt=dict(prompt=prompt,fingerprint='a'*64)),self.compiled),prompt)

    def test_changed_input_does_not_silently_reuse_manual_prompt(self):
        with self.assertRaisesRegex(ValueError,'已变化'):
            adopted(dict(manual_h3_prompt=dict(prompt=PROMPT,fingerprint='b'*64)),self.compiled)

    def test_wrong_character_picture_missing_structure_or_changed_dialogue_is_rejected(self):
        bad=[PROMPT.replace('<Picture 2>','<Picture 3>'),PROMPT.replace('小林！','你好！'),
             PROMPT.replace('overall_soundscape:','sound:'),PROMPT.replace('S1 is the voice','S2 is the voice'),
             PROMPT.replace('Quiet courtyard.','<Subject 9> joins.')]
        for prompt in bad:
            with self.subTest(prompt=prompt),self.assertRaises(ValueError):validate(prompt,self.compiled)

    def test_adoption_does_not_invalidate_existing_video_or_start_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            shot=dict(id='shot_005',video='clip.mp4',video_status='completed',native_dialogue={'frozen':True})
            record=dict(status='video_review',revision=3,logs=[],shots=[shot])
            before=copy.deepcopy(shot)
            with patch.object(generation.studio,'require_user',return_value={'id':1}), \
                 patch.object(generation.studio,'directory',return_value=Path(tmp)), \
                 patch.object(generation.studio,'read',return_value=record), \
                 patch.object(generation.studio,'editable'),patch.object(generation.studio,'save'), \
                 patch.object(generation,'validate_shot_inputs',return_value=({},[],{})), \
                 patch('backend.app.video_dialogue.prepare_dialogue',return_value=self.compiled), \
                 patch.object(generation,'run_video_profile') as gpu:
                generation.adopt_manual_h3('project','shot_005',generation.ManualH3Prompt(revision=3,prompt=PROMPT),None)
                self.assertEqual(shot.pop('manual_h3_prompt')['prompt'],PROMPT)
                self.assertEqual(shot,before);gpu.assert_not_called()

    def test_missing_audio_does_not_silently_run_tts_on_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            record=dict(status='video_review',revision=3,logs=[],shots=[dict(id='s')])
            with patch.object(generation.studio,'require_user',return_value={'id':1}), \
                 patch.object(generation.studio,'directory',return_value=Path(tmp)), \
                 patch.object(generation.studio,'read',return_value=record), \
                 patch.object(generation.studio,'editable'), \
                 patch.object(generation,'validate_shot_inputs',return_value=({},[],{})), \
                 patch('backend.app.video_dialogue.prepare_dialogue',return_value=dict(self.compiled,external_dialogue=None)):
                with self.assertRaises(HTTPException):generation.adopt_manual_h3('p','s',generation.ManualH3Prompt(revision=3,prompt=PROMPT),None)

    def test_current_binding_preview_is_read_only_and_does_not_adopt_or_generate(self):
        record=dict(id='p',revision=3,shots=[dict(id='s',manual_h3_prompt={'prompt':'old','fingerprint':'old'})])
        before=copy.deepcopy(record)
        with patch.object(generation.studio,'require_user',return_value={'id':1}), \
             patch.object(generation.studio,'directory',return_value=Path('.')), \
             patch.object(generation.studio,'read',return_value=record), \
             patch.object(generation.studio,'save') as save, \
             patch.object(generation,'validate_shot_inputs',return_value=({},[],{})), \
             patch('backend.app.video_dialogue.prepare_dialogue',return_value=self.compiled) as prepare, \
             patch.object(generation,'run_video_profile') as gpu:
            preview=generation.preview_manual_h3('p','s',None)
            self.assertEqual(preview,dict(project_id='p',shot_id='s',revision=3,prompt=PROMPT))
            self.assertNotIn('prepare_narration',prepare.call_args.kwargs)
            self.assertEqual(record,before)
            save.assert_not_called();gpu.assert_not_called()


if __name__=='__main__':unittest.main()
