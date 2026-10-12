import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.app.video_opening_preflight import check_shots
from backend.app.video_native_preparation import prepare_native


class OpeningPreflightTests(unittest.TestCase):
    def test_all_shots_collect_conflicts_and_retain_success_without_gpu_or_tts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);(path/'core.jpg').write_bytes(b'core')
            shots=[dict(id=f's{i}',kind='video',native_dialogue={'plan':i},image='core.jpg',
                        visual_description='站在石阶上',video_status='pending') for i in range(3)]
            shots.append(dict(id='done',kind='video',native_dialogue={},video_status='completed'))
            record=dict(shots=shots)
            def prep(owner,path,record,shot,selection,images,**kw):
                self.assertFalse(kw['audio'])
                if shot['id']=='s1':raise ValueError('开场图与已批准动作不一致，尚未提交H3：站在平地')
                shot['native_visual_grounding']={'fingerprint':'retained'}
                return {},'source'
            with patch('backend.app.video_inputs.validate_shot_inputs',return_value=({},[],{})), \
                 patch('backend.app.video_native_preparation.prepare_native',side_effect=prep):
                rows=check_shots(1,path,record)
            self.assertEqual([r['status'] for r in rows],['ready','blocked','ready'])
            self.assertEqual(shots[2]['native_visual_grounding']['fingerprint'],'retained')
            self.assertIn('opening_image_repair',shots[1])
            self.assertEqual(shots[1]['video_status'],'pending')

    def test_local_reedit_does_not_check_other_shots(self):
        shots=[dict(id=x,kind='video',native_dialogue={'plan':x},video_status='pending') for x in ('a','b')]
        before=copy.deepcopy(shots[0])
        with patch('backend.app.video_inputs.validate_shot_inputs',return_value=({},[],{})), \
             patch('backend.app.video_native_preparation.prepare_native',return_value=({},'auto')) as prep:
            check_shots(1,Path('.'),dict(shots=shots),shot_id='b')
        self.assertEqual(prep.call_count,1);self.assertEqual(shots[0],before)

    def test_manual_path_does_not_call_grounding_or_director(self):
        compiled={'prompt':'current','external_dialogue':{'key':'existing'},'fingerprint':'a'}
        shot=dict(native_dialogue={'frozen':True},manual_h3_prompt={'prompt':'manual'})
        with patch('backend.app.video_native_staging.assert_staging_current'), \
             patch('backend.app.video_dialogue.prepare_dialogue',return_value=compiled), \
             patch('backend.app.video_manual_h3.adopted',return_value='manual'), \
             patch('backend.app.video_native_grounding.ground_performance') as grounding, \
             patch('backend.app.video_native_staging.complete_performance') as director:
            result,source=prepare_native(1,Path('.'),{},shot,{},[])
        self.assertEqual(result['prompt'],'manual');self.assertEqual(source,'user-manual-h3-v1')
        grounding.assert_not_called();director.assert_not_called()


if __name__=='__main__':unittest.main()
