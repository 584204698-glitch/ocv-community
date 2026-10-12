"""Recovery changes one preparation identity, never creative work or paid jobs."""
import copy
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.app import video_studio as studio
from tests.test_video_inputs import VideoInputTest
from tests.test_h3_dialogue import data


class VideoRecoveryTests(unittest.TestCase):
    def setUp(self):
        VideoInputTest.setUp(self)
        self.record['status']='video_review'
        shot=self.record['shots'][0]
        shot.update(native_dialogue=data(),video_status='failed',video_terminal=True,
                    video_error='开场对接缺少已批准角色的站位，未提交H3')
        studio.save(self.path,self.record)
        app=FastAPI();app.include_router(studio.router)
        self.client=TestClient(app)

    def submit(self,revision=1):
        return self.client.post(self.base+'/shots/shot-A/retry-preparation',json={'revision':revision})

    def test_retry_preserves_assets_dialogue_other_shots_and_is_idempotent_until_consumed(self):
        before=copy.deepcopy(self.record)
        image=(self.path/'core.png').read_bytes()
        with patch('backend.app.video_native_grounding.ground_performance',side_effect=AssertionError('not here')):
            response=self.submit()
        self.assertEqual(response.status_code,200,response.text)
        out=response.json()
        self.assertEqual(out['shots'][1],before['shots'][1])
        shot=out['shots'][0]
        for k,v in before['shots'][0].items():self.assertEqual(shot[k],v,k)
        self.assertEqual(shot['native_grounding_retry'],1)
        self.assertTrue(shot['native_grounding_force_recheck'])
        self.assertEqual((self.path/'core.png').read_bytes(),image)
        self.assertEqual(self.submit(out['revision']).json()['revision'],out['revision'])
        self.assertEqual(self.submit(1).status_code,409)

    def test_unresolved_paid_task_and_completed_clip_cannot_be_reset(self):
        for changes in ({'video_task_id':'paid-id','video_terminal':False},
                        {'video_resume_available':True}, {'video_status':'completed'}):
            with self.subTest(changes=changes):
                record=copy.deepcopy(self.record);record['shots'][0].update(changes)
                studio.save(self.path,record)
                self.assertEqual(self.submit().status_code,409)
                self.assertNotIn('native_grounding_retry',studio.read(self.path)['shots'][0])

    def test_ownership_and_running_task_remain_enforced(self):
        with patch.object(studio,'require_user',return_value={'id':2}):
            self.assertEqual(self.submit().status_code,404)
        studio.ACTIVE.add(str(self.path))
        try:self.assertEqual(self.submit().status_code,409)
        finally:studio.ACTIVE.discard(str(self.path))

if __name__=='__main__':unittest.main()
