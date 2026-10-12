import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.app import video_auto_cast as auto, video_studio as studio
from backend.app.video_cast_planning import saved_planning_cast,validate_planning_cast


class AutoCastTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'1'/('a'*32);self.path.mkdir(parents=True)
        self.record={'id':'a'*32,'revision':1,'status':'draft','logs':[], 'shots':[],
          'creation_parameters':{'auto_character_design':True},'references':[],
          'settings':{'style':'蜡笔绘本'},'scenes':[{'text':'小兔找到了乌龟。'}]}
        self.rows=[{'name':name,'basis':'小兔找到了乌龟','appearance':name+'的固定造型','image_prompt':'单角色全身'} for name in ['小兔','乌龟']]
        self.ask=Mock(return_value={'characters':self.rows})
        self.render=Mock(side_effect=self.make_image)
        self.save=Mock()
    def make_image(self,macro,configs):
        path=Path(macro['_output_path']);Image.new('RGB',(12,12),'white').save(path);return path
    def ensure(self):
        return auto.ensure_auto_cast(1,self.path,self.record,persist=self.save,progress=lambda s:None,
            image_configs=lambda:[{'model':'fake'}],render=self.render,ask=self.ask)
    def test_new_cast_has_separate_provenance_and_valid_planning_snapshot(self):
        self.assertTrue(self.ensure())
        self.assertEqual(self.record['project_cast']['source'],'auto_designed_project_identity')
        self.assertEqual(len(validate_planning_cast(saved_planning_cast(self.record))['characters']),2)
        self.assertEqual(self.render.call_count,2)
        self.assertFalse(self.ensure());self.assertEqual(self.render.call_count,2)
    def test_failed_second_image_resumes_without_repeating_first(self):
        n=[0]
        def render(macro,configs):
            n[0]+=1
            if n[0]==2:raise ValueError('synthetic failure')
            return self.make_image(macro,configs)
        self.render.side_effect=render
        with self.assertRaises(ValueError):self.ensure()
        self.assertEqual(len(self.record['references']),0)
        self.ensure();self.assertEqual(n[0],3);self.assertEqual(self.ask.call_count,1)
    def test_modified_generated_file_is_not_silently_overwritten(self):
        self.render.side_effect=[self.path/'wrong-output']
        with self.assertRaises(ValueError):self.ensure()
    def test_existing_manual_cast_is_preserved(self):
        self.record['project_cast']={'characters':[{'name':'existing'}]}
        old=copy.deepcopy(self.record);self.assertFalse(self.ensure())
        self.assertEqual(self.record,old);self.ask.assert_not_called()
    def test_existing_unbound_references_require_binding(self):
        self.record['references']=[{'id':'uploaded'}]
        with self.assertRaises(ValueError):self.ensure()
        self.ask.assert_not_called()
    def test_unanchored_design_does_not_generate_images(self):
        self.rows[0]['basis']='原文没有的角色'
        with self.assertRaises(ValueError):self.ensure()
        self.render.assert_not_called()
    def test_invalid_source_evidence_is_repaired_before_any_image_call(self):
        bad=copy.deepcopy(self.rows);bad[0]['basis']='小兔见到了乌龟'
        def ask(system,data):
            self.render.assert_not_called()
            if 'repair' not in data:return {'characters':bad}
            self.assertEqual(data['repair']['previous']['characters'],bad)
            self.assertIn('basis',data['repair']['error'])
            self.assertEqual(data['text'],'小兔找到了乌龟。')
            return {'characters':self.rows}
        self.ask.side_effect=ask
        self.assertTrue(self.ensure())
        self.assertEqual(self.ask.call_count,2)
        self.assertEqual(self.render.call_count,2)
        self.assertEqual(self.record['auto_cast_design']['previous']['characters'],bad)
    def test_failed_repair_is_preserved_and_resume_does_not_repeat_model_calls(self):
        self.rows[0]['basis']='并不在原文里的句子'
        with self.assertRaisesRegex(ValueError,'basis'):self.ensure()
        snapshot=copy.deepcopy(self.record['auto_cast_design'])
        with self.assertRaisesRegex(ValueError,'basis'):self.ensure()
        self.assertEqual(self.ask.call_count,2)
        self.assertEqual(self.record['auto_cast_design'],snapshot)
        self.render.assert_not_called()
    def test_repair_cannot_hide_invalid_characters_by_returning_empty_roster(self):
        bad=copy.deepcopy(self.rows);bad[0]['basis']='不在原文'
        self.ask.side_effect=[{'characters':bad},{'characters':[]}]
        with self.assertRaisesRegex(ValueError,'丢弃了全部角色'):self.ensure()
        self.render.assert_not_called()
    def test_explicit_retry_archives_failed_design_then_uses_valid_new_evidence(self):
        valid=copy.deepcopy(self.rows)
        self.rows[0]['basis']='原文没有的句子'
        with self.assertRaises(ValueError):self.ensure()
        prior=copy.deepcopy(self.record['auto_cast_design'])
        auto.retry_failed_cast_design(self.record)
        self.assertEqual(self.record['auto_cast_design_history'],[prior])
        self.ask.return_value={'characters':valid}
        self.assertTrue(self.ensure())
        self.assertEqual(self.ask.call_count,3)
        self.assertEqual(self.render.call_count,2)
    def test_empty_roster_is_cached(self):
        self.ask.return_value={'characters':[]}
        self.assertFalse(self.ensure());self.assertFalse(self.ensure())
        self.assertEqual(self.ask.call_count,1);self.render.assert_not_called()
    def test_planning_worker_receives_generated_identities_before_storyboard(self):
        from contextlib import ExitStack,nullcontext
        from types import SimpleNamespace
        import module4_video_render as visual
        self.record['settings'].update(characters='',world='')
        self.record['scenes'][0].update(slide_id='scene_001',start=0,end=4)
        studio.save(self.path,self.record)
        workers=[]
        def ensure(*args,**kwargs):
            return auto.ensure_auto_cast(*args,**kwargs,ask=self.ask)
        def plan(scenes,style,characters,world,refs,progress,parameters,**kwargs):
            self.assertEqual(len(refs),2)
            self.assertEqual(len(parameters['project_cast']['characters']),2)
            self.assertEqual(parameters['project_cast']['source'],'auto_designed_project_identity')
            return {'project_cast':parameters['project_cast']},[]
        with ExitStack() as stack:
            for target,name,value in [(studio,'ROOT',self.path.parent.parent),(studio,'ACTIVE',set()),
                 (studio,'require_user',lambda request:{'id':1}),
                 (studio,'project_language_scope',lambda *args,**kw:nullcontext()),
                 (studio,'plan_storyboard',plan),(studio,'_image_configs',lambda *args:[{'model':'fake'}]),
                 (auto,'ensure_auto_cast',ensure),
                 (visual,'_render_poster_with_retry',self.make_image),
                 (visual,'shared_runninghub_account_pool',lambda configs:configs)]:
                # Keep a reference to the original function to avoid patch recursion.
                if target is auto:
                    original=auto.ensure_auto_cast
                    value=lambda *args,**kw:original(*args,**kw,ask=self.ask)
                stack.enter_context(patch.object(target,name,value))
            stack.enter_context(patch.object(studio.threading,'Thread',lambda target,**kw:SimpleNamespace(start=lambda:workers.append(target))))
            studio.plan(self.record['id'],None);self.assertEqual(len(workers),1);workers[0]()
            import json
            result=json.loads((self.path/'record.json').read_text(encoding='utf-8'))
            self.assertEqual(result['status'],'storyboard_review',result.get('error'))
            self.assertEqual(len(result['project_cast']['characters']),2)
    def test_repair_archives_old_images_and_keeps_audio_and_groups(self):
        self.record.update(status='video_generation_ready',audio={'path':'unchanged.wav'},
            shots=[{'id':'shot1','slide_ids':['scene_001'],'duration':7,'image':'old.jpg'}])
        studio.save(self.path,self.record)
        (self.path/'old.jpg').write_bytes(b'original-image')
        app=FastAPI();app.include_router(studio.router)
        with patch.object(studio,'ROOT',self.path.parent.parent),patch.object(studio,'ACTIVE',set()), \
             patch.object(studio,'require_user',return_value={'id':1}),patch.object(studio,'plan',return_value={'status':'planning'}) as plan,TestClient(app) as client:
            url='/api/video-studio/'+self.record['id']+'/auto-characters'
            self.assertEqual(client.post(url,json={'revision':0}).status_code,409)
            result=client.post(url,json={'revision':1});self.assertEqual(result.status_code,200,result.text)
            plan.assert_called_once()
        import json
        after=json.loads((self.path/'record.json').read_text(encoding='utf-8'))
        self.assertEqual(after['audio'],self.record['audio'])
        self.assertEqual(after['scenes'],self.record['scenes'])
        self.assertEqual(after['manual_groups'],[{'id':'shot1','slide_ids':['scene_001']}])
        self.assertEqual((self.path/'old.jpg').read_bytes(),b'original-image')
        self.assertEqual(len(list((self.path/'revision_history').glob('*.json'))),1)
        self.assertFalse(after['creation_parameters']['dynamic_auto_advance'])


if __name__=='__main__':unittest.main()
