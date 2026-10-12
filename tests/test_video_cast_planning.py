"""Planning identities stay explicit, current and separate from shot confirmation."""
import copy
import hashlib
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request

from backend.app import video_cast_planning as cast, video_studio as studio
from backend.app.video_project_cast import build_project_cast


class CastPlanningTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / '1' / ('a' * 32)
        self.path.mkdir(parents=True)
        for identity, color in [('a', 'red'), ('b', 'blue'), ('room', 'white')]:
            Image.new('RGB', (18, 16), color).save(self.path / (identity + '.png'))
        self.record = {
            'id': 'a' * 32, 'revision': 1, 'status': 'draft', 'logs': [], 'shots': [],
            'scenes': [{'slide_id': 1, 'start': 0, 'end': 4, 'text': 'A waits; B enters.'}],
            'settings': {'style': 'watercolor', 'characters': '', 'world': ''},
            'creation_parameters': {},
            'references': [{'id': 'room', 'label': '图1', 'kind': 'scene', 'file': 'room.png'},
                           {'id': 'a', 'label': '图2', 'kind': 'character', 'file': 'a.png'}],
            'video_input_assets': [{'id': 'b', 'name': 'B', 'file': 'b.png'}],
        }
        self.save_cast([{'name': 'A', 'reference_id': 'a', 'constraints': 'No visible ears.'},
                        {'name': 'B', 'reference_id': 'b', 'constraints': 'Green dress.'}])

    def catalog(self):
        return [*self.record['references'], *self.record.get('redraw_references', []),
                *self.record['video_input_assets']]

    def save_cast(self, rows):
        self.record['project_cast'] = build_project_cast(1, self.path, self.record, self.catalog(), rows)
        return self.record['project_cast']['characters']

    def snapshot(self):
        return cast.saved_planning_cast(self.record)

    def plan(self, indices=(0, 1)):
        snapshot = self.snapshot()
        return cast.bind_planned_characters(snapshot, [
            {'project_character_id': snapshot['characters'][index]['project_character_id'],
             'presence': 'opening' if index == 0 else 'later', 'basis': 'Explicit narrative timing.'}
            for index in indices])

    @staticmethod
    def editable(row, **changes):
        return {**{key: row[key] for key in ('id', 'name', 'reference_id', 'constraints')}, **changes}

    @staticmethod
    def rehash(snapshot):
        body = {key: value for key, value in snapshot.items() if key != 'cast_sha256'}
        snapshot['cast_sha256'] = hashlib.sha256(json.dumps(body, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        return snapshot

    def test_creation_bindings_require_explicit_uploaded_ids_and_distinct_names(self):
        rows = [{'asset_id': 'a', 'name': '  A  ', 'constraints': '  Keep costume.  '}]
        self.assertEqual(cast.creation_cast_bindings(rows, ['a']), [
            {'asset_id': 'a', 'name': 'A', 'constraints': 'Keep costume.'}])
        self.assertEqual(cast.creation_cast_bindings([], []), [])
        cases = [None, {}, rows * 2, [{'asset_id': 'foreign', 'name': 'A'}],
                 [{'asset_id': 'a', 'name': 'A'}, {'asset_id': 'b', 'name': ' A '}],
                 [{'asset_id': 'a', 'name': 'A', 'user_confirmed': True}],
                 [{'asset_id': 'a', 'name': 'A\x00'}],
                 [{'asset_id': str(i), 'name': str(i)} for i in range(7)]]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                cast.creation_cast_bindings(values, ['a', 'b', *map(str, range(7))])

    def test_malformed_creation_reference_catalog_fails_as_validation_error(self):
        for refs in (None, 'a', {'a': 'fake'}, ['a'] * 7, [*map(str, range(6)), 'a']):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                cast.creation_cast_bindings([{'asset_id': 'a', 'name': 'A'}], refs)

    def test_verified_snapshot_contains_exact_image_identities_without_mutation(self):
        before = copy.deepcopy(self.record)
        snapshot = cast.verified_planning_cast(1, self.path, self.record, self.catalog())
        self.assertEqual(snapshot, self.snapshot())
        self.assertEqual(self.record, before)
        self.assertEqual(snapshot['project_id'], self.record['id'])
        self.assertEqual(snapshot['characters'][0]['reference_sha256'],
                         hashlib.sha256((self.path / 'a.png').read_bytes()).hexdigest())
        self.assertNotIn('file', snapshot['characters'][0])
        self.assertNotIn('owner_id', snapshot)
        snapshot['characters'][0]['name'] = 'Changed outside record'
        self.assertEqual(self.record, before)

    def test_changed_deleted_or_invalid_picture_blocks_execution_snapshot(self):
        saved = self.snapshot()
        for kind in ('changed', 'deleted', 'invalid'):
            with self.subTest(kind=kind):
                if kind == 'changed':
                    Image.new('RGB', (18, 16), 'yellow').save(self.path / 'a.png')
                elif kind == 'deleted':
                    (self.path / 'a.png').unlink()
                else:
                    (self.path / 'a.png').write_bytes(b'not an image')
                self.assertEqual(self.snapshot(), saved)
                with self.assertRaises(ValueError):
                    cast.verified_planning_cast(1, self.path, self.record, self.catalog())

    def test_owner_project_and_ambiguous_reference_are_checked_before_execution(self):
        with self.assertRaises(ValueError):
            cast.verified_planning_cast(2, self.path, self.record, self.catalog())
        damaged = copy.deepcopy(self.record)
        damaged['id'] = 'b' * 32
        with self.assertRaises(ValueError):
            cast.saved_planning_cast(damaged)
        with self.assertRaises(ValueError):
            cast.verified_planning_cast(1, self.path, self.record,
                                       [*self.catalog(), copy.deepcopy(self.catalog()[1])])

    def test_snapshot_rejects_tampering_and_untrusted_provenance(self):
        baseline = self.snapshot()
        cases = []
        for changes in ({'version': True}, {'source': 'user_confirmed'}, {'project_id': ''},
                        {'characters': []}, {'characters': [baseline['characters'][0]] * 2},
                        {'extra': 'client-controlled'}):
            cases.append(self.rehash({**copy.deepcopy(baseline), **changes}))
        for field, value in [('identity_sha256', ''), ('reference_sha256', 'g' * 64),
                             ('project_character_id', 'vc_' + 'a' * 32), ('name', ''),
                             ('confirmed', True)]:
            snapshot = copy.deepcopy(baseline)
            snapshot['characters'][0][field] = value
            cases.append(self.rehash(snapshot))
        changed = copy.deepcopy(baseline)
        changed['characters'][0]['constraints'] = 'Changed without digest'
        cases.append(changed)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                cast.validate_planning_cast(value)

    def test_model_plan_preserves_timing_and_is_not_visibility_confirmation(self):
        planned = self.plan()
        self.assertEqual(planned['source'], 'director_plan')
        self.assertEqual([row['presence'] for row in planned['characters']], ['opening', 'later'])
        self.assertEqual(planned['characters'][1]['identity_sha256'],
                         self.snapshot()['characters'][1]['identity_sha256'])
        self.assertNotIn('video_character_review', planned)
        self.assertNotIn('user_confirmed', json.dumps(planned))
        self.assertEqual(self.record['shots'], [])
        self.assertEqual(cast.bind_planned_characters(self.snapshot(), [])['characters'], [])

    def test_model_cannot_invent_ids_hashes_presence_or_confirmation(self):
        raw = {'project_character_id': self.snapshot()['characters'][0]['project_character_id'],
               'presence': 'opening', 'basis': 'Visible in opening.'}
        cases = [None, {}, [raw, raw], [{**raw, 'project_character_id': 'pc_' + 'f' * 32}],
                 [{**raw, 'presence': 'both'}], [{**raw, 'basis': ' '}],
                 [{**raw, 'identity_sha256': 'f' * 64}], [{**raw, 'user_confirmed': True}]]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                cast.bind_planned_characters(self.snapshot(), rows)

    def test_role_changes_invalidate_only_plans_which_use_that_role(self):
        first, second, empty = self.plan((0,)), self.plan((1,)), self.plan(())
        rows = self.record['project_cast']['characters']
        self.save_cast([self.editable(rows[0]), self.editable(rows[1], constraints='Green dress stays green.')])
        self.assertEqual(cast.validate_cast_plan(self.snapshot(), first), first)
        self.assertEqual(cast.validate_cast_plan(self.snapshot(), empty), empty)
        with self.assertRaises(ValueError):
            cast.validate_cast_plan(self.snapshot(), second)
        foreign = {**first, 'project_id': 'b' * 32}
        with self.assertRaises(ValueError):
            cast.validate_cast_plan(self.snapshot(), foreign)

    def test_opening_references_include_opening_role_and_exclude_later_role(self):
        self.assertEqual(cast.plan_reference_ids(self.snapshot(), self.plan(), ['room']), ['room', 'a'])
        self.assertEqual(cast.plan_reference_ids(self.snapshot(), self.plan(), ['a', 'room', 'a']), ['a', 'room'])
        for selected in (['b'], ['a', 'b'], None, ['a', 5]):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                cast.plan_reference_ids(self.snapshot(), self.plan(), selected)
        with self.assertRaises(ValueError):
            cast.plan_reference_ids(self.snapshot(), self.plan((0,)), ['b'])

    def test_planning_catalog_adds_only_explicitly_bound_supplemental_assets(self):
        self.record['video_input_assets'].append({'id': 'unused', 'file': 'unused.png'})
        before = copy.deepcopy(self.record)
        result = cast.planning_references(self.record)
        self.assertEqual([row['id'] for row in result], ['room', 'a', 'b'])
        self.assertEqual(result[-1]['label'], '图3')
        self.assertEqual(result[-1]['kind'], 'character')
        self.assertEqual(self.record, before)
        result[0]['label'] = 'outside mutation'
        self.assertEqual(self.record, before)

    def test_missing_or_ambiguous_supplemental_id_cannot_be_selected_by_name(self):
        original = copy.deepcopy(self.record['video_input_assets'])
        for assets in ([], original * 2, [{'id': 'unbound', 'name': 'B', 'file': 'b.png'}]):
            with self.subTest(assets=assets), self.assertRaises(ValueError):
                self.record['video_input_assets'] = assets
                cast.planning_references(self.record)

    def test_malformed_reference_directory_fails_as_validation_error(self):
        baseline = copy.deepcopy(self.record)
        for field, value in [('references', None), ('references', [None]),
                             ('video_input_assets', [None]), ('video_input_assets', 'damaged')]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.record = copy.deepcopy(baseline)
                self.record[field] = value
                cast.planning_references(self.record)

    def test_injected_creation_parameter_snapshot_has_no_authority(self):
        self.record['creation_parameters']['project_cast'] = {'source': 'client', 'characters': ['fake']}
        self.assertEqual(studio.planning_parameters(self.record)['project_cast'], self.snapshot())
        self.save_cast([])
        self.assertIsNone(self.snapshot())
        self.assertNotIn('project_cast', studio.planning_parameters(self.record))

    def test_identity_change_invalidates_planning_resume_and_old_context(self):
        fingerprint = studio.planning_fingerprint(self.record['scenes'], 'watercolor', '', '',
            cast.planning_references(self.record), studio.planning_parameters(self.record))
        self.record['planning_state'] = {'fingerprint': fingerprint, 'context': {'summary': 'old'}}
        self.record['context'] = {'production_fingerprint': fingerprint, 'summary': 'old',
                                 'project_cast': self.snapshot()}
        self.assertIsNotNone(studio.resumable_planning_state(self.record))
        rows = self.record['project_cast']['characters']
        self.save_cast([self.editable(rows[0], constraints='New explicit costume.'), self.editable(rows[1])])
        self.assertIsNone(studio.resumable_planning_state(self.record))
        context = studio.production_prompt_context(self.record)
        self.assertNotIn('summary', context)
        self.assertEqual(context['project_cast'], self.snapshot())

    def test_clearing_last_role_discards_old_reading_context_without_story_snapshot(self):
        fingerprint = studio.planning_fingerprint(self.record['scenes'], 'watercolor', '', '',
            cast.planning_references(self.record), studio.planning_parameters(self.record))
        self.record['context'] = {'production_fingerprint': fingerprint, 'summary': 'A has old costume.',
                                 'project_cast': self.snapshot()}
        self.record['planning_state'] = {'fingerprint': fingerprint}
        old_shot = {'cast_plan': self.plan(), 'reference_ids': ['a']}
        self.save_cast([])
        self.assertIsNone(studio.resumable_planning_state(self.record))
        with self.assertRaises(ValueError):
            studio.validate_cast_shot(self.record, old_shot)
        context = studio.production_prompt_context(self.record)
        self.assertNotIn('project_cast', context)
        self.assertNotIn('summary', context)

    def test_planning_identity_does_not_silently_supply_missing_shot_plan(self):
        with self.assertRaises(ValueError):
            studio.validate_cast_shot(self.record, {'reference_ids': ['a']})
        with self.assertRaises(ValueError):
            studio.validate_cast_shot(self.record, {'cast_plan': self.plan(), 'reference_ids': []})
        studio.validate_cast_shot(self.record, {'cast_plan': self.plan(), 'reference_ids': ['a']})

    def test_image_request_uses_opening_asset_and_retains_later_entrance_instruction(self):
        shot = {'cast_plan': self.plan(), 'reference_ids': ['room', 'a']}
        prompt, paths, scene = studio._image_inputs(self.path, self.record, shot, 'Opening scene.', use_scene=False)
        self.assertEqual(paths, [str((self.path / 'room.png').resolve()), str((self.path / 'a.png').resolve())])
        self.assertIsNone(scene)
        self.assertIn('No visible ears.', prompt)
        self.assertIn('不提前画入', prompt)
        self.assertIn('图2', prompt)

    def test_opening_positions_follow_ids_and_refresh_without_adding_later_roles(self):
        first, second = self.snapshot()['characters']
        shot = {'cast_plan': self.plan(), 'reference_ids': ['a'], 'native_dialogue': {
            'speakers': [
                {'character_id': second['project_character_id'], 'name': 'A',
                 'kind': 'character', 'on_screen': True, 'presence': 'later',
                 'position': 'Later doorway position'},
                {'character_id': first['project_character_id'], 'name': 'B',
                 'kind': 'character', 'on_screen': True, 'presence': 'opening',
                 'position': 'Left of table beside window'},
                {'character_id': 'narrator', 'kind': 'narrator', 'on_screen': False,
                 'position': 'Narrator must not appear'}]}}
        prompt, paths, _ = studio._image_inputs(self.path, self.record, shot, 'Opening.', use_scene=False)
        self.assertIn('Left of table beside window', prompt)
        self.assertNotIn('Later doorway position', prompt)
        self.assertNotIn('Narrator must not appear', prompt)
        self.assertEqual(len(paths), 1)
        speaker = shot['native_dialogue']['speakers'][1]
        speaker['position'] = 'Behind table beside window'
        refreshed, _, _ = studio._image_inputs(self.path, self.record, shot, prompt, use_scene=False)
        self.assertNotIn('Left of table beside window', refreshed)
        self.assertEqual(refreshed.count(speaker['position']), 1)
        self.assertEqual(studio._image_inputs(self.path, self.record, shot, refreshed,
                                            use_scene=False)[0], refreshed)
        self.assertEqual(studio._image_inputs(self.path, self.record, shot, 'Manual.',
                                            references=[], use_scene=False)[0], 'Manual.')
        speaker['on_screen'] = False
        hidden, _, _ = studio._image_inputs(self.path, self.record, shot, 'Opening.', use_scene=False)
        self.assertNotIn(speaker['position'], hidden)

    def test_partial_identity_visibility_survives_reference_binding_and_resubmit(self):
        shot = {'cast_plan': self.plan(), 'reference_ids': ['a']}
        opening = '树后只露出角色的手，头和身体仍被遮住。'
        prompt, paths, _ = studio._image_inputs(self.path, self.record, shot, opening, use_scene=False)
        repeated, repeated_paths, _ = studio._image_inputs(self.path, self.record, shot, prompt, use_scene=False)
        self.assertTrue(prompt.startswith(opening))
        self.assertIn('可见部位以本镜开场为准', prompt)
        self.assertEqual(repeated, prompt)
        self.assertEqual(repeated_paths, paths)
        self.assertEqual(len(paths), 1)
        self.assertIn('不提前画入', prompt)

    def test_shot_costume_does_not_inherit_other_scene_hair_or_clothing(self):
        self.save_cast([{'name':'A','reference_id':'a','constraints':'黑色长发、现代衬衣、竹叶发饰。'},
                        {'name':'B','reference_id':'b','constraints':'Green dress.'}])
        identity=self.snapshot()['characters'][0]['project_character_id']
        shot={'cast_plan':self.plan(),'reference_ids':['a'],
              'native_dialogue':{'speakers':[{'character_id':identity,'kind':'character',
                'appearance':'Same face, short hair and a white tactical shirt.',
                'position':'On the elevated ruins.'}]}}
        before=copy.deepcopy(shot)
        prompt,paths,_=studio._image_inputs(self.path,self.record,shot,'未来短发角色立于废墟。',use_scene=False)
        self.assertIn('身份与面容',prompt)
        self.assertIn('short hair and a white tactical shirt',prompt)
        self.assertNotIn('黑色长发',prompt)
        self.assertNotIn('竹叶发饰',prompt)
        self.assertIn('本镜开场要求为准',prompt)
        again,again_paths,_=studio._image_inputs(self.path,self.record,shot,prompt,use_scene=False)
        self.assertEqual((again,again_paths),(prompt,paths))
        self.assertEqual(shot,before)

    def test_without_approved_shot_appearance_retains_original_constraints(self):
        shot={'cast_plan':self.plan(),'reference_ids':['a']}
        prompt,_,_=studio._image_inputs(self.path,self.record,shot,'Opening.',use_scene=False)
        self.assertIn('No visible ears.',prompt)

    def test_shared_scene_does_not_shift_native_video_identity_slots_or_add_later_actor_to_core(self):
        from backend.app import video_inputs
        from tests.test_video_inputs import profile
        self.record['native_story']={'text':'A waits; B enters.'}
        self.record['creation_parameters'].update(dynamic_text_mode='visual_first',
            scene_references_enabled=True,video_generation_backend='comfyui',comfyui_profile_id='composition_reference')
        self.record['scene_assets']=[dict(id='shared_room',image='room.png',image_status='completed')]
        shot={'id':'shot_A','cast_plan':self.plan(),'reference_ids':['a'],
              'scene_reference_id':'shared_room','image':'room.png','image_status':'completed'}
        prompt,paths,_=studio._image_inputs(self.path,self.record,shot,'图2中的A等待，B稍后才进入。')
        self.assertEqual(paths,[str((self.path/'a.png').resolve()),str((self.path/'room.png').resolve())])
        self.assertIn('图1只绑定开场角色',prompt)
        self.assertIn('图2仅用于保持同一空间',prompt)
        self.assertIn('不提前画入',prompt)
        with patch.object(video_inputs,'video_profile',return_value=profile('composition_reference',2)):
            _,inputs=video_inputs.resolve_video_inputs(self.path,self.record,shot)
        self.assertEqual([row['id'] for row in inputs],['shot_A','a','b'])
        self.assertEqual([row['label'] for row in inputs],['<Picture 1>','<Picture 2>','<Picture 3>'])
        self.assertEqual([row['role'] for row in inputs],['composition','identity_reference','identity_reference'])

    def boundary(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, value in [('ROOT', self.root), ('ACTIVE', set()), ('IMAGE_EDITS', set()),
                            ('CANCEL_EVENTS', {}), ('require_user', lambda request: {'id': 1})]:
            stack.enter_context(patch.object(studio, name, value))
        return stack

    def fixed_shot(self):
        return {'id': 'shot-A', 'kind': 'video', 'slide_ids': [1], 'start': 0, 'end': 4,
                'duration': 4, 'generation_duration': 4, 'reference_ids': ['a'],
                'image_prompt': 'Old opening design.', 'video_prompt': 'Old motion design.',
                'cast_plan': self.plan(), 'image_status': 'pending',
                'video_history': [{'video': 'old.mp4', 'reason': 'protected sentinel'}]}

    @staticmethod
    def request():
        return Request({'type': 'http', 'headers': []})

    def test_replaced_picture_blocks_fixed_and_repair_plans_before_worker(self):
        self.boundary()
        self.record['shots'] = [self.fixed_shot()]
        self.record['manual_groups'] = [{'id': 'shot-A', 'slide_ids': [1]}]
        studio.save(self.path, self.record)
        before = studio.read(self.path)
        Image.new('RGB', (18, 16), 'yellow').save(self.path / 'a.png')
        with patch.object(studio, 'plan_storyboard') as planner, \
                patch.object(studio, 'threading', SimpleNamespace(Thread=Mock())) as thread_api:
            for repair_only in (False, True):
                with self.subTest(repair_only=repair_only), self.assertRaises(HTTPException) as caught:
                    studio.plan(self.record['id'], self.request(), repair_only=repair_only)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(studio.read(self.path), before)
            planner.assert_not_called()
            thread_api.Thread.assert_not_called()

    def test_explicitly_resaved_identity_replans_fixed_shot_without_old_cast_or_context(self):
        self.boundary()
        baseline = copy.deepcopy(self.record)
        for repair_only in (False, True):
            with self.subTest(repair_only=repair_only):
                self.record = copy.deepcopy(baseline)
                shot = self.fixed_shot()
                self.record['shots'] = [shot]
                self.record['manual_groups'] = [{'id': 'shot-A', 'slide_ids': [1]}]
                fingerprint = studio.planning_fingerprint(self.record['scenes'], 'watercolor', '', '',
                    cast.planning_references(self.record), studio.planning_parameters(self.record))
                self.record['context'] = {'production_fingerprint': fingerprint, 'summary': 'Old role reading.'}
                self.record['planning_state'] = {'fingerprint': fingerprint, 'context': copy.deepcopy(self.record['context'])}
                rows = self.record['project_cast']['characters']
                self.save_cast([self.editable(rows[0], constraints='Changed explicit costume.'), self.editable(rows[1])])
                studio.save(self.path, self.record)

                def planned(*args, **kwargs):
                    self.assertIsNone(kwargs['resume_state'])
                    self.assertEqual(kwargs['repair_only'], repair_only)
                    fixed = kwargs['fixed_shots'][0]
                    self.assertNotIn('cast_plan', fixed)
                    self.assertTrue(fixed['design_needs_review'])
                    self.assertEqual(fixed['video_history'], shot['video_history'])
                    self.assertEqual(args[6]['project_cast'], self.snapshot())
                    return {'summary': 'Current role reading.'}, kwargs['fixed_shots']

                planner = Mock(side_effect=planned)
                sync_threads = SimpleNamespace(Event=threading.Event,
                    Thread=lambda target, daemon: SimpleNamespace(start=target))
                with patch.object(studio, 'threading', sync_threads), \
                        patch.object(studio, 'project_language_scope', return_value=nullcontext()), \
                        patch.object(studio, 'plan_storyboard', planner):
                    result = studio.plan(self.record['id'], self.request(), repair_only=repair_only)
                planner.assert_called_once()
                self.assertEqual(result['status'], 'storyboard_review', result.get('error'))
                self.assertEqual(result['context']['summary'], 'Current role reading.')
                self.assertFalse(studio.ACTIVE)

    def test_native_version_recheck_keeps_unchanged_completed_core_image(self):
        self.boundary()
        from backend.app import video_native_story as native
        design = self.fixed_shot()
        design.pop('video_history')
        self.record.update(native_story={'text': 'A waits; B enters.'},
            native_planning_state={'candidate': {'shots': [{'source_text': 'A waits; B enters.'}]},
                                   'version_revalidation': True},
            context={'project_cast': self.snapshot()}, status='image_review')
        produced = copy.deepcopy(design)
        produced['image_prompt'] = studio._image_inputs(
            self.path, self.record, design, design['image_prompt'], use_scene=False)[0]
        produced['image_material_numbers_bound'] = True
        produced.update(image='a.png', image_status='completed',
                        image_version=hashlib.sha256((self.path/'a.png').read_bytes()).hexdigest())
        self.record['shots'] = [produced]
        self.record['scene_references_status'] = 'completed'
        studio.save(self.path, self.record)
        sync_threads = SimpleNamespace(Event=threading.Event,
            Thread=lambda target, daemon: SimpleNamespace(start=target))
        with patch.object(studio, 'threading', sync_threads), \
                patch.object(studio, 'project_language_scope', return_value=nullcontext()), \
                patch('backend.app.video_auto_cast.ensure_auto_cast', return_value=False), \
                patch.object(native, 'plan_native_story', return_value=(
                    {'project_cast': self.snapshot()}, self.record['scenes'], [design])):
            result = studio.plan(self.record['id'], self.request())
        self.assertFalse(result.get('error'), result.get('error'))
        self.assertEqual(result['shots'][0]['image_status'], 'completed')
        self.assertEqual(result['shots'][0]['image_version'], produced['image_version'])
        self.assertEqual(result['shots'][0]['image'], 'a.png')
        self.assertEqual(result['scene_references_status'], 'completed')
        self.assertNotIn('edit_history', result)

    def test_image_reuse_checks_design_and_bytes_and_does_not_copy_video(self):
        base = self.fixed_shot()
        base.pop('video_history')
        produced = {**copy.deepcopy(base), 'image': 'a.png', 'image_status': 'completed',
            'image_version': hashlib.sha256((self.path/'a.png').read_bytes()).hexdigest(),
            'video': 'old.mp4', 'video_status': 'completed'}
        for change in ({'action': 'A leaves.'}, {'reference_ids': ['room', 'a']},
                       {'duration': 8}, {'image_prompt': 'Different opening.'}):
            self.record['shots'] = [copy.deepcopy(produced)]
            target = {**copy.deepcopy(base), **change}
            studio._retain_unchanged_native_images(self.path, self.record, [target])
            self.assertNotIn('image', target)
        for field, value in [('image_version', 'wrong'), ('image_prompt_out_of_sync', True),
                             ('image_task', {'status': 'failed'})]:
            self.record['shots'] = [{**copy.deepcopy(produced), field: value}]
            target = copy.deepcopy(base)
            studio._retain_unchanged_native_images(self.path, self.record, [target])
            self.assertNotIn('image', target)
        self.record['shots'] = [copy.deepcopy(produced)]
        target = copy.deepcopy(base)
        studio._retain_unchanged_native_images(self.path, self.record, [target])
        self.assertEqual(target['image'], 'a.png')
        self.assertNotIn('video', target)
        self.assertNotIn('video_status', target)

    def test_confirming_native_plan_preserves_reused_images_through_normalization(self):
        self.boundary()
        self.record['scenes'][0]['slide_id']='s1'
        design=self.fixed_shot();design['slide_ids']=['s1'];design.pop('video_history')
        self.record['references'][0]['label']='图3'
        self.record['references'][1]['label']='图1'
        design['reference_ids']=['room','a']
        design['image_prompt']='图1是本镜房间，图2是角色A。'
        normalized=studio.normalize_shots([design],self.record['scenes'],['room','a'])[0]
        normalized['image_material_numbers_bound']=True
        normalized.update(image='a.png',image_status='completed',
            image_version=hashlib.sha256((self.path/'a.png').read_bytes()).hexdigest())
        self.record.update(native_story={'text':'A waits; B enters.'},shots=[normalized],status='storyboard_review')
        studio.save(self.path,self.record)
        with patch('backend.app.video_agents.audit_storyboard',side_effect=lambda rows,refs:rows):
            result=studio.review_for_user(self.record['id'],studio.Review(revision=1),{'id':1})
        self.assertEqual(result['status'],'generation_ready')
        self.assertEqual(result['shots'][0].get('image_status'),'completed')
        self.assertEqual(result['shots'][0].get('image'),'a.png')
        self.assertTrue(result['shots'][0]['image_material_numbers_bound'])
        self.assertEqual(result['shots'][0]['image_prompt'],design['image_prompt'])

    def test_replanning_recovers_matching_archived_image_only_before_any_new_request(self):
        from backend.app.openai_image_transport import ledger_path
        design=self.fixed_shot();design.pop('video_history')
        archived={**copy.deepcopy(design),'image':'a.png','image_status':'completed',
            'image_version':hashlib.sha256((self.path/'a.png').read_bytes()).hexdigest()}
        pending={**copy.deepcopy(design),'image_prompt':'Damaged binding',
                 'image_status':'pending','image_output_round':'unused'}
        self.record.update(shots=[pending],edit_history=[{'shots':[archived]}])
        target=copy.deepcopy(design)
        studio._retain_unchanged_native_images(self.path,self.record,[target])
        self.assertEqual(target['image'],'a.png')
        self.assertEqual(self.record['shots'][0],pending)
        changed={**copy.deepcopy(design),'action':'A different action.'}
        studio._retain_unchanged_native_images(self.path,self.record,[changed])
        self.assertNotIn('image',changed)
        receipt=ledger_path(studio.storyboard_output(self.path,self.record,pending))
        receipt.parent.mkdir(parents=True,exist_ok=True);receipt.write_text('{"status":"dispatching"}')
        target=copy.deepcopy(design)
        studio._retain_unchanged_native_images(self.path,self.record,[target])
        self.assertNotIn('image',target)
        receipt.unlink()
        for field,value in [('image_task',{'status':'running'}),('image_error','Unknown outcome'),
                            ('image_attempt_output','accepted-output.jpg')]:
            self.record['shots']=[{**pending,field:value}]
            target=copy.deepcopy(design)
            studio._retain_unchanged_native_images(self.path,self.record,[target])
            self.assertNotIn('image',target)

    def test_reused_image_retains_scene_version_and_changed_scene_still_blocks(self):
        base = self.fixed_shot()
        base.pop('video_history')
        scene_version = hashlib.sha256((self.path/'room.png').read_bytes()).hexdigest()
        produced = {**copy.deepcopy(base), 'image': 'a.png', 'image_status': 'completed',
            'image_version': hashlib.sha256((self.path/'a.png').read_bytes()).hexdigest(),
            'scene_reference_id': 'room-asset', 'scene_reference_used_version': scene_version}
        self.record['shots'] = [produced]
        self.record['creation_parameters'].update(dynamic_text_mode='visual_first', scene_references_enabled=True)
        self.record['scene_references_status'] = 'completed'
        asset = dict(id='room-asset', image='room.png', image_status='completed', image_version=scene_version)
        self.record['scene_assets'] = [asset]
        target = copy.deepcopy(base)
        studio._retain_unchanged_native_images(self.path, self.record, [target])
        self.record['shots'] = [target]
        self.assertEqual(studio.storyboard_review_error(self.path, self.record), '')
        Image.new('RGB', (18,16), 'green').save(self.path/'room.png')
        asset['image_version'] = hashlib.sha256((self.path/'room.png').read_bytes()).hexdigest()
        self.assertEqual(target['scene_reference_used_version'], scene_version)
        self.assertIn('旧版场景', studio.storyboard_review_error(self.path, self.record))
        self.assertEqual(target['image_status'], 'completed')

    def test_normal_plan_route_hands_off_current_report_and_rejects_old_candidate(self):
        self.boundary()
        from backend.app import video_native_story as native, video_native_image_quality as quality
        baseline=copy.deepcopy(self.record)
        design=self.fixed_shot();design['id']='shot_001';design.pop('video_history')
        report=dict(fingerprint='current-report',image_versions={'shot_001':'image-hash'},
            review=dict(issues=[dict(shot_id='shot_001',code='opening_state',
                evidence='The core image hides the required doorway.',
                correction='Keep the doorway visible behind the character.')],continuity_checks=[]))
        for stale in (False,True):
            with self.subTest(stale=stale):
                self.record=copy.deepcopy(baseline)
                self.record.update(native_story={'text':'A waits; B enters.'},
                    native_planning_state={'candidate':{'shots':[{'source_text':'A waits; B enters.'}]},
                                           'review':[],'story_review':{'problems':[]}},
                    context={'project_cast':self.snapshot()},shots=[copy.deepcopy(design)])
                if stale:self.record['shots'][0]['image_prompt']='User changed current design.'
                studio.save(self.path,self.record)
                before=studio.read(self.path)
                def planned(record,*args,**kwargs):
                    repair=record['native_planning_state']['repair']
                    self.assertEqual(repair['shot_ids'],['shot_001'])
                    self.assertEqual(repair['image_problems'][0]['fingerprint'],'current-report')
                    return {'project_cast':self.snapshot()},self.record['scenes'],[copy.deepcopy(design)]
                sync=SimpleNamespace(Event=threading.Event,Thread=lambda target,daemon:SimpleNamespace(start=target))
                with patch.object(quality,'cached_current_findings',return_value=[report]), \
                     patch.object(native,'compile_storyboard',return_value=(self.record['scenes'],[design])), \
                     patch.object(native,'plan_native_story',side_effect=planned) as planner, \
                     patch('backend.app.video_auto_cast.ensure_auto_cast',return_value=False), \
                     patch.object(studio,'project_language_scope',return_value=nullcontext()), \
                     patch.object(studio,'threading',sync):
                    if stale:
                        with self.assertRaises(HTTPException) as caught:
                            studio.plan(self.record['id'],self.request())
                        self.assertEqual(caught.exception.status_code,409)
                        self.assertEqual(studio.read(self.path),before)
                        planner.assert_not_called()
                    else:
                        result=studio.plan(self.record['id'],self.request())
                        self.assertEqual(result['status'],'storyboard_review',result.get('error'))
                        planner.assert_called_once()

    def test_added_role_replans_empty_or_unchanged_role_selection_in_fixed_and_repair_modes(self):
        self.boundary()
        baseline = copy.deepcopy(self.record)
        for selected in ((), (0,)):
            for repair_only in (False, True):
                with self.subTest(selected=selected, repair_only=repair_only):
                    self.record = copy.deepcopy(baseline)
                    shot = self.fixed_shot()
                    original_a = self.record['project_cast']['characters'][0]
                    self.save_cast([self.editable(original_a)])
                    old_cast = self.snapshot()
                    shot.update(cast_plan=self.plan(selected), reference_ids=['a'] if selected else [],
                                design_needs_review=False)
                    old_plan = copy.deepcopy(shot['cast_plan'])
                    self.record['shots'] = [shot]
                    self.record['manual_groups'] = [{'id': 'shot-A', 'slide_ids': [1]}]
                    fingerprint = studio.planning_fingerprint(self.record['scenes'], 'watercolor', '', '',
                        cast.planning_references(self.record), studio.planning_parameters(self.record))
                    self.record['context'] = {'production_fingerprint': fingerprint, 'project_cast': old_cast,
                                             'summary': 'Only A was available during the old reading.'}
                    self.record['planning_state'] = {'fingerprint': fingerprint,
                        'context': copy.deepcopy(self.record['context']), 'shots': copy.deepcopy(self.record['shots'])}
                    self.save_cast([self.editable(original_a),
                        {'name': 'B', 'reference_id': 'b', 'constraints': 'Green dress.'}])
                    current_cast = self.snapshot()
                    # Existing selections still validate: the changed roster, not a stale
                    # selected identity, must force the director to consider B's entrance.
                    self.assertEqual(current_cast['characters'][0], old_cast['characters'][0])
                    self.assertEqual(cast.validate_cast_plan(current_cast, old_plan), old_plan)
                    studio.save(self.path, self.record)

                    def planned(*args, **kwargs):
                        self.assertIsNone(kwargs['resume_state'])
                        self.assertEqual(kwargs['repair_only'], repair_only)
                        fixed = kwargs['fixed_shots'][0]
                        self.assertTrue(fixed['design_needs_review'])
                        self.assertNotIn('cast_plan', fixed)
                        self.assertEqual(fixed['slide_ids'], [1])
                        self.assertEqual(fixed['video_history'], shot['video_history'])
                        self.assertEqual(args[6]['project_cast'], current_cast)
                        renewed = {**fixed, 'image_prompt': 'New opening design after considering B.',
                                   'video_prompt': 'B enters after A waits.', 'cast_plan': self.plan(),
                                   'reference_ids': ['a'], 'design_needs_review': False}
                        return {'project_cast': current_cast, 'summary': 'A waits before B enters.'}, [renewed]

                    planner = Mock(side_effect=planned)
                    sync_threads = SimpleNamespace(Event=threading.Event,
                        Thread=lambda target, daemon: SimpleNamespace(start=target))
                    with patch.object(studio, 'threading', sync_threads), \
                            patch.object(studio, 'project_language_scope', return_value=nullcontext()), \
                            patch.object(studio, 'plan_storyboard', planner):
                        result = studio.plan(self.record['id'], self.request(), repair_only=repair_only)
                    planner.assert_called_once()
                    self.assertEqual(result['status'], 'storyboard_review', result.get('error'))
                    self.assertEqual(result['context']['summary'], 'A waits before B enters.')
                    self.assertEqual(result['shots'][0]['video_prompt'], 'B enters after A waits.')
                    self.assertNotIn('planning_state', result)
                    self.assertFalse(studio.ACTIVE)

    def test_review_and_generation_block_missing_stale_cleared_or_wrong_reference_plan(self):
        self.boundary()
        baseline = copy.deepcopy(self.record)
        cases = ('missing', 'stale', 'cleared', 'missing_opening', 'later_in_opening', 'damaged_after_clear')
        with patch.object(studio, '_image_configs') as configurations, \
                patch.object(studio, '_start_storyboard_images') as images:
            for operation in ('review', 'generate'):
                for cause in cases:
                    with self.subTest(operation=operation, cause=cause):
                        self.record = copy.deepcopy(baseline)
                        shot = self.fixed_shot()
                        self.record['shots'] = [shot]
                        self.record['status'] = 'storyboard_review' if operation == 'review' else 'generation_ready'
                        if cause == 'missing':
                            shot.pop('cast_plan')
                        elif cause == 'stale':
                            rows = self.record['project_cast']['characters']
                            self.save_cast([self.editable(rows[0], constraints='New explicit costume.'), self.editable(rows[1])])
                        elif cause in ('cleared', 'damaged_after_clear'):
                            self.save_cast([])
                            if cause == 'damaged_after_clear':
                                shot['cast_plan'] = ['damaged old plan']
                        elif cause == 'missing_opening':
                            shot['reference_ids'] = []
                        elif cause == 'later_in_opening':
                            shot['reference_ids'] = ['a', 'b']
                        studio.save(self.path, self.record)
                        before = studio.read(self.path)
                        fn = studio.review if operation == 'review' else studio.generate_storyboard_images
                        with self.assertRaises(HTTPException) as caught:
                            fn(self.record['id'], studio.Review(revision=before['revision']), self.request())
                        self.assertEqual(caught.exception.status_code, 400)
                        self.assertEqual(studio.read(self.path), before)
            configurations.assert_not_called()
            images.assert_not_called()


if __name__ == '__main__':
    unittest.main()
