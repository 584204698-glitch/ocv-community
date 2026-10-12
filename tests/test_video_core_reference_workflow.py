"""Normal image routes and worker, real files/cast/cache; no provider calls."""
import copy
import io
import json
import unittest
from unittest.mock import Mock, patch
from PIL import Image
from backend.app import video_studio as studio, video_cast_planning as cast
from backend.app import video_core_references as refs
from backend.app.video_project_cast import build_project_cast
from backend.app.video_native_image_quality import screen_group
from tests import test_video_image_recovery as fixtures


class CoreReferenceWorkflowTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.VideoImageRecoveryTest();f.setUp();self.addCleanup(f.doCleanups);self.f=f
        r=f.record;r['native_story']={'text':'Three friends wait beside a lamp.'}
        r.setdefault('creation_parameters', {})['quality_review_policy']='strict'
        for asset in r['scene_assets']:asset['name']=asset['id']
        r['references']=[]
        for index,color in enumerate(('red','blue','yellow')):
            filename=f'actor{index}.jpg';f.write_image(filename,color)
            r['references'].append(dict(id=f'actor{index}',label=f'图{index+1}',kind='character',file=filename))
        r['project_cast']=build_project_cast(1,f.path,r,r['references'],[
            dict(name=f'Actor{index}',reference_id=f'actor{index}',constraints='Keep identity') for index in range(3)])
        snapshot=cast.saved_planning_cast(r)
        plan=cast.bind_planned_characters(snapshot,[dict(project_character_id=row['project_character_id'],
            presence='opening',basis='All three friends wait together.') for row in snapshot['characters']])
        r['shots']=[]
        for index in range(3):
            shot=f.shot(f'core{index}','scene-A',completed=False)
            shot.update(image_status='pending',image_error='',reference_ids=['actor0','actor1','actor2'],
                cast_plan=copy.deepcopy(plan),
                continuity_props=[dict(id='prop_lamp',presence='opening',appearance='蓝色台灯',appearance_en='A blue lamp.')],
                continuity_states=[dict(prop_id='prop_lamp',visibility='opening',
                    opening=dict(id='table',description='台灯在桌子上',description_en='The lamp is on the table.'))])
            r['shots'].append(shot)
        f.bulk_prepare.side_effect=None
        f.stack.enter_context(patch('backend.app.video_scene_quality.check_native_scene_images'))
        f.stack.enter_context(patch.dict('os.environ',{'RUNNINGHUB_PER_KEY_CONCURRENCY':'1'}))
        self.issue=False
        def answer(system,evidence,media):
            ids=evidence['image_order']
            return dict(observations=[dict(shot_id=identity,description='All three identities and the blue lamp are visible.') for identity in ids],
                continuity_checks=[],uncertainties=[],issues=([dict(shot_id=ids[0],code='opening_state',
                    evidence='The lamp is missing.',correction='Add the approved blue lamp on the table.')] if self.issue else []))
        self.ask=Mock(side_effect=answer)
        f.stack.enter_context(patch.object(refs,'screen_group',side_effect=lambda path,shots,**kw:
            screen_group(path,shots,ask=self.ask,cached_only=kw.get('cached_only',False))))
        studio.save(f.path,r)

    def generate(self):
        response=self.f.client.post(self.f.base+'/images/generate',json={'revision':self.f.read()['revision']})
        self.assertEqual(response.status_code,200,response.text)
        self.f.run_worker()
        return self.f.read()

    def test_three_role_normal_batch_uses_checked_first_core_in_same_fourth_slot(self):
        after=self.generate()
        self.assertEqual(after['error'],'',after['logs'])
        self.assertEqual([s['image_status'] for s in after['shots']],['completed']*3)
        self.assertEqual(self.ask.call_count,1)
        requests=[call.args[0] for call in self.f.renderer.call_args_list]
        self.assertEqual(len(requests),3)
        source=str((self.f.path/after['shots'][0]['image']).resolve())
        self.assertNotEqual(requests[0]['reference_image_paths'][-1],source)
        for request in requests[1:]:
            self.assertEqual(len(request['reference_image_paths']),4)
            self.assertEqual(request['reference_image_paths'][-1],source)
            self.assertIn('图4是同场景',request['image_prompt'])
        self.assertEqual(after['shots'][1]['core_reference_used']['source_shot_id'],'core0')
        self.assertEqual(studio.storyboard_review_error(self.f.path,after),'')
        self.assertEqual(refs.dependency_error(self.f.path,after,after['shots'][2]),'')

    def test_rejected_source_stops_children_and_normal_retry_does_not_resubmit_source_or_review(self):
        self.issue=True
        after=self.generate()
        self.assertIn('源图检查',after['error'])
        self.assertEqual([s['image_status'] for s in after['shots']],['completed','pending','pending'])
        self.assertEqual(self.f.renderer.call_count,1)
        self.generate()
        self.assertEqual(self.f.renderer.call_count,1)
        self.assertEqual(self.ask.call_count,1)

    def test_changed_cast_and_repair_use_appearance_in_normal_ordered_worker(self):
        record=self.f.read()
        for shot in record['shots'][1:]:
            shot['cast_plan']['characters']=shot['cast_plan']['characters'][:1]
            shot['reference_ids']=['actor0']
            shot['continuity_states'][0]['opening']=dict(id='repaired',description='台灯修补后仍未亮',
                description_en='The same blue lamp is repaired and remains unlit on the table.')
            shot['image_prompt']+=' 本镜批准动作：查看修补后的灯。'
        before=copy.deepcopy(record['shots'])
        studio.save(self.f.path,record)
        after=self.generate()
        self.assertEqual(after['error'],'',after['logs'])
        self.assertEqual([shot['image_status'] for shot in after['shots']],['completed']*3)
        self.assertEqual(self.ask.call_count,1)
        requests=[call.args[0] for call in self.f.renderer.call_args_list]
        source=str((self.f.path/after['shots'][0]['image']).resolve())
        for index in (1,2):
            shot=after['shots'][index]
            self.assertEqual(shot['core_reference_used']['mode'],'appearance_only')
            self.assertEqual(shot['core_reference_used']['source_shot_id'],'core0')
            self.assertEqual(requests[index]['reference_image_paths'][-1],source)
            self.assertIn('appearance_only',requests[index]['image_prompt'])
            self.assertIn('不复制参考图中的旧损坏',requests[index]['image_prompt'])
            for key in ('cast_plan','continuity_states','native_dialogue'):
                self.assertEqual(shot.get(key),before[index].get(key))
            self.assertTrue(shot['image_prompt'].startswith(before[index]['image_prompt']))
            self.assertEqual(refs.dependency_error(self.f.path,after,shot),'')

    def test_rejected_appearance_source_stops_children_without_weaker_reference_fallback(self):
        record=self.f.read()
        for shot in record['shots'][1:]:
            shot['continuity_states'][0]['opening']['description']='台灯已经修补，仍未亮'
        studio.save(self.f.path,record)
        self.issue=True
        after=self.generate()
        self.assertIn('源图检查',after['error'])
        self.assertEqual([shot['image_status'] for shot in after['shots']],['completed','pending','pending'])
        self.assertEqual(self.f.renderer.call_count,1)
        self.assertEqual(self.ask.call_count,1)

    def test_source_upload_invalidates_only_actual_users_and_bulk_retry_keeps_original_files(self):
        after=self.generate();old=copy.deepcopy(after['shots'][1]);original=self.f.files()
        independent=self.f.shot('independent','scene-B');after['shots'].append(independent)
        studio.save(self.f.path,after)
        data=io.BytesIO();Image.new('RGB',(32,24),'orange').save(data,format='PNG')
        response=self.f.client.post(self.f.base+'/images/core0/upload',
            data={'revision':str(after['revision'])},files={'file':('new.png',data.getvalue(),'image/png')})
        self.assertEqual(response.status_code,200,response.text)
        edited=self.f.read()
        self.assertTrue(refs.dependency_error(self.f.path,edited,edited['shots'][1]))
        self.assertIn('换版',edited['shots'][1]['core_reference_error'])
        self.assertEqual(edited['shots'][3],independent)
        refreshed=self.generate()
        self.assertEqual(refreshed['shots'][3],independent)
        self.assertEqual(self.f.renderer.call_count,5)  # 3 originals + 2 dependents
        self.assertNotEqual(refreshed['shots'][1]['image'],old['image'])
        self.assertEqual((self.f.path/old['image']).read_bytes(),original[old['image']])
        self.assertEqual(refs.dependency_error(self.f.path,refreshed,refreshed['shots'][1]),'')

    def test_manual_redraw_uses_same_reference_and_undo_preserves_saved_lineage(self):
        after=self.generate();binding=copy.deepcopy(after['shots'][1]['core_reference_used'])
        after.pop('core_reference_policy');studio.save(self.f.path,after)
        response=self.f.client.post(self.f.base+'/images/core1/redraw',json=dict(revision=after['revision'],
            prompt='Keep the lamp and room, simplify the shadows.',reference_ids=['actor0','actor1','actor2'],
            use_current_image=False,use_scene_reference=True))
        self.assertEqual(response.status_code,200,response.text);self.f.run_worker()
        edited=self.f.read();self.assertEqual(edited['shots'][1]['core_reference_used'],binding)
        self.assertEqual(edited['core_reference_policy'],1)
        self.assertEqual(edited['shots'][1]['image_history'][-1]['core_reference_used'],binding)
        response=self.f.client.post(self.f.base+'/images/core1/undo',json={'revision':edited['revision']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.f.read()['shots'][1]['core_reference_used'],binding)

    def test_manual_request_binds_actual_reordered_ids_with_current_image_offset(self):
        after=self.generate();shot=after['shots'][1]
        selected=[str(self.f.path/shot['image']),str(self.f.path/'actor2.jpg'),str(self.f.path/'actor0.jpg')]
        prompt,paths,_=studio._image_inputs(self.f.path,after,shot,
            '新的半身构图。\n【已选项目角色】\n图1绑定过期角色。',references=selected,use_scene=False)
        self.assertEqual(paths,selected)
        self.assertIn('图2只绑定开场角色“Actor2”',prompt)
        self.assertIn('图3只绑定开场角色“Actor0”',prompt)
        self.assertNotIn('开场角色“Actor1”',prompt)
        self.assertNotIn('过期角色',prompt)
        again,_,_=studio._image_inputs(self.f.path,after,shot,prompt,references=selected,use_scene=False)
        self.assertEqual(prompt,again)

    def test_unknown_old_dependent_receipt_prevents_new_reservation_and_preserves_whole_record(self):
        from backend.app import openai_image_transport as transport
        after=self.generate();source=after['shots'][0];target=after['shots'][1]
        self.f.write_image(source['image'],'orange');source['image_version']=self.f.file_hash(source['image'])
        receipt=transport.ledger_path(self.f.path/target['image'])
        receipt.write_text(json.dumps(dict(status='unknown',request_sha256='original',attempt_id='paid-original')),encoding='utf-8')
        before=copy.deepcopy(after);receipt_bytes=receipt.read_bytes()
        with self.assertRaises(transport.OpenAIImageError):
            studio._reserve_stale_scene_images(self.f.path,after)
        self.assertEqual(after,before)
        self.assertEqual(receipt.read_bytes(),receipt_bytes)
        self.assertEqual(self.f.renderer.call_count,3)

    def test_changed_core_dependency_cannot_reach_new_video_inputs_or_export(self):
        from backend.app import video_generation
        after=self.generate();source=after['shots'][0]
        self.f.write_image(source['image'],'orange');source['image_version']=self.f.file_hash(source['image'])
        with self.assertRaisesRegex(ValueError,'源图已换版'):
            video_generation._inputs(self.f.path,after,after['shots'][1])
        for shot in after['shots']:
            shot['video_status']='completed'
            shot['video']='assets/videos/'+shot['id']+'.mp4'
            target=self.f.path/shot['video'];target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(b'Existing artifact; decoding must never occur in this rejected export.')
        after['status']='video_review';studio.save(self.f.path,after)
        response=self.f.client.post(self.f.base+'/export',json={'revision':after['revision']})
        self.assertEqual(response.status_code,409,response.text)
        self.assertIn('源图已换版',response.text)
        self.assertEqual(self.f.workers,[])
