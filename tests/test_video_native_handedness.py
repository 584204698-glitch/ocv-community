import copy
import tempfile
import unittest
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import patch
from PIL import Image
from backend.app import video_native_handedness as subject,video_studio as studio,one_click
from tests.test_h3_dialogue import data


class HandednessTests(unittest.TestCase):
    def setUp(self):
        self.record={'id':'a'*32,'revision':1,'native_story':{'text':'Two friends share a gift.'},
            'native_owner_id':1,'settings':{},'status':'image_review','logs':[]}
        plan=data();plan['speakers'][0]['position']='Near the table, holding a cup in the right hand.'
        self.shot={'id':'shot_001','kind':'video','action':'右手拿着杯子。','visual_description':'右手持杯。',
            'image_prompt':'一个人右手持杯。','native_dialogue':plan,
            'cast_plan':{'source':'director_plan','characters':[{'basis':'右手持杯。'}]},
            'image':'image.jpg','image_status':'completed'}
        self.findings=[{'shot_id':'shot_001','code':'opening_state','evidence':'杯子在自身左手，规划为右手。'}]
        self.record['shots']=[self.shot,{'id':'shot_002','kind':'video','action':'Unchanged'}]

    def test_only_incidental_side_changes_dialogue_identities_times_and_space_preserved(self):
        updated=subject.candidate(self.record,self.shot,self.findings)
        self.assertEqual(updated['action'],'手拿着杯子。')
        original_plan=copy.deepcopy(self.shot['native_dialogue'])
        expected=copy.deepcopy(original_plan)
        expected['speakers'][0]['position']='Near the table, holding a cup in the hand.'
        self.assertEqual(updated['native_dialogue'],expected)
        self.assertEqual(self.shot['native_dialogue'],original_plan)
        self.assertEqual(subject.neutralize('door on the right-hand side, 桌子在右手边。'),
                         'door on the right-hand side, 桌子在右手边。')

    def test_author_and_identity_constraints_manual_edits_and_finished_clips_are_protected(self):
        for mode in ('story','appearance','prop','manual','clip','retry','anatomy','presence'):
            r=copy.deepcopy(self.record);s=r['shots'][0];issues=copy.deepcopy(self.findings)
            if mode=='story':r['native_story']['text']='She injured her left hand.'
            elif mode=='appearance':s['native_dialogue']['speakers'][0]['appearance']='A red glove on the right hand.'
            elif mode=='prop':s['continuity_states']=[{'opening':{'description':'右手接过杯子'}}]
            elif mode=='manual':s['prompt_refresh_note']='User adjusted action'
            elif mode=='clip':s['video_task_id']='submitted-task'
            elif mode=='retry':s['native_handedness_adaptation']={'done':True}
            elif mode=='anatomy':issues[0]['code']='anatomy'
            else:issues[0]['code']='cast'
            with self.subTest(mode=mode):self.assertIsNone(subject.candidate(r,s,issues))

    def fixture(self,root):
        path=studio.directory(1,self.record['id']);path.mkdir(parents=True)
        Image.new('RGB',(20,20),'blue').save(path/'image.jpg')
        studio.save(path,self.record)
        from backend.app.video_native_image_repair import _digest
        report={'image_order':['shot_001'],'image_versions':{'shot_001':_digest(path/'image.jpg')},
                'review':{'observations':[{'shot_id':'shot_001','description':'Holding the cup.'}],
                          'issues':self.findings,'uncertainties':[]}}
        return path,report

    def test_real_commit_retains_pixels_and_other_shots_and_requires_new_review(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(studio,'ROOT',Path(folder)), \
             patch('backend.app.language_routing.project_language_scope',return_value=nullcontext()):
            path,report=self.fixture(folder);before=(path/'image.jpg').read_bytes()
            result=subject.adapt(self.record['id'],1,{'id':1},[report],
                ask=lambda *a:{'safe':True,'reason':'The story does not depend on a particular hand.'})
            self.assertEqual(result['revision'],2)
            self.assertEqual(result['status'],'image_review')
            self.assertEqual((path/'image.jpg').read_bytes(),before)
            self.assertEqual(result['shots'][1],self.record['shots'][1])
            evidence=result['shots'][0]['native_handedness_adaptation']
            self.assertEqual(evidence['previous_staging'],self.shot)
            self.assertFalse(evidence['quality_accepted'])

    def test_denied_semantics_pause_and_stale_images_do_not_modify(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(studio,'ROOT',Path(folder)), \
             patch('backend.app.language_routing.project_language_scope',return_value=nullcontext()):
            path,report=self.fixture(folder);before=(path/'record.json').read_bytes()
            self.assertIsNone(subject.adapt(self.record['id'],1,{'id':1},[report],
                ask=lambda *a:{'safe':False,'reason':'The hand is essential to the planned transfer.'}))
            self.assertEqual((path/'record.json').read_bytes(),before)
            self.assertIsNone(subject.adapt(self.record['id'],1,{'id':1},[report],
                ask=lambda *a:self.fail('unchanged denied result must use cache')))
            token=one_click._executing.set(True)
            try:
                with patch.object(one_click,'load',return_value={'state':'paused'}):
                    subject.adapt(self.record['id'],1,{'id':1},[report],ask=lambda *a:{'safe':True,'reason':'No specific hand is required.'})
                self.assertEqual((path/'record.json').read_bytes(),before)
            finally:one_click._executing.reset(token)
            Image.new('RGB',(20,20),'red').save(path/'image.jpg')
            with self.assertRaisesRegex(ValueError,'过期'):
                subject.adapt(self.record['id'],1,{'id':1},[report],ask=lambda *a:self.fail('must not call model'))

    def test_adjacent_unrendered_contracts_are_synchronized_but_completed_clip_is_untouched(self):
        next_shot=copy.deepcopy(self.shot);next_shot['id']='shot_002'
        done=copy.deepcopy(self.shot);done.update(id='shot_003',video_task_id='paid',video_status='completed')
        self.record['shots']=[self.shot,next_shot,done]
        with tempfile.TemporaryDirectory() as folder,patch.object(studio,'ROOT',Path(folder)), \
             patch('backend.app.language_routing.project_language_scope',return_value=nullcontext()):
            path,report=self.fixture(folder)
            def approve(system,evidence):
                self.assertEqual([s['id'] for s in evidence['proposed_shots']],['shot_001','shot_002'])
                self.assertEqual(evidence['shots'][2]['action'],done['action'])
                return {'safe':True,'reason':'Only these adjacent unrendered shots use incidental handedness.'}
            result=subject.adapt(self.record['id'],1,{'id':1},[report],ask=approve)
            self.assertEqual(result['shots'][1]['action'],'手拿着杯子。')
            self.assertEqual(result['shots'][2],done)

    def test_pause_after_positive_semantic_result_wins_before_commit(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(studio,'ROOT',Path(folder)), \
             patch('backend.app.language_routing.project_language_scope',return_value=nullcontext()):
            path,report=self.fixture(folder);before=(path/'record.json').read_bytes()
            token=one_click._executing.set(True)
            try:
                with patch.object(one_click,'load',return_value={'state':'paused'}):
                    subject.adapt(self.record['id'],1,{'id':1},[report],
                        ask=lambda *a:{'safe':True,'reason':'The holding hand has no story significance.'})
                self.assertEqual((path/'record.json').read_bytes(),before)
            finally:one_click._executing.reset(token)
