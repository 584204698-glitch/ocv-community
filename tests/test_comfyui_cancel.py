import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from backend.app import comfyui_cancel as cancel
from backend.app import comfyui_guard as guard


class ScopedCancelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.folder=self.root/'attempt_001/comfyui_submission'
        self.folder.mkdir(parents=True)
        self.receipt=dict(version=1,task_id='a'*32,state='submitted',graph_sha256='b'*64,
                          base_url=guard.TARGET,prompt_path='/prompt',script=str(self.root/'h3_guard.py'),
                          pid=12345,process_identity='1',prompt_id='owned',delivered=True)
        guard._write_json(self.folder/'receipt.json',self.receipt)
        guard._write_json(self.root/'gpu-comfyui-state.json',dict(status='HELD',owner='ocv',
                         task_id='a'*32,pid=12345,prompt_id='owned'))
        self.connection=dict(base_url=guard.TARGET,prompt_path='/prompt')
        self.empty=dict(queue_running=[],queue_pending=[])
        self.initial=dict(queue_running=[[0,'owned']],queue_pending=[])
        self.terminal=dict(status=dict(status_str='error',completed=False))
        self.alive=True;self.queues=0

    def get(self,url,**kwargs):
        if url.endswith('/queue'):
            self.queues+=1;payload=self.initial if self.queues==1 else self.empty
        else:
            payload={'owned':self.terminal} if self.terminal else {}
            if self.terminal:self.alive=False
        return Mock(json=lambda:payload)

    def run_cancel(self):
        with patch.object(guard,'_alive',side_effect=lambda *a,**k:self.alive), \
             patch.object(cancel.requests,'get',side_effect=self.get), \
             patch.object(cancel.requests,'post') as post, \
             patch.object(cancel,'targeted_support',return_value=True), \
             patch.object(cancel.os,'kill',side_effect=lambda *a:setattr(self,'alive',False)) as kill:
            result=cancel.cancel(self.connection,self.folder)
        return result,post,kill

    def test_running_task_is_interrupted_by_id_only_and_release_confirmed(self):
        result,post,kill=self.run_cancel()
        self.assertEqual(result['status'],'cancelled')
        post.assert_called_once_with(guard.TARGET+'/interrupt',json={'prompt_id':'owned'},timeout=10)
        kill.assert_not_called()
        self.assertTrue(json.loads((self.folder/'cancel.json').read_text(encoding='utf8'))['terminal'])

    def test_pending_task_is_deleted_by_id_and_only_its_waiting_guard_stopped(self):
        self.initial=dict(queue_running=[],queue_pending=[[0,'owned']]);self.terminal={}
        result,post,kill=self.run_cancel()
        self.assertEqual(result['status'],'cancelled')
        post.assert_called_once_with(guard.TARGET+'/queue',json={'delete':['owned']},timeout=10)
        kill.assert_called_once_with(12345,cancel.signal.SIGTERM)

    def test_foreign_ownership_cannot_interrupt_or_delete(self):
        guard._write_json(self.root/'gpu-comfyui-state.json',dict(status='HELD',owner='other',
                         task_id='a'*32,pid=12345,prompt_id='owned'))
        with patch.object(guard,'_alive',return_value=True),patch.object(cancel.requests,'get',side_effect=self.get),patch.object(cancel.requests,'post') as post:
            with self.assertRaisesRegex(ValueError,'归属不匹配'):cancel.cancel(self.connection,self.folder)
        post.assert_not_called()

    def test_no_terminal_history_does_not_prove_cancellation(self):
        self.initial=self.empty;self.terminal={}
        with patch.object(cancel.requests,'get',side_effect=self.get),patch.object(cancel.requests,'post') as post:
            with self.assertRaisesRegex(ValueError,'无终态证据'):cancel.cancel(self.connection,self.folder)
        post.assert_not_called()

    def test_old_server_cannot_fall_back_to_global_interrupt(self):
        with patch.object(guard,'_alive',return_value=True),patch.object(cancel.requests,'get',side_effect=self.get),patch.object(cancel.requests,'post') as post,patch.object(cancel,'targeted_support',return_value=False):
            with self.assertRaisesRegex(ValueError,'未验证'):cancel.cancel(self.connection,self.folder)
        post.assert_not_called()

    def test_completed_task_preserves_result_without_cancelling_it(self):
        self.initial=self.empty;self.terminal=dict(status=dict(status_str='success',completed=True))
        result,post,kill=self.run_cancel()
        self.assertEqual(result['status'],'already_finished')
        post.assert_not_called();kill.assert_not_called()
