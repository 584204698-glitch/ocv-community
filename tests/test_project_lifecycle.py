import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from fastapi import HTTPException
from backend.app import video_studio as studio


class ProjectLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        for key,value in [('ROOT',self.root),('ACTIVE',set()),('IMAGE_EDITS',set()),('require_user',lambda _: {'id':1})]:
            p=patch.object(studio,key,value);p.start();self.addCleanup(p.stop)
        self.identity='a'*32;self.folder=studio.directory(1,self.identity)
        self.record=dict(id=self.identity,revision=1,status='storyboard_review',settings={'name':'保留配音'},
                         scenes=[],shots=[],references=[],creation_parameters={},logs=[],audio='audio.wav')
        studio.save(self.folder,self.record);(self.folder/'audio.wav').write_bytes(b'original-audio')

    def test_archive_restore_preserves_assets_and_list_identity(self):
        archived=studio.archive_project(self.identity,studio.ArchiveReview(revision=1),None)
        self.assertTrue(archived['archived'])
        self.assertEqual((self.folder/'audio.wav').read_bytes(),b'original-audio')
        with self.assertRaises(HTTPException) as error:studio.read(self.folder)
        self.assertEqual(error.exception.status_code,410)
        self.assertEqual(studio.listing(None)['items'][0]['id'],self.identity)
        restored=studio.archive_project(self.identity,studio.ArchiveReview(revision=2,archived=False),None)
        self.assertFalse(restored['archived']);self.assertEqual(restored['status'],'storyboard_review')
        self.assertEqual(studio.read(self.folder)['id'],self.identity)

    def test_owner_and_revision_guards(self):
        with patch.object(studio,'require_user',return_value={'id':2}):
            with self.assertRaises(HTTPException) as error:studio.archive_project(self.identity,studio.ArchiveReview(revision=1),None)
            self.assertEqual(error.exception.status_code,404)
        with self.assertRaises(HTTPException) as error:studio.archive_project(self.identity,studio.ArchiveReview(revision=0),None)
        self.assertEqual(error.exception.status_code,409)
        self.assertFalse(studio.read(self.folder).get('archived'))

    def test_active_work_and_image_edit_refuse_archive(self):
        studio.ACTIVE.add(str(self.folder))
        with self.assertRaises(HTTPException):studio.archive_project(self.identity,studio.ArchiveReview(revision=1),None)
        studio.ACTIVE.clear();studio.IMAGE_EDITS.add(self.identity+':shot')
        with self.assertRaises(HTTPException):studio.archive_project(self.identity,studio.ArchiveReview(revision=1),None)

    def test_archived_bound_audio_cannot_create_duplicate(self):
        job=SimpleNamespace(user_id=1,id='job',status='waiting_confirmation',request={'dynamic_video':True,'_dynamic_video_project_id':self.identity})
        record=studio.read(self.folder);record['source_project']={'id':'job'};record['creation_parameters']={'dynamic_video':True};record['archived']=True;studio.save(self.folder,record)
        with patch('backend.app.video_sources.dependencies',return_value=(Mock(get=Mock(return_value=job)),Mock(),Mock())):
            with self.assertRaises(HTTPException) as error:studio.find_audio_storyboard(1,'job')
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(len(list(self.root.glob('1/*/record.json'))),1)

    def test_legacy_default_backend_roundtrip_preserves_native_images(self):
        from backend.app import comfyui_bridge
        record=studio.read(self.folder)
        record.update(status='video_generation_ready', native_story={'text':'The same story'},
            shots=[{'id':'shot1','kind':'video','image':'assets/storyboards/shot1.jpg',
                    'image_status':'completed','image_version':'unchanged','video_status':'pending'}],
            context={'summary':'approved'}, scene_assets=[{'id':'scene1','image_status':'completed'}])
        studio.save(self.folder,record)
        record['creation_parameters']['dynamic_text_mode']='visual_first'
        studio.save(self.folder,record)
        before=copy.deepcopy(record)
        profile={'video_input_mode':'composition_reference','workflow':{},'mappings':{}}
        with patch.object(comfyui_bridge,'video_profile',return_value=profile), \
             patch.object(comfyui_bridge,'_patch_voice_references'), \
             patch.object(comfyui_bridge,'_api_graph',return_value={}):
            result=studio.edit_project_settings(self.identity,studio.ProjectSettingsEdit(
                revision=1,name='????',video_generation_backend='comfyui',
                parameters={'visual_backend':'poster','video_render_variant':'both'}),None)
        self.assertEqual(result['status'],'video_generation_ready')
        self.assertEqual(result['shots'],before['shots'])
        self.assertEqual(result['context'],before['context'])
        self.assertEqual(result['scene_assets'],before['scene_assets'])
        self.assertIsNone(result['edit_history'][-1]['shots'])
        self.assertEqual(result['creation_parameters']['visual_backend'],'poster')

    def test_real_backend_change_still_invalidates_visual_design(self):
        record=studio.read(self.folder)
        record['shots']=[{'id':'shot1','kind':'static','image_status':'completed'}]
        record['creation_parameters']['visual_backend']='poster'
        studio.save(self.folder,record)
        result=studio.edit_project_settings(self.identity,studio.ProjectSettingsEdit(
            revision=1,name='????',parameters={'visual_backend':'html'}),None)
        self.assertEqual(result['status'],'draft')
        self.assertEqual(result['shots'],[])
        self.assertEqual(len(result['edit_history'][-1]['shots']),1)

    def recovery_fixture(self):
        import hashlib
        record=studio.read(self.folder)
        source={'slide_id':'s1','start':0,'end':4,'text':'Same story'}
        record['scenes']=[source]
        image=self.folder/'assets/storyboards/shot1.jpg'
        image.parent.mkdir(parents=True,exist_ok=True);image.write_bytes(b'verified picture')
        shot={'id':'shot1','kind':'static','slide_ids':['s1'],'source_subtitles':[source],
              'image':'assets/storyboards/shot1.jpg','image_status':'completed',
              'image_version':hashlib.sha256(image.read_bytes()).hexdigest()}
        record['edit_history']=[{'settings':copy.deepcopy(record['settings']),
            'creation_parameters':copy.deepcopy(record['creation_parameters']),
            'status':'video_generation_ready','shots':[copy.deepcopy(shot)]}]
        record['creation_parameters']['visual_backend']='poster'
        record['project_voices']={'retained':'binding'}
        studio.save(self.folder,record)
        return record,image

    def test_restores_verified_settings_snapshot_without_recreating_images(self):
        record,image=self.recovery_fixture()
        with patch.object(studio,'validate_cast_shot'):
            result=studio.restore_settings_assets(self.identity,studio.Review(revision=1),None)
        self.assertEqual(result['status'],'image_review')
        self.assertEqual(result['shots'],record['edit_history'][0]['shots'])
        self.assertEqual(result['project_voices'],record['project_voices'])
        self.assertEqual(image.read_bytes(),b'verified picture')
        self.assertEqual(result['revision'],2)

    def test_restore_rejects_changed_story_hash_settings_and_cast(self):
        for failure in ('story','hash','settings','cast','existing','revision'):
            with self.subTest(failure=failure):
                record,image=self.recovery_fixture()
                if failure=='story':record['scenes'][0]['text']='Changed story'
                if failure=='hash':image.write_bytes(b'changed')
                if failure=='settings':record['creation_parameters']['visual_backend']='html'
                if failure=='existing':record['shots']=[{'image':'existing.jpg'}]
                studio.save(self.folder,record)
                before=(self.folder/'record.json').read_bytes()
                with patch.object(studio,'validate_cast_shot',side_effect=ValueError('changed cast') if failure=='cast' else None):
                    with self.assertRaises(HTTPException):
                        studio.restore_settings_assets(self.identity,studio.Review(revision=0 if failure=='revision' else 1),None)
                self.assertEqual((self.folder/'record.json').read_bytes(),before)
                record['shots']=[];record['creation_parameters']={};record['scenes']=[]
                studio.save(self.folder,record)


    def test_cached_scene_metadata_requires_exact_plan_and_receipt(self):
        import hashlib
        import json
        from backend.app.openai_image_transport import ledger_path
        record,image=self.recovery_fixture()
        shots=copy.deepcopy(record['edit_history'][0]['shots'])
        shots.append({**copy.deepcopy(shots[0]),'id':'shot2'})
        mapping=[{'image_prompt':'same approved room','visual_design':{}} for _ in shots]
        entry={'scene_id':'location_1','name':'Room','members':[0,1],
               'reference_prompt':'An approved room.','reason':'same physical room'}
        settings=record['settings']
        source={'version':2,'items':[{'index':i,'prompt':item['image_prompt'],'visual_design':item['visual_design']} for i,item in enumerate(mapping)],
                'source':{'subtitles':record['scenes'],'world':'','ratio':'16:9'},'style':'',
                'coordination_contract':studio.scene_references.VIDEO_CONTRACT}
        plan={'version':1,'fingerprint':hashlib.sha256(json.dumps(source,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
              'scenes':[entry]}
        cache=self.folder/'scene_reference_plan.json'
        cache.write_text(json.dumps(plan),encoding='utf-8')
        identity='scene_ref_'+hashlib.sha256(json.dumps({'prompt':entry['reference_prompt'],
            'members':['shot1','shot2'],'style':'','world':'','ratio':'16:9'},sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
        scene_image=self.folder/f'assets/scene_references/{identity}.jpg'
        scene_image.parent.mkdir(parents=True);scene_image.write_bytes(b'scene pixels')
        digest=hashlib.sha256(scene_image.read_bytes()).hexdigest()
        ledger_path(scene_image).write_text(json.dumps({'output_sha256':digest}),encoding='utf-8')
        with patch.object(studio.scene_references,'scene_mapping',return_value=mapping),patch.object(studio,'saved_planning_cast',return_value=None):
            assets=studio._cached_scene_assets_for_restore(self.folder,record,shots)
            self.assertEqual(assets[0]['id'],identity)
            self.assertEqual(assets[0]['image_version'],digest)
            ledger_path(scene_image).write_text(json.dumps({'output_sha256':'changed'}),encoding='utf-8')
            with self.assertRaises(ValueError):studio._cached_scene_assets_for_restore(self.folder,record,shots)
            plan['fingerprint']='changed';cache.write_text(json.dumps(plan),encoding='utf-8')
            with self.assertRaises(ValueError):studio._cached_scene_assets_for_restore(self.folder,record,shots)

    def test_pacing_edit_reuses_audio_and_project(self):
        before=copy.deepcopy(studio.read(self.folder))
        result=studio.edit_project_settings(self.identity,studio.ProjectSettingsEdit(revision=1,name='保留配音',parameters={'visual_pacing_preset':'fast'}),None)
        self.assertEqual(result['id'],self.identity)
        self.assertEqual(result['audio'],before['audio']);self.assertEqual(result['scenes'],before['scenes'])
        self.assertEqual((self.folder/'audio.wav').read_bytes(),b'original-audio')
        self.assertEqual(result['status'],'draft')
        self.assertEqual(len(list(self.root.glob('1/*/record.json'))),1)

if __name__=='__main__':unittest.main()
