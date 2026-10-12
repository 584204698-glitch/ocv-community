import copy
import unittest
from unittest.mock import Mock, patch

from backend.app import video_studio as studio
from tests.test_video_native_staging import NativeMotionApiTest


class DynamicReviewTests(unittest.TestCase):
    setUp = NativeMotionApiTest.setUp
    create = NativeMotionApiTest.create

    def response(self, target='shot_001'):
        return dict(summary='保留原稿，缩短无必要的尾部停顿。',warnings=[],shots=[dict(
            id=target,reason='原台词3秒，开场0.5秒，收尾0.5秒，共4秒。',proposal=dict(
                duration=4,action='角色举起伞，看向朋友。',turn_timing=[dict(turn_index=0,start=.5,end=3.5)],
                performance_beats=[dict(start=0,end=.5,action='The character looks toward the friend.',action_zh='角色看向朋友。'),
                    dict(start=.5,end=3.5,action='The character raises the umbrella while speaking.',action_zh='角色说话时举起伞。'),
                    dict(start=3.5,end=4,action='The character lowers its arm.',action_zh='角色放下手臂。')]))])

    def review(self, response=None, **changes):
        with patch('backend.app.video_agents.ask_json',return_value=response or self.response()) as ask:
            result=self.client.post('/api/video-studio/'+self.record['id']+'/dynamic-review',json={
                'revision':self.record['revision'],'instruction':'紧凑一点，保留台词。','shot_ids':['shot_001'],**changes})
        return result,ask

    def test_preview_cached_then_apply_changes_only_selected_assets_and_reflows_timeline(self):
        record=studio.read(self.path)
        record['shots'][0].update(video='old.mp4',video_status='completed',video_version='old',image='first.jpg')
        record['shots'][0]['manual_h3_prompt'] = {'prompt': 'previous approved manual', 'basis': 'old'}
        record['shots'][1].update(video='second.mp4',video_status='completed',video_version='second',image='second.jpg')
        record['shots'][1]['manual_h3_prompt'] = {'prompt': 'unrelated manual', 'basis': 'second'}
        studio.save(self.path,record)
        before=studio.read(self.path)
        original=(self.path/'record.json').read_bytes()
        result,ask=self.review()
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual((self.path/'record.json').read_bytes(),original)
        repeated,again=self.review()
        self.assertEqual(result.json(),repeated.json());again.assert_not_called()
        proposal=result.json()
        response=self.client.post('/api/video-studio/'+self.record['id']+'/dynamic-review/apply',json={
            'revision':self.record['revision'],'review_id':proposal['review_id'],'shot_ids':['shot_001']})
        self.assertEqual(response.status_code,200,response.text)
        after=response.json();first,second=after['shots']
        self.assertEqual(first['duration'],4);self.assertEqual(first['generation_duration'],4)
        self.assertEqual(first['image'],'first.jpg');self.assertEqual(first['video_history'][0]['video'],'old.mp4')
        self.assertNotIn('manual_h3_prompt', first)
        self.assertEqual(first['manual_h3_history'][-1]['prompt'], 'previous approved manual')
        self.assertEqual(second['manual_h3_prompt'], before['shots'][1]['manual_h3_prompt'])
        self.assertEqual(second['video'],'second.mp4');self.assertEqual(second['video_version'],'second')
        self.assertEqual(second['native_dialogue'],before['shots'][1]['native_dialogue'])
        self.assertEqual(second['start'],4);self.assertEqual(after['scenes'][1]['start'],4)
        self.assertEqual(after['scenes'][0]['text'],before['scenes'][0]['text'])
        self.assertEqual(first['native_dialogue']['speakers'],before['shots'][0]['native_dialogue']['speakers'])
        self.assertEqual(first['native_dialogue']['turns'],before['shots'][0]['native_dialogue']['turns'])
        self.assertEqual(len(after['shots']),len(before['shots']))
        self.assertNotEqual(after['status'],'video_generating')

    def test_proposal_cannot_add_words_or_remove_turn(self):
        for mutate in (lambda p:p['turn_timing'].clear(),lambda p:p.update(turns=[{'text':'替换原稿'}])):
            raw=self.response();mutate(raw['shots'][0]['proposal'])
            # Independent instruction gets an independent persisted proposal.
            before=(self.path/'record.json').read_bytes()
            result,_=self.review(raw,instruction=str(raw))
            self.assertEqual(result.status_code,400,result.text)
            self.assertEqual((self.path/'record.json').read_bytes(),before)

    def test_stale_preview_and_foreign_shots_cannot_apply(self):
        result,_=self.review();self.assertEqual(result.status_code,200,result.text)
        record=studio.read(self.path);record['revision']+=1;studio.save(self.path,record)
        response=self.client.post('/api/video-studio/'+self.record['id']+'/dynamic-review/apply',json={
            'revision':record['revision'],'review_id':result.json()['review_id'],'shot_ids':['shot_001']})
        self.assertEqual(response.status_code,409)

    def test_whole_project_review_requires_exact_order_and_supports_no_change(self):
        raw=self.response();raw['shots'].append(dict(id='shot_002',reason='本镜时长适合动作。',proposal=None))
        result,_=self.review(raw,shot_ids=None)
        self.assertEqual(result.status_code,200,result.text)
        self.assertFalse(result.json()['shots'][1]['changed'])
        self.assertEqual(result.json()['shots'][1]['duration'],8)

    def test_unfinished_remote_request_blocks_review_before_language_call(self):
        record=studio.read(self.path);record['shots'][0].update(video_task_id='pending',video_status='unknown')
        studio.save(self.path,record)
        result,ask=self.review()
        self.assertEqual(result.status_code,409);ask.assert_not_called()

    def test_adoption_updates_prompt_fingerprint_only_for_unchanged_verified_inputs(self):
        from backend.app.video_prompt_inputs import adopt_prompt_basis, content_fingerprint
        basis={'version':1,'images':[]}
        record=studio.read(self.path);adopt_prompt_basis(record['shots'][0],basis);studio.save(self.path,record)
        result,_=self.review();self.assertEqual(result.status_code,200,result.text)
        with patch('backend.app.video_prompt_inputs.prompt_input_basis',return_value=basis):
            response=self.client.post('/api/video-studio/'+self.record['id']+'/dynamic-review/apply',json={
                'revision':record['revision'],'review_id':result.json()['review_id'],'shot_ids':['shot_001']})
        self.assertEqual(response.status_code,200,response.text)
        shot=response.json()['shots'][0]
        self.assertEqual(shot['video_prompt_input_basis']['content_fingerprint'],content_fingerprint(shot))

    def test_language_review_receives_counted_speech_without_counting_punctuation(self):
        from backend.app.video_timing_policy import speech_budget
        rows=speech_budget([{'text':'奶奶生日的早晨。'},{'text':'等奶奶散步回来，就能吃到一个有圆脸和光芒的面包！'}])
        self.assertEqual([row['chinese_characters'] for row in rows],[7,22])
        self.assertEqual(rows[1]['natural_seconds_min'],5.5)
        result,ask=self.review()
        self.assertEqual(result.status_code,200,result.text)
        self.assertIn('speech_budget',ask.call_args.args[1]['shots'][0])

    def state_fixture(self):
        record=studio.read(self.path)
        record['native_story']['text'] += '\n把已涂胶的小纸片轻轻按在裂口上。'
        state=dict(id='prepared',description='未涂胶的纸片。',description_en='An uncoated patch.')
        for shot in record['shots']:
            shot.update(video='keep.mp4',video_status='completed',video_version='keep')
            shot['continuity_states']=[dict(prop_id='prop_01',name='纸片',opening=copy.deepcopy(state),
                ending=copy.deepcopy(state),visibility='offscreen',visibility_basis='位于画外。',change_basis='')]
        studio.save(self.path,record)
        advice=dict(summary='纠正原文冲突。',warnings=[],shots=[dict(id=s['id'],reason='仅纠正画外状态。',proposal=None)
                    for s in record['shots']],state_corrections=[dict(prop_id='prop_01',state_id='prepared',
                    source_quote='已涂胶的小纸片',reason='原文已准备好，不增加涂胶工序。',
                    description='已涂胶的纸片。',description_en='An already glued patch.')])
        return record,advice

    def test_source_state_repair_preview_apply_keeps_offscreen_videos_and_requires_all_consumers(self):
        record,advice=self.state_fixture()
        original=(self.path/'record.json').read_bytes()
        result,_=self.review(advice,shot_ids=None,repair_source_states=True)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual((self.path/'record.json').read_bytes(),original)
        self.assertTrue(all(row['changed'] and not row['render_changed'] for row in result.json()['shots']))
        body=dict(revision=record['revision'],review_id=result.json()['review_id'],shot_ids=['shot_001'])
        incomplete=self.client.post('/api/video-studio/'+record['id']+'/dynamic-review/apply',json=body)
        self.assertEqual(incomplete.status_code,400)
        self.assertEqual((self.path/'record.json').read_bytes(),original)
        body['shot_ids']=['shot_001','shot_002']
        applied=self.client.post('/api/video-studio/'+record['id']+'/dynamic-review/apply',json=body)
        self.assertEqual(applied.status_code,200,applied.text)
        for shot in applied.json()['shots']:
            self.assertEqual(shot['video'],'keep.mp4')
            self.assertEqual(shot['video_status'],'completed')
            self.assertEqual(shot['continuity_states'][0]['opening']['description'],'已涂胶的纸片。')

    def test_source_state_repair_rejects_unapproved_quote_unknown_id_and_partial_scope(self):
        from backend.app.video_dynamic_review import Advice,candidates
        record,advice=self.state_fixture()
        for change in ({'source_quote':'原稿不存在的工序'},{'prop_id':'unknown'},{'state_id':'unknown'}):
            bad=copy.deepcopy(advice);bad['state_corrections'][0].update(change)
            with self.assertRaises(ValueError):candidates(record,Advice.model_validate(bad),['shot_001','shot_002'],True)
        with self.assertRaises(ValueError):candidates(record,Advice.model_validate(advice),['shot_001','shot_002'])
        partial=copy.deepcopy(advice);partial['shots']=partial['shots'][:1]
        with self.assertRaises(ValueError):candidates(record,Advice.model_validate(partial),['shot_001'],True)

    def test_invisible_preparation_condition_retains_visible_shot_but_visible_change_requires_redo(self):
        from backend.app.video_dynamic_review import Advice,candidates
        record,advice=self.state_fixture()
        record['shots'][0]['continuity_states'][0]['visibility']='opening'
        selected=['shot_001','shot_002']
        rows=candidates(record,Advice.model_validate(advice),selected,True)
        self.assertTrue(rows[0]['render_changed'])
        correction=advice['state_corrections'][0]
        correction.update(visible_change=False,visual_reason='仅纸片背面不可辨的薄胶层，位置形状颜色均不变。')
        rows=candidates(record,Advice.model_validate(advice),selected,True)
        self.assertTrue(rows[0]['changed']);self.assertFalse(rows[0]['render_changed'])
        self.assertEqual(rows[0]['state_shot']['video'],'keep.mp4')
        self.assertEqual(rows[0]['plan']['turns'],record['shots'][0]['native_dialogue']['turns'])
        correction['visual_reason']=''
        with self.assertRaises(ValueError):candidates(record,Advice.model_validate(advice),selected,True)


if __name__=='__main__':unittest.main()
