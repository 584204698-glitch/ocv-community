"""Real storyboard routes/workers with local renderers; never call a provider."""
import copy
import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
import wave
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import module4_video_render as visual
from backend.app import video_studio as studio
from backend.app import video_generation as generation, video_export as export, video_sources as sources


class VideoImageRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.real_prepare = studio._prepare_scene_assets
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = 'a' * 32
        self.path = self.root / '1' / self.identity
        self.path.mkdir(parents=True)
        self.base = '/api/video-studio/' + self.identity
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.workers = []

        def thread_factory(*, target, daemon, **kwargs):
            return SimpleNamespace(start=lambda: self.workers.append(target))

        for name, value in (
            ('ROOT', self.root), ('require_user', lambda request: {'id': 1}),
            ('ACTIVE', set()), ('IMAGE_EDITS', set()), ('CANCEL_EVENTS', {}),
            ('threading', SimpleNamespace(Thread=thread_factory, Event=threading.Event)),
        ):
            self.stack.enter_context(patch.object(studio, name, value))
        self.configs = self.stack.enter_context(patch.object(
            studio, '_image_configs', return_value=[{'protocol': 'openai_images', 'resolution': '1k'}]))
        self.stack.enter_context(patch.object(studio, '_image_language_scope', return_value=nullcontext()))
        self.stack.enter_context(patch.object(visual, 'shared_runninghub_account_pool', return_value=object()))
        self.renderer = self.stack.enter_context(patch.object(visual, '_render_poster_with_retry', side_effect=self.render))
        self.scene_planner = self.stack.enter_context(patch.object(
            studio.scene_references, 'plan_references', side_effect=AssertionError('Unexpected scene planning')))
        self.bulk_prepare = self.stack.enter_context(patch.object(
            studio, '_prepare_scene_assets', side_effect=AssertionError('Unexpected bulk scene preparation')))
        self.record = {
            'id': self.identity, 'revision': 7, 'status': 'image_review', 'logs': [], 'error': '',
            'settings': {'name': 'Recovery fixture', 'style': 'Clay', 'characters': '', 'world': '', 'ratio': '16:9'},
            'creation_parameters': {'dynamic_text_mode': 'visual_first', 'scene_references_enabled': True},
            'context': {'summary': 'Preserve the approved plan'},
            'scenes': [{'slide_id': 's1', 'start': 0, 'end': 4, 'text': 'A enters.'}],
            'references': [], 'scene_references_status': 'completed',
            'scene_assets': [], 'shots': [],
        }
        for identity, color in [('scene-A', 'green'), ('scene-B', 'blue')]:
            relative = 'assets/scenes/' + identity + '.jpg'
            self.write_image(relative, color)
            self.record['scene_assets'].append({
                'id': identity, 'asset_kind': 'scene_reference', 'image': relative,
                'image_prompt': 'Approved room ' + identity, 'image_status': 'completed',
                'image_version': self.file_hash(relative), 'used_by': [], 'fingerprint': identity + '-plan',
            })
        self.record['shots'] = [
            self.shot('failed-A', 'scene-A', completed=False),
            self.shot('dependent-B', 'scene-A'),
            self.shot('independent-C', 'scene-B'),
        ]
        self.record['scene_assets'][0]['used_by'] = ['failed-A', 'dependent-B']
        self.record['scene_assets'][1]['used_by'] = ['independent-C']
        self.plan_file = self.path / 'scene_reference_plan.json'
        self.plan_file.write_bytes(b'{ "scenes": ["approved-A", "approved-B"] }\n')
        self.old_output = 'assets/storyboards/failed-A.jpg'
        self.old_receipt = self.path / (self.old_output + '.openai-image.json')
        self.old_receipt.parent.mkdir(parents=True, exist_ok=True)
        self.old_receipt.write_bytes(b'{ "status": "unknown", "request_id": "original-unknown" }\n')
        studio.save(self.path, self.record)
        app = FastAPI()
        app.include_router(studio.router)
        app.include_router(generation.router)
        app.include_router(export.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def write_image(self, relative, color):
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (32, 24), color).save(target, format='JPEG')
        return target

    def file_hash(self, relative):
        return hashlib.sha256((self.path / relative).read_bytes()).hexdigest()

    def shot(self, identity, scene, *, completed=True):
        asset = next(row for row in self.record['scene_assets'] if row['id'] == scene)
        row = {'id': identity, 'kind': 'video', 'slide_ids': ['s1'], 'start': 0, 'end': 4,
               'duration': 4, 'generation_duration': 4, 'intent': identity,
               'action': 'A enters.', 'image_prompt': 'Opening ' + identity,
               'video_prompt': '图1保持同一房间。', 'reference_ids': [],
               'scene_reference_id': scene, 'image_status': 'completed' if completed else 'failed',
               'image_error': '' if completed else 'Prior request outcome unknown',
               'scene_reference_used_version': asset['image_version'] if completed else ''}
        if completed:
            row['image'] = 'assets/storyboards/' + identity + '.jpg'
            self.write_image(row['image'], 'white')
            row['image_version'] = self.file_hash(row['image'])
            row.update(video='assets/videos/' + identity + '.mp4', video_status='completed',
                       video_task_id='original-' + identity, video_request={'prompt': 'Frozen ' + identity},
                       video_version='video-' + identity, video_attempt=1)
            target = self.path / row['video']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(('original clip ' + identity).encode())
        return row

    def read(self):
        # Raw persisted record, so assertions do not themselves invoke recovery.
        return json.loads((self.path / 'record.json').read_text(encoding='utf-8'))

    def files(self):
        return {file.relative_to(self.path).as_posix(): file.read_bytes()
                for file in self.path.rglob('*') if file.is_file()}

    def render(self, macro, pool):
        target = Path(macro['_output_path'])
        self.assertTrue(target.resolve().is_relative_to(self.path.resolve()))
        Image.new('RGB', (32, 24), 'purple').save(target, format='JPEG')
        return target

    def retry(self, *, confirmed=True, shot='failed-A', **extra):
        return self.client.post(self.base + '/images/' + shot + '/retry', json={
            'revision': self.read()['revision'], 'confirm_new_attempt': confirmed, **extra})

    def run_worker(self):
        self.assertEqual(len(self.workers), 1)
        self.workers.pop()()
        self.assertFalse(studio.project_has_image_edits(self.identity))

    def complete_failed(self):
        record = self.read()
        record['shots'][0] = self.shot('failed-A', 'scene-A')
        record['export'] = {'raw': 'paid-final.mp4', 'subtitles': 'paid-subtitles.mp4'}
        studio.save(self.path, record)

    def confirm(self):
        return self.client.post(self.base + '/images/confirm', json={'revision': self.read()['revision']})

    def assert_readonly_rejection(self, call, expected=409):
        before = self.files()
        response = call()
        self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.workers, [])
        self.renderer.assert_not_called()
        return response

    def test_failed_auxiliary_repair_cannot_confirm_preserved_original(self):
        self.complete_failed()
        record=self.read()
        record['shots'][0]['image_task']={'action':'auto_repair','status':'failed'}
        studio.save(self.path,record)
        response=self.assert_readonly_rejection(self.confirm)
        self.assertIn('辅助修图尚未完成',response.json()['detail'])

    def open_local_edit(self, shot='independent-C', status='video_review'):
        record=self.read();record['status']=status;studio.save(self.path,record)
        response=self.client.post(self.base+'/shots/'+shot+'/reopen',json={'revision':record['revision']})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_local_exit_preserves_assets_despite_unrelated_failed_image_and_blocks_full_run(self):
        from backend.app import one_click
        before=self.read()['shots']
        self.open_local_edit()
        files=self.files();files.pop('record.json')
        response=self.confirm()
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertNotIn('reedit_shot_id',result)
        self.assertEqual(result['shots'],before)
        after=self.files();after.pop('record.json');self.assertEqual(after,files)
        self.assertEqual(result['status'],'video_generation_ready')
        with patch.object(generation,'generate_for_user') as generate, \
             patch.object(export,'start_export_for_user') as render:
            for action in ('videos','export'):
                with self.assertRaisesRegex(ValueError,'全部核心分镜图'):
                    one_click.dispatch(action,{'id':1},result)
            generate.assert_not_called();render.assert_not_called()
        self.assertEqual(self.workers,[]);self.renderer.assert_not_called()

    def test_unchanged_local_exit_retains_completed_export(self):
        self.complete_failed()
        before=self.read()
        self.open_local_edit(status='completed')
        response=self.confirm()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['status'],'completed')
        self.assertEqual(response.json()['export'],before['export'])
        self.assertEqual(response.json()['shots'],before['shots'])

    def test_local_exit_checks_only_used_scene_but_rejects_changed_dependency(self):
        self.open_local_edit()
        record=self.read();record['scene_references_status']='failed'
        record['scene_assets'][0]['image_status']='failed'
        studio.save(self.path,record)
        response=self.confirm()
        self.assertEqual(response.status_code,200,response.text)
        self.open_local_edit()
        record=self.read();record['scene_assets'][1]['image_version']='changed'
        studio.save(self.path,record)
        response=self.assert_readonly_rejection(self.confirm)
        self.assertIn('场景',response.json()['detail'])

    def test_local_exit_does_not_bypass_target_failure_or_stale_revision(self):
        self.open_local_edit(shot='failed-A')
        self.assert_readonly_rejection(self.confirm)
        record=self.read();record['reedit_shot_id']='missing';studio.save(self.path,record)
        self.assert_readonly_rejection(self.confirm)
        record['reedit_shot_id']='independent-C';studio.save(self.path,record)
        self.assert_readonly_rejection(lambda:self.client.post(self.base+'/images/confirm',
            json={'revision':record['revision']-1}))

    def test_local_reroll_invalidates_only_selected_clip_and_keeps_history(self):
        self.complete_failed();self.open_local_edit(status='completed')
        before=self.read()
        response=self.client.post(self.base+'/images/confirm',json={
            'revision':before['revision'],'regenerate_shot_id':'independent-C'})
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertEqual(result['shots'][:2],before['shots'][:2])
        self.assertNotIn('export',result)
        self.assertEqual(result['shots'][2]['video_status'],'pending')
        self.assertEqual(result['shots'][2]['video_history'][-1]['video'],before['shots'][2]['video'])
        self.assertTrue((self.path/before['shots'][2]['video']).is_file())
        self.assertEqual(result['status'],'video_generation_ready')

    def test_adopted_video_input_asset_is_available_for_image_redraw_without_upload(self):
        self.complete_failed();record=self.read()
        file='assets/adopted-character.jpg';self.write_image(file,'blue')
        record['video_input_assets']=[dict(id='adopted',file=file,name='New character')]
        studio.save(self.path,record)
        response=self.client.get(self.base+'/redraw-references/adopted')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.content,(self.path/file).read_bytes())
        response=self.client.post(self.base+'/images/failed-A/redraw',json={
            'revision':record['revision'],'prompt':'Use the adopted identity.',
            'reference_ids':['adopted'],'use_scene_reference':False})
        self.assertEqual(response.status_code,200,response.text)
        self.run_worker()
        self.assertEqual(self.renderer.call_args.args[0]['reference_image_paths'],[str((self.path/file).resolve())])

    def archive_replaced_scene(self):
        record = self.read()
        old = copy.deepcopy(record['scene_assets'][0])
        record['scene_asset_archive'] = [old]
        new = copy.deepcopy(old)
        new.update(id='scene-new', image='assets/scenes/scene-new.jpg')
        self.write_image(new['image'], 'orange')
        new['image_version'] = self.file_hash(new['image'])
        record['scene_assets'][0] = new
        for shot in record['shots']:
            if shot['scene_reference_id'] == old['id']:
                shot['scene_reference_id'] = new['id']
        studio.save(self.path, record)
        return record

    def prepare_replanned_image(self, *, receipt_status='completed', legacy=False):
        from backend.app import openai_image_transport as transport
        record = self.read()
        old = self.shot('failed-A', 'scene-A')
        for asset in record['scene_assets']:
            asset['name'] = asset['id']
        record['edit_history'] = [{'shots': [copy.deepcopy(old)]}]
        fresh = copy.deepcopy(old)
        for key in list(fresh):
            if key.startswith(('image_', 'video_')) or key in ('image', 'video'):
                fresh.pop(key)
        fresh.update(image_prompt='New approved opening', video_prompt='New approved action')
        record['shots'][0] = fresh
        if legacy:
            self.old_receipt.unlink()
        else:
            request = {'prompt_sha256': 'old-request'}
            self.old_receipt.write_text(json.dumps({
                'version': 1, 'status': receipt_status, 'request': request,
                'request_sha256': transport._sha(transport._json_bytes(request)),
                'attempt_id': 'a' * 32, 'output_sha256': self.file_hash(self.old_output),
            }), encoding='utf-8')
        studio.save(self.path, record)
        return record

    def generate_batch(self):
        return self.client.post(self.base + '/images/generate', json={'revision': self.read()['revision']})

    def test_replan_reserves_new_output_preserves_paid_media_and_uses_it_in_worker(self):
        before = self.prepare_replanned_image()
        original_files = self.files()
        response = self.generate_batch()
        self.assertEqual(response.status_code, 200, response.text)
        reserved = self.read()['shots'][0]['image_output_round']
        self.bulk_prepare.side_effect = None
        self.run_worker()
        after = self.read()
        self.assertEqual(after['shots'][0]['image_status'], 'completed')
        self.assertEqual(after['shots'][0]['image'], f'assets/storyboards/failed-A_{reserved}.jpg')
        self.assertEqual(after['shots'][1:], before['shots'][1:])
        self.assertEqual(after['edit_history'], before['edit_history'])
        for filename, content in original_files.items():
            if filename != 'record.json':
                self.assertEqual((self.path / filename).read_bytes(), content)
        self.assertEqual(self.renderer.call_count, 1)

    def test_replan_failed_new_request_retries_identical_output_version(self):
        self.prepare_replanned_image()
        self.bulk_prepare.side_effect = None
        self.renderer.side_effect = RuntimeError('Outcome unknown')
        self.assertEqual(self.generate_batch().status_code, 200)
        reserved = self.read()['shots'][0]['image_output_round']
        self.run_worker()
        self.assertEqual(self.generate_batch().status_code, 200)
        self.run_worker()
        targets = [call.args[0]['_output_path'] for call in self.renderer.call_args_list]
        self.assertEqual(targets[0], targets[1])
        self.assertEqual(self.read()['shots'][0]['image_output_round'], reserved)

    def test_fresh_plan_cannot_bypass_unknown_dispatching_or_invalid_receipt(self):
        for state in ('unknown', 'dispatching', 'invalid'):
            with self.subTest(state=state):
                self.prepare_replanned_image(receipt_status=state)
                self.assert_readonly_rejection(self.generate_batch)

    def test_replan_rejects_changed_old_image_or_missing_archive(self):
        for failure in ('changed', 'no_archive'):
            with self.subTest(failure=failure):
                record = self.prepare_replanned_image()
                if failure == 'changed':
                    self.write_image(self.old_output, 'red')
                else:
                    record.pop('edit_history')
                    studio.save(self.path, record)
                self.assert_readonly_rejection(self.generate_batch)

    def test_legacy_replan_requires_matching_archived_completed_hash(self):
        self.prepare_replanned_image(legacy=True)
        self.assertEqual(self.generate_batch().status_code, 200)
        self.assertTrue(self.read()['shots'][0]['image_output_round'])

    def test_batch_validation_does_not_partially_reserve_before_later_unknown(self):
        record = self.prepare_replanned_image()
        second = copy.deepcopy(record['shots'][0])
        second['id'] = 'new-B'
        record['shots'].append(second)
        ledger = self.path / 'assets/storyboards/new-B.jpg.openai-image.json'
        previous = json.loads(self.old_receipt.read_text())
        previous['status'] = 'unknown'
        ledger.write_text(json.dumps(previous), encoding='utf-8')
        studio.save(self.path, record)
        self.assert_readonly_rejection(self.generate_batch)

    def test_normal_replan_archives_existing_media_and_refreshes_scene_bindings_only_after_success(self):
        from backend.app import video_auto_cast, video_native_story
        record = self.read()
        record['native_story'] = {'text': 'A enters.', 'minimum_shots': 1, 'duration_seconds': 4}
        record['creation_parameters']['speech_mode'] = 'dialogue'
        studio.save(self.path, record)
        files = self.files()
        new_shots = [dict(id='shot_new', kind='video', image_prompt='New core', reference_ids=[])]
        with patch.object(video_auto_cast, 'ensure_auto_cast', return_value=False), \
             patch.object(video_native_story, 'plan_native_story', return_value=({}, record['scenes'], new_shots)), \
             patch.object(studio, 'project_language_scope', return_value=nullcontext()):
            response = self.client.post(self.base + '/plan')
            self.assertEqual(response.status_code, 200, response.text)
            self.run_worker()
        result = self.read()
        self.assertEqual(result['status'], 'storyboard_review', result.get('error'))
        self.assertEqual(result['shots'], new_shots)
        self.assertEqual(result['edit_history'][-1]['shots'], record['shots'])
        self.assertEqual(result['edit_history'][-1]['scene_assets'], record['scene_assets'])
        self.assertNotIn('scene_references_status', result)
        self.assertEqual(result['scene_assets'], record['scene_assets'])
        for filename, data in files.items():
            if filename != 'record.json': self.assertEqual((self.path / filename).read_bytes(), data)
        self.renderer.assert_not_called()

    def test_failed_replan_retains_existing_shots_and_completed_scene_status(self):
        from backend.app import video_auto_cast, video_native_story
        record = self.read()
        record['native_story'] = {'text': 'A enters.', 'minimum_shots': 1, 'duration_seconds': 4}
        record['creation_parameters']['speech_mode'] = 'dialogue'
        studio.save(self.path, record)
        with patch.object(video_auto_cast, 'ensure_auto_cast', return_value=False), \
             patch.object(video_native_story, 'plan_native_story', side_effect=ValueError('Planning failed')), \
             patch.object(studio, 'project_language_scope', return_value=nullcontext()):
            self.assertEqual(self.client.post(self.base + '/plan').status_code, 200)
            self.run_worker()
        result = self.read()
        self.assertEqual(result['shots'], record['shots'])
        self.assertEqual(result['scene_references_status'], 'completed')
        self.assertFalse(result.get('edit_history'))
        self.renderer.assert_not_called()

    def test_bulk_scene_change_regenerates_only_stale_dependency_and_preserves_originals(self):
        record=self.read()
        record['shots']=record['shots'][1:]
        old=copy.deepcopy(record['shots'][0]);independent=copy.deepcopy(record['shots'][1])
        original_files=self.files()
        def changed_scene(path,live,pool,cancelled):
            live['scene_assets'][0]['name']='Changed room'
            self.write_image(live['scene_assets'][0]['image'],'orange')
            live['scene_assets'][0]['image_version']=self.file_hash(live['scene_assets'][0]['image'])
        self.bulk_prepare.side_effect=changed_scene
        studio._start_storyboard_images(self.path,record,[{}])
        self.run_worker()
        after=self.read();changed=after['shots'][0]
        self.assertEqual(after.get('error'),'')
        self.renderer.assert_called_once()
        self.assertEqual(changed['image_status'],'completed')
        self.assertNotEqual(changed['image'],old['image'])
        self.assertEqual(changed['scene_reference_used_version'],after['scene_assets'][0]['image_version'])
        self.assertEqual(after['shots'][1],independent)
        self.assertEqual(after['edit_history'][-1]['shots'][0],old)
        self.assertEqual((self.path/old['image']).read_bytes(),original_files[old['image']])
        self.assertEqual((self.path/old['video']).read_bytes(),original_files[old['video']])
        self.assertEqual(changed['video_status'],'pending')
        self.assertNotIn('video',changed)
        self.assertEqual(self.old_receipt.read_bytes(),original_files[self.old_receipt.relative_to(self.path).as_posix()])

    def test_bad_shared_scene_stops_bulk_worker_before_any_core_image_submission(self):
        record=self.read();record['native_story']={'text':'A waits.'}
        record.setdefault('creation_parameters', {})['quality_review_policy']='strict'
        before=self.files();self.bulk_prepare.side_effect=self.real_prepare
        with patch('backend.app.video_scene_quality.check_native_scene_images',
                   side_effect=ValueError('Shared scene has incorrect sign text')):
            studio._start_storyboard_images(self.path,record,[{}]);self.run_worker()
        after=self.read()
        self.assertEqual(after['status'],'image_review')
        self.assertIn('incorrect sign text',after['error'])
        self.renderer.assert_not_called()
        for relative,data in before.items():
            if relative!='record.json':self.assertEqual((self.path/relative).read_bytes(),data)

    def test_explicit_scene_quality_repair_uses_current_picture_and_only_invalidates_dependents(self):
        record=self.read();record['native_story']={'text':'A waits.'}
        asset=record['scene_assets'][0]
        asset['quality_review']={'fingerprint':'current','image_version':asset['image_version']}
        studio.save(self.path,record);before=self.files()
        report={'fingerprint':'current','review':{'issues':[{'correction':'Change the sign text to Library.'}],
            'uncertainties':[]}}
        with patch('backend.app.video_scene_quality.scene_report',return_value=report) as cached:
            response=self.client.post(self.base+'/scene-references/scene-A/repair',json={'revision':record['revision']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(cached.call_args.kwargs['cached_only'])
        duplicate=self.client.post(self.base+'/scene-references/scene-A/repair',json={'revision':self.read()['revision']})
        self.assertEqual(duplicate.status_code,409)
        self.run_worker();after=self.read()
        self.renderer.assert_called_once()
        request=self.renderer.call_args.args[0]
        self.assertEqual(request['reference_image_paths'],[str((self.path/asset['image']).resolve())])
        self.assertIn('Change the sign text',request['image_prompt'])
        self.assertEqual(after['scene_assets'][0]['image_task']['status'],'completed')
        archived=after['scene_assets'][0]['image_history'][-1]['image']
        self.assertEqual((self.path/archived).read_bytes(),before[asset['image']])
        self.assertNotIn('video',after['shots'][1])
        self.assertEqual(after['shots'][2],record['shots'][2])

    def test_scene_refresh_refuses_unresolved_receipt_before_mutating_any_shot(self):
        from backend.app import openai_image_transport as transport
        record=self.read();record['shots']=record['shots'][1:]
        record['scene_assets'][0]['image_version']='new-version'
        record['scene_assets'][1]['image_version']='another-new-version'
        shot=record['shots'][1]
        receipt=self.path/(shot['image']+'.openai-image.json')
        request={'prompt':'prior request'}
        receipt.write_text(json.dumps(dict(version=1,status='unknown',request=request,
            request_sha256=transport._sha(transport._json_bytes(request)),attempt_id='a'*32)),encoding='utf-8')
        before=copy.deepcopy(record);files=self.files()
        with self.assertRaisesRegex(transport.OpenAIImageError,'PREVIOUS_OUTCOME_UNKNOWN_NO_RETRY'):
            studio._reserve_stale_scene_images(self.path,record)
        self.assertEqual(record,before)
        self.assertEqual(self.files(),files)
        self.renderer.assert_not_called()

    def test_scene_refresh_reservation_survives_restart_without_rotating_unknown_attempt(self):
        from backend.app import openai_image_transport as transport
        record=self.read();record['shots']=record['shots'][1:]
        record['scene_assets'][0]['image_version']='new-version'
        old=copy.deepcopy(record['shots'][0]);original=self.path/old['image']
        request={'prompt':'original image'}
        completed=dict(version=1,status='completed',request=request,attempt_id='a'*32,
            request_sha256=transport._sha(transport._json_bytes(request)),output_sha256=old['image_version'])
        receipt=transport.ledger_path(original)
        receipt.write_text(json.dumps(completed),encoding='utf-8');paid=receipt.read_bytes()
        studio._reserve_stale_scene_images(self.path,record)
        reserved=studio.storyboard_output(self.path,record,record['shots'][0])
        record['shots'][0].update(image_status='failed',image_error='Outcome unknown')
        transport.ledger_path(reserved).write_text(json.dumps({**completed,'status':'unknown'}),encoding='utf-8')
        studio.save(self.path,record);restarted=self.read();before=copy.deepcopy(restarted)
        studio._reserve_stale_scene_images(self.path,restarted)
        self.assertEqual(restarted,before)
        self.assertEqual(studio.storyboard_output(self.path,restarted,restarted['shots'][0]),reserved)
        self.assertEqual(receipt.read_bytes(),paid)
        self.assertEqual(self.file_hash(old['image']),old['image_version'])
        self.renderer.assert_not_called()

    def test_normal_bulk_retry_can_refresh_completed_but_stale_scene_images(self):
        record=self.read();record['shots']=record['shots'][1:]
        asset=record['scene_assets'][0];asset['name']='Updated room'
        self.write_image(asset['image'],'orange');asset['image_version']=self.file_hash(asset['image'])
        studio.save(self.path,record)
        self.bulk_prepare.side_effect=self.real_prepare
        response=self.client.post(self.base+'/images/generate',json={'revision':record['revision']})
        self.assertEqual(response.status_code,200,response.text)
        self.run_worker()
        after=self.read()
        self.assertEqual(after['shots'][0]['image_status'],'completed',after['logs'])
        self.assertEqual(after['shots'][0]['scene_reference_used_version'],asset['image_version'])
        self.renderer.assert_called_once()
        self.scene_planner.assert_not_called()
        self.assertEqual(after['shots'][1],record['shots'][1])

    def test_bulk_retry_reuses_completed_scene_assets_even_after_prompt_suffixes_or_local_edits(self):
        before = self.files()
        record = self.read()
        record['shots'][1]['image_prompt'] += '\nNew transport notes or local lighting edit'
        self.real_prepare(self.path, record, object(), threading.Event())
        self.scene_planner.assert_not_called()
        self.renderer.assert_not_called()
        self.assertEqual(self.files(), before)
        self.assertEqual(record['scene_assets'], self.record['scene_assets'])

    def test_corrupt_completed_scene_is_not_silently_replanned_on_retry(self):
        record = self.read()
        self.write_image(record['scene_assets'][0]['image'], 'orange')
        with self.assertRaises(ValueError):
            self.real_prepare(self.path, record, object(), threading.Event())
        self.scene_planner.assert_not_called()
        self.renderer.assert_not_called()

    def test_restore_archived_scene_retains_image_versions_prompts_videos_and_receipts(self):
        before = self.archive_replaced_scene()
        files = self.files()
        response = self.client.post(self.base + '/scene-references/restore-bindings', json={'revision': before['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        after = self.read()
        restored = after['shots'][1]
        self.assertEqual(restored['scene_reference_id'], 'scene-A')
        for key, value in before['shots'][1].items():
            if key != 'scene_reference_id':
                self.assertEqual(restored[key], value)
        self.assertEqual(after['shots'][0], before['shots'][0])
        self.assertEqual(after['shots'][2], before['shots'][2])
        self.assertEqual(after['scene_asset_archive'], before['scene_asset_archive'])
        self.assertEqual({k:v for k,v in self.files().items() if k != 'record.json'},
                         {k:v for k,v in files.items() if k != 'record.json'})
        self.assertEqual(studio.scene_reference_review_error(self.path, after), '')
        self.renderer.assert_not_called()
        self.scene_planner.assert_not_called()

    def test_restore_rejects_missing_modified_or_unrelated_archive_without_writes(self):
        for failure in ('missing', 'modified', 'unrelated'):
            with self.subTest(failure=failure):
                studio.save(self.path, self.record)
                record = self.archive_replaced_scene()
                if failure == 'missing':
                    record['scene_asset_archive'] = []
                elif failure == 'modified':
                    self.write_image(record['scene_asset_archive'][0]['image'], 'black')
                else:
                    record['scene_asset_archive'][0]['used_by'] = []
                studio.save(self.path, record)
                self.assert_readonly_rejection(lambda: self.client.post(
                    self.base + '/scene-references/restore-bindings', json={'revision': record['revision']}))
                self.write_image(self.record['scene_assets'][0]['image'], 'green')

    def test_retry_requires_explicit_new_attempt_confirmation_before_any_write_or_config(self):
        for payload in ({'confirmed': False}, {'confirmed': False, 'prompt': 'New opening'}):
            with self.subTest(payload=payload):
                self.assert_readonly_rejection(lambda: self.retry(**payload))
        self.configs.assert_not_called()
        self.scene_planner.assert_not_called()

    def test_retry_only_accepts_failed_core_images(self):
        for status in ('pending', 'unknown', 'running', 'completed'):
            with self.subTest(status=status):
                record = self.read()
                record['shots'][0]['image_status'] = status
                studio.save(self.path, record)
                self.assert_readonly_rejection(self.retry)
        self.assert_readonly_rejection(lambda: self.retry(shot='scene-A'))
        self.configs.assert_not_called()

    def test_retry_rejects_unready_scene_stale_revision_and_unconfirmed_stage(self):
        for changes in ({'scene_references_status': 'failed'}, {'status': 'storyboard_review'}):
            with self.subTest(changes=changes):
                record = copy.deepcopy(self.record)
                record.update(changes)
                studio.save(self.path, record)
                self.assert_readonly_rejection(self.retry)
        studio.save(self.path, self.record)
        self.assert_readonly_rejection(lambda: self.retry(revision=6))

    def test_success_uses_independent_output_and_preserves_old_unknown_receipt_plan_and_other_shots(self):
        # An unknown prior submission may have left image bytes before its outcome was saved.
        self.write_image(self.old_output, 'orange')
        before = self.read()
        protected = self.files()
        response = self.retry()
        self.assertEqual(response.status_code, 200, response.text)
        pending = self.read()['shots'][0]
        self.assertEqual((pending['image_status'], pending['image_task']['action']), ('running', 'retry'))
        self.assertNotEqual(pending['image_attempt_output'], self.old_output)
        self.assertEqual(pending['image_attempt_history'][0]['output'], self.old_output)
        self.assertTrue(pending['image_attempt_history'][0]['new_attempt_confirmed'])
        self.assert_readonly_rejection_while_pending()
        self.run_worker()
        after = self.read()
        shot = after['shots'][0]
        self.assertEqual(shot['image_status'], 'completed')
        self.assertEqual(shot['image'], pending['image_attempt_output'])
        self.assertEqual(shot['image_version'], self.file_hash(shot['image']))
        self.assertEqual(shot['scene_reference_used_version'], before['scene_assets'][0]['image_version'])
        self.assertEqual(after['shots'][1:], before['shots'][1:])
        for key in ('scene_assets', 'scenes', 'context', 'references', 'creation_parameters'):
            self.assertEqual(after[key], before[key], key)
        for relative, content in protected.items():
            if relative != 'record.json':
                self.assertEqual((self.path / relative).read_bytes(), content, relative)
        self.renderer.assert_called_once()
        macro = self.renderer.call_args.args[0]
        self.assertEqual(macro['reference_image_paths'], [str((self.path / before['scene_assets'][0]['image']).resolve())])
        self.scene_planner.assert_not_called()
        self.bulk_prepare.assert_not_called()

    def assert_readonly_rejection_while_pending(self):
        before = self.files()
        current = self.client.get(self.base)
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()['shots'][0]['image_task']['status'], 'running')
        self.assertEqual(self.files(), before)
        response = self.retry()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.files(), before)
        self.assertEqual(len(self.workers), 1)
        self.renderer.assert_not_called()

    def test_failure_preserves_new_unknown_receipt_and_batch_cannot_replay_an_attempt(self):
        old = self.old_receipt.read_bytes()
        outputs = []

        def fail(macro, pool):
            target = Path(macro['_output_path'])
            outputs.append(target)
            target.with_name(target.name + '.openai-image.json').write_bytes(b'{"status":"unknown","attempt":"local-double"}\n')
            raise TimeoutError('Local renderer result unknown')

        self.renderer.side_effect = fail
        response = self.retry()
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()
        first = self.read()
        self.assertEqual(first['shots'][0]['image_status'], 'failed')
        self.assertEqual(self.renderer.call_count, 1)
        ledger = outputs[0].with_name(outputs[0].name + '.openai-image.json')
        receipt_bytes = ledger.read_bytes()
        before = self.files()
        batch = self.client.post(self.base + '/images/generate', json={'revision': first['revision']})
        self.assertEqual(batch.status_code, 409, batch.text)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.workers, [])
        self.assertEqual(self.renderer.call_count, 1)
        denied = self.retry(confirmed=False)
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual(self.files(), before)
        second = self.retry()
        self.assertEqual(second.status_code, 200, second.text)
        next_output = self.read()['shots'][0]['image_attempt_output']
        self.assertNotEqual(next_output, outputs[0].resolve().relative_to(self.path.resolve()).as_posix())
        self.run_worker()
        self.assertEqual(self.renderer.call_count, 2)
        self.assertNotEqual(outputs[0], outputs[1])
        self.assertEqual(ledger.read_bytes(), receipt_bytes)
        self.assertEqual(self.old_receipt.read_bytes(), old)
        self.bulk_prepare.assert_not_called()

    def test_restart_recovers_interrupted_retry_as_failed_without_submitting(self):
        response = self.retry()
        self.assertEqual(response.status_code, 200, response.text)
        running = self.read()
        output = running['shots'][0]['image_attempt_output']
        receipt = self.path / (output + '.openai-image.json')
        receipt.write_bytes(b'{"status":"unknown","request_id":"accepted-before-restart"}')
        receipt_before = receipt.read_bytes()
        self.workers.clear()
        studio.IMAGE_EDITS.clear()
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200, response.text)
        after = response.json()
        self.assertEqual(after['status'], 'image_review')
        self.assertEqual(after['shots'][0]['image_status'], 'failed')
        self.assertEqual(after['shots'][0]['image_task']['action'], 'retry')
        self.assertEqual(after['shots'][0]['image_task']['status'], 'failed')
        self.assertEqual(after['shots'][0]['image_attempt_output'], output)
        self.assertEqual(after['shots'][0]['image_attempt_history'], running['shots'][0]['image_attempt_history'])
        self.assertEqual(after['shots'][1:], running['shots'][1:])
        self.assertEqual(receipt.read_bytes(), receipt_before)
        recovered_bytes = (self.path / 'record.json').read_bytes()
        self.assertEqual(self.client.get(self.base).status_code, 200)
        self.assertEqual((self.path / 'record.json').read_bytes(), recovered_bytes)
        self.renderer.assert_not_called()
        self.assertEqual(self.workers, [])
        denied = self.client.post(self.base + '/images/generate', json={'revision': after['revision']})
        self.assertEqual(denied.status_code, 409, denied.text)

    def test_retry_startup_failure_releases_edit_lock_and_get_recovers_without_submission(self):
        before = self.files()
        thread = SimpleNamespace(start=Mock(side_effect=RuntimeError('Local worker could not start')))
        with patch.object(studio.threading, 'Thread', return_value=thread):
            with self.assertRaisesRegex(RuntimeError, 'Local worker could not start'):
                self.retry()
        self.assertFalse(studio.project_has_image_edits(self.identity))
        recovered = self.client.get(self.base)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()['shots'][0]['image_status'], 'failed')
        self.assertEqual(recovered.json()['shots'][0]['image_task']['status'], 'failed')
        for relative, content in before.items():
            if relative != 'record.json':
                self.assertEqual((self.path / relative).read_bytes(), content, relative)
        self.renderer.assert_not_called()
        response = self.retry()
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()
        self.renderer.assert_called_once()

    def test_retry_initial_save_failure_releases_edit_lock_and_keeps_original_record(self):
        before = self.files()
        with patch.object(studio, 'save', side_effect=OSError('Local record save failed')):
            with self.assertRaisesRegex(OSError, 'Local record save failed'):
                self.retry()
        self.assertFalse(studio.project_has_image_edits(self.identity))
        self.assertEqual(self.files(), before)
        self.assertEqual(self.workers, [])
        self.renderer.assert_not_called()
        response = self.retry()
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()

    def test_redraw_cleanup_failure_still_releases_edit_lock_after_committed_success(self):
        self.complete_failed()
        response = self.client.post(self.base + '/images/scene-A/redraw', json={
            'revision': self.read()['revision'], 'prompt': 'Changed room', 'use_scene_reference': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.workers), 1)
        original_unlink = Path.unlink

        def blocked_unlink(path, *args, **kwargs):
            if path.parent.name == 'storyboard_redraw':
                raise PermissionError('Local cleanup denied')
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', blocked_unlink):
            try:
                self.workers.pop()()
            except PermissionError:
                pass
        self.assertFalse(studio.project_has_image_edits(self.identity))
        self.assertEqual(self.read()['scene_assets'][0]['image_task']['status'], 'completed')
        self.renderer.assert_called_once()

    def assert_scene_change(self, before):
        after = self.read()
        self.assertNotEqual(after['scene_assets'][0]['image_version'], before['scene_assets'][0]['image_version'])
        self.assertEqual(after['scene_assets'][1], before['scene_assets'][1])
        self.assertEqual(after['shots'][2], before['shots'][2])
        self.assertNotIn('export', after)
        for old, current in zip(before['shots'][:2], after['shots'][:2]):
            self.assertEqual(current['image'], old['image'])
            self.assertEqual(current['image_status'], 'completed')
            self.assertEqual(current['scene_reference_used_version'], old['scene_reference_used_version'])
            self.assertEqual(current['video_status'], 'pending')
            self.assertNotIn('video', current)
            self.assertEqual(current['video_history'][-1]['video'], old['video'])
            self.assertTrue((self.path / old['video']).is_file())
        persisted = self.files()
        response = self.confirm()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.files(), persisted)
        self.scene_planner.assert_not_called()
        return after

    def test_scene_redraw_invalidates_only_dependents_and_blocks_confirmation(self):
        self.complete_failed()
        before = self.read()
        response = self.client.post(self.base + '/images/scene-A/redraw', json={
            'revision': before['revision'], 'prompt': 'Changed room geometry', 'use_scene_reference': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()
        self.assert_scene_change(before)
        self.renderer.assert_called_once()

    def test_failed_scene_redraw_retains_scene_bytes_and_all_existing_dependents(self):
        self.complete_failed()
        before = self.read()
        protected = self.files()
        self.renderer.side_effect = TimeoutError('Local renderer result unknown')
        response = self.client.post(self.base + '/images/scene-A/redraw', json={
            'revision': before['revision'], 'prompt': 'New scene version', 'use_scene_reference': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()
        after = self.read()
        self.assertEqual(after['shots'], before['shots'])
        self.assertEqual(after['export'], before['export'])
        self.assertEqual(after['scene_assets'][0]['image_version'], before['scene_assets'][0]['image_version'])
        self.assertEqual(after['scene_assets'][0]['image_task']['status'], 'failed')
        self.renderer.assert_called_once()
        for relative, content in protected.items():
            if relative != 'record.json':
                self.assertEqual((self.path / relative).read_bytes(), content, relative)

    def test_prop_only_edit_retains_scene_dependency_and_later_scene_change_blocks_confirm(self):
        self.complete_failed()
        before = self.read()
        original = before['shots'][1]
        response = self.client.post(self.base + '/images/dependent-B/redraw', json={
            'revision': before['revision'], 'prompt': 'Shorten only the stool legs',
            'use_current_image': True, 'use_scene_reference': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()
        after = self.read()
        edited = after['shots'][1]
        self.assertNotEqual(edited['image_version'], original['image_version'])
        self.assertEqual(edited['scene_reference_used_version'], original['scene_reference_used_version'])
        self.assertEqual(after['shots'][0], before['shots'][0])
        self.assertEqual(after['shots'][2], before['shots'][2])
        self.assertEqual(edited['image_history'][-1]['scene_reference_used_version'],
                         original['scene_reference_used_version'])
        macro = self.renderer.call_args.args[0]
        self.assertEqual(len(macro['reference_image_paths']), 1)
        # A later scene update must still discover this derived image.
        response = self.client.post(self.base + '/images/scene-A/redraw', json={
            'revision': after['revision'], 'prompt': 'Change doorway geometry', 'use_scene_reference': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()
        response = self.confirm()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('1、2', response.json()['detail'])

    def test_prop_only_edit_of_stale_core_cannot_erase_scene_review_blocker(self):
        self.complete_failed()
        before = self.read()
        old_version = before['scene_assets'][0]['image_version']
        response = self.client.post(self.base + '/images/scene-A/redraw', json={
            'revision': before['revision'], 'prompt': 'New doorway', 'use_scene_reference': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()
        response = self.client.post(self.base + '/images/dependent-B/redraw', json={
            'revision': self.read()['revision'], 'prompt': 'Change only the stool color',
            'use_current_image': True, 'use_scene_reference': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()
        self.assertEqual(self.read()['shots'][1]['scene_reference_used_version'], old_version)
        response = self.confirm()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('1、2', response.json()['detail'])

    def test_new_scene_input_takes_precedence_over_inherited_old_core_dependency(self):
        self.complete_failed()
        response = self.client.post(self.base + '/images/scene-A/redraw', json={
            'revision': self.read()['revision'], 'prompt': 'New doorway', 'use_scene_reference': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()
        current_scene = self.read()['scene_assets'][0]['image_version']
        response = self.client.post(self.base + '/images/dependent-B/redraw', json={
            'revision': self.read()['revision'], 'prompt': 'Use the updated doorway',
            'use_current_image': True, 'use_scene_reference': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()
        self.assertEqual(len(self.renderer.call_args.args[0]['reference_image_paths']), 2)
        self.assertEqual(self.read()['shots'][1]['scene_reference_used_version'], current_scene)
        response = self.confirm()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('第 1 镜', response.json()['detail'])

    def test_prompt_only_redraw_does_not_invent_inherited_scene_input(self):
        response = self.client.post(self.base + '/images/dependent-B/redraw', json={
            'revision': self.read()['revision'], 'prompt': 'An independently designed room',
            'use_current_image': False, 'use_scene_reference': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()
        self.assertEqual(self.renderer.call_args.args[0]['reference_image_paths'], [])
        self.assertEqual(self.read()['shots'][1]['scene_reference_used_version'], '')

    def test_failed_local_edit_preserves_scene_lineage_and_original_image(self):
        before = self.read()
        original = before['shots'][1]
        self.renderer.side_effect = TimeoutError('Local renderer result unknown')
        response = self.client.post(self.base + '/images/dependent-B/redraw', json={
            'revision': before['revision'], 'prompt': 'Fix the prop',
            'use_current_image': True, 'use_scene_reference': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.run_worker()
        edited = self.read()['shots'][1]
        for key in ('image', 'image_version', 'scene_reference_used_version', 'video', 'video_task_id'):
            self.assertEqual(edited[key], original[key])
        self.assertEqual(edited['image_task']['status'], 'failed')

    def test_scene_upload_invalidates_only_dependents_and_blocks_confirmation(self):
        self.complete_failed()
        before = self.read()
        content = io.BytesIO()
        Image.new('RGB', (32, 24), 'orange').save(content, format='PNG')
        response = self.client.post(self.base + '/images/scene-A/upload', data={'revision': before['revision']},
                                    files={'file': ('scene.png', content.getvalue(), 'image/png')})
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_scene_change(before)
        self.renderer.assert_not_called()

    def test_scene_undo_invalidates_only_dependents_and_blocks_confirmation(self):
        self.complete_failed()
        record = self.read()
        archived = 'assets/storyboard_history/scene-A/old.jpg'
        self.write_image(archived, 'orange')
        record['scene_assets'][0]['image_history'] = [{'image': archived, 'prompt': 'Previous room'}]
        studio.save(self.path, record)
        before = self.read()
        response = self.client.post(self.base + '/images/scene-A/undo', json={'revision': before['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_scene_change(before)
        self.renderer.assert_not_called()

    def test_stale_static_scene_dependency_also_blocks_confirmation(self):
        self.complete_failed()
        record = self.read()
        for shot in record['shots']:
            shot['kind'] = 'static'
        record['scene_assets'][0]['image_version'] = 'different-scene-version'
        studio.save(self.path, record)
        self.assert_readonly_rejection(self.confirm)

    def test_same_scene_and_manually_uploaded_core_without_scene_binding_can_confirm(self):
        self.complete_failed()
        record = self.read()
        record['shots'][0]['scene_reference_used_version'] = ''
        studio.save(self.path, record)
        response = self.confirm()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'video_generation_ready')
        self.renderer.assert_not_called()

    def test_replaced_scene_bytes_without_record_commit_cannot_be_confirmed_after_restart(self):
        self.complete_failed()
        response = self.client.post(self.base + '/images/scene-A/redraw', json={
            'revision': self.read()['revision'], 'prompt': 'Changed room', 'use_scene_reference': False})
        self.assertEqual(response.status_code, 200, response.text)
        # Simulate the real worker's copy-to-live-image / record-save crash window.
        scene = self.read()['scene_assets'][0]
        self.write_image(scene['image'], 'purple')
        self.workers.clear()
        studio.IMAGE_EDITS.clear()
        recovered = self.client.get(self.base)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        before = self.files()
        response = self.confirm()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.files(), before)
        self.renderer.assert_not_called()
        self.assertEqual(self.read()['scene_assets'][0]['image_version'], scene['image_version'])

    def test_missing_or_uncommitted_scene_bytes_block_confirm_without_silent_record_repair(self):
        self.complete_failed()
        scene = self.read()['scene_assets'][0]
        original = (self.path / scene['image']).read_bytes()
        for state in ('missing', 'changed'):
            with self.subTest(state=state):
                target = self.path / scene['image']
                if state == 'missing':
                    target.unlink()
                else:
                    self.write_image(scene['image'], 'red')
                self.assert_readonly_rejection(self.confirm)
                target.write_bytes(original)

    def test_audio_sync_cannot_promote_stale_or_incomplete_scenes_past_image_review(self):
        self.complete_failed()
        initial = self.read()
        initial['source_project'] = {'id': 'audio-fixture'}
        initial['audio'] = 'assets/audio.wav'
        initial['scenes'][0]['slide_id'] = 'scene_001'
        for shot in initial['shots']:
            shot['slide_ids'] = ['scene_001']
        source = self.root / 'audio-source'
        (source / 'input').mkdir(parents=True)
        (source / 'other').mkdir()
        (source / 'other' / '最终字幕.srt').write_text(
            '1\n00:00:00,000 --> 00:00:04,000\nA enters.\n', encoding='utf-8')

        def write_audio(target, sample):
            with wave.open(str(target), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(800)
                stream.writeframes(sample * 3200)

        write_audio(source / 'input' / '配音.wav', b'\x01\x00')
        job = SimpleNamespace(id='audio-fixture', user_id=1, request={'dynamic_video': True})
        store = SimpleNamespace(get=Mock(return_value=job))
        editor = SimpleNamespace(output_dir=Mock(return_value=source))
        tts = ModuleType('backend.app.tts_editor')
        tts.tts_editor = SimpleNamespace(status=Mock(return_value={'status': 'idle'}), commit_step_review=Mock())
        with patch.object(sources, 'dependencies', return_value=(store, editor, None)), \
                patch.dict(sys.modules, {'backend.app.tts_editor': tts}), \
                patch.object(generation, '_start_local_worker') as video_worker, \
                patch.object(export, '_worker') as export_worker:
            for issue in ('stale-version', 'uncommitted-bytes', 'scene-preparation-failed'):
                with self.subTest(issue=issue):
                    record = copy.deepcopy(initial)
                    scene = record['scene_assets'][0]
                    self.write_image(scene['image'], 'green')
                    write_audio(self.path / record['audio'], b'\x00\x00')
                    if issue == 'stale-version':
                        self.write_image(scene['image'], 'purple')
                        scene['image_version'] = self.file_hash(scene['image'])
                    elif issue == 'uncommitted-bytes':
                        self.write_image(scene['image'], 'purple')
                    else:
                        record['scene_references_status'] = 'failed'
                    studio.save(self.path, record)
                    response = self.client.post(self.base + '/sync-audio', json={'revision': record['revision']})
                    self.assertEqual(response.status_code, 200, response.text)
                    current = response.json()
                    self.assertEqual(current['status'], 'image_review')
                    self.assertEqual((self.path / current['audio']).read_bytes(), (source / 'input' / '配音.wav').read_bytes())
                    self.assertEqual(current['scenes'], record['scenes'])
                    self.assertEqual(current['scene_assets'], record['scene_assets'])
                    persisted = self.files()
                    for endpoint, payload in (
                        ('/images/confirm', {'revision': current['revision']}),
                        ('/videos/generate', {'revision': current['revision'], 'shot_ids': ['failed-A']}),
                        ('/export', {'revision': current['revision'], 'use_video_audio': True}),
                    ):
                        denied = self.client.post(self.base + endpoint, json=payload)
                        self.assertEqual(denied.status_code, 409, denied.text)
                        self.assertEqual(self.files(), persisted)
            video_worker.assert_not_called()
            export_worker.assert_not_called()
        self.renderer.assert_not_called()
        self.scene_planner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
