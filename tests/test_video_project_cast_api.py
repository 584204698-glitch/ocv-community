"""Project identity binding must never manufacture per-shot visibility evidence."""
import copy
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from PIL import Image

from backend.app import video_inputs as inputs, video_studio as studio, video_generation as generation
from tests import test_video_character_coverage as fixtures


class ProjectCastApiTest(unittest.TestCase):
    setUp = fixtures.VideoCharacterCoverageTest.setUp
    read = fixtures.VideoCharacterCoverageTest.read
    coverage = fixtures.VideoCharacterCoverageTest.coverage
    confirm = fixtures.VideoCharacterCoverageTest.confirm
    save_selection = fixtures.VideoCharacterCoverageTest.save_selection
    actor = staticmethod(fixtures.VideoCharacterCoverageTest.actor)

    def save_cast(self, rows):
        response = self.client.put(self.base + '/project-cast', json={
            'revision': self.read()['revision'], 'characters': rows})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def make_cast(self):
        return self.save_cast([{'name': '米粒', 'reference_id': 'actor-a',
                                'constraints': '没有可见外耳，始终穿蓝色马甲'}])['characters'][0]

    def declaration(self, cast, presence='opening'):
        return self.actor(name=cast['name'], presence=presence, reference_id=cast['reference_id'],
                          project_character_id=cast['id'])

    @staticmethod
    def editable_cast(cast, **changes):
        return {**{key: cast[key] for key in ('id', 'name', 'reference_id', 'constraints')}, **changes}

    def frozen_failure(self):
        cast = self.make_cast()
        self.assertEqual(self.confirm([self.declaration(cast)]).status_code, 200)
        record = self.read()
        record['status'] = 'video_review'
        shot = record['shots'][0]
        selection, images = inputs.resolve_video_inputs(self.path, record, shot)
        identities = generation._confirmed_character_inputs(1, self.path, record, shot, selection, images)
        generation._freeze_local_request(self.path, record, shot, 'single_first', character_inputs=identities)
        shot.update(video_status='failed', video_task_id='accepted-cast-task')
        frozen = copy.deepcopy(shot['video_request'])
        studio.save(self.path, record)
        self.save_cast([self.editable_cast(cast, constraints='蓝色马甲，右手不能提重物')])
        self.assertEqual(self.coverage()['status'], 'stale')
        return frozen

    def test_binding_and_get_do_not_confirm_shots_or_change_inputs(self):
        original = copy.deepcopy(self.read()['shots'])
        cast = self.make_cast()
        self.assertEqual(self.read()['shots'], original)
        self.assertEqual(self.coverage()['status'], 'unknown')
        response = self.client.get(self.base + '/project-cast')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['characters'][0], cast)
        self.assertTrue(response.json()['assets'][0]['url'].startswith(self.base))
        self.assertNotIn('file', cast)

    def test_explicit_link_preserves_later_timing_and_needs_actual_selected_reference(self):
        cast = self.make_cast()
        response = self.confirm([self.declaration(cast, 'later')])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.coverage()['status'], 'partial')
        self.assertEqual(self.coverage()['characters'][0]['presence'], 'later')
        self.assertEqual(self.save_selection('composition_reference', reference_ids=['actor-a']).status_code, 200)
        view = self.coverage()
        self.assertEqual(view['status'], 'covered')
        row = view['characters'][0]
        self.assertEqual(row['project_character_id'], cast['id'])
        self.assertEqual(row['constraints'], cast['constraints'])
        record = self.read()
        selection, images = inputs.resolve_video_inputs(self.path, record, record['shots'][0])
        frozen = generation._confirmed_character_inputs(1, self.path, record, record['shots'][0], selection, images)
        self.assertEqual(frozen['characters'][0]['picture_labels'], ['<Picture 2>'])
        self.assertEqual(frozen['characters'][0]['identity_sha256'], cast['identity_sha256'])
        self.assertEqual(frozen['characters'][0]['constraints'], cast['constraints'])

    def test_rebinding_excludes_old_identity_from_planning_but_keeps_assets_and_voices(self):
        from backend.app.video_cast_planning import planning_references
        cast = self.make_cast()
        record = self.read()
        record['project_voices'] = {'saved_voice_marker': cast['id']}
        studio.save(self.path, record)
        before = copy.deepcopy(record)
        changed = self.save_cast([self.editable_cast(cast, reference_id='actor-b')])
        after = self.read()
        self.assertEqual(changed['characters'][0]['id'], cast['id'])
        self.assertEqual(after['references'], before['references'])
        self.assertEqual(after['shots'], before['shots'])
        self.assertEqual(after['project_voices'], before['project_voices'])
        self.assertEqual([r['id'] for r in planning_references(after)], ['tail', 'actor-b'])
        self.assertTrue((self.path / 'actor-a.png').is_file())
        self.assertIn('actor-a', [r['id'] for r in changed['assets']])
        self.save_cast([self.editable_cast(cast)])
        restored = self.read()
        self.assertEqual([r['id'] for r in planning_references(restored)], ['tail', 'actor-a'])
        self.assertEqual(restored['retired_character_reference_ids'], ['actor-b'])

    def test_shared_reference_is_retired_only_when_no_character_uses_it(self):
        from backend.app.video_cast_planning import planning_references
        first, second = self.save_cast([
            {'name': 'A', 'reference_id': 'actor-a'},
            {'name': 'B', 'reference_id': 'actor-a'},
        ])['characters']
        self.save_cast([self.editable_cast(first, reference_id='actor-b'), self.editable_cast(second)])
        self.assertIn('actor-a', [r['id'] for r in planning_references(self.read())])
        self.save_cast([])
        self.assertEqual([r['id'] for r in planning_references(self.read())], ['tail'])

    def test_project_identity_edit_invalidates_linked_review_only_and_keeps_clips(self):
        cast = self.make_cast()
        self.assertEqual(self.confirm([self.declaration(cast)]).status_code, 200)
        second = self.base + '/shots/shot-B/character-coverage'
        response = self.client.put(second, json={'revision': self.read()['revision'],
            'characters': [self.actor('Unlinked', 'opening')]})
        self.assertEqual(response.status_code, 200, response.text)
        before = copy.deepcopy(self.read()['shots'])
        self.save_cast([{key: cast[key] for key in ('id', 'name', 'reference_id')}
                        | {'constraints': '蓝色马甲保持完整，头部没有外耳'}])
        self.assertEqual(self.read()['shots'], before)
        self.assertEqual(self.coverage()['status'], 'stale')
        other = self.client.get(self.base + '/shots/shot-B/video-inputs').json()
        self.assertEqual(other['coverage']['status'], 'covered')

    def test_unknown_foreign_or_mismatched_links_cannot_create_confirmation(self):
        cast = self.make_cast()
        for change in ({'project_character_id': 'pc_' + 'd'*32}, {'name': '同名不猜'},
                       {'reference_id': 'actor-b'}):
            response = self.confirm([self.declaration(cast) | change])
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self.coverage()['status'], 'unknown')
        with patch.object(studio, 'require_user', lambda request: {'id': 2}):
            self.assertEqual(self.client.get(self.base + '/project-cast').status_code, 404)

    def test_replaced_or_deleted_project_identity_is_stale_and_cannot_generate(self):
        cast = self.make_cast()
        self.assertEqual(self.confirm([self.declaration(cast)]).status_code, 200)
        Image.new('RGB', (18, 16), 'yellow').save(self.path / 'actor-a.png')
        view = self.client.get(self.base + '/project-cast').json()['characters'][0]
        self.assertEqual(view['status'], 'stale')
        self.assertFalse(view['identity_sha256'])
        self.assertEqual(self.coverage()['status'], 'stale')
        with self.assertRaises(ValueError):
            inputs.validate_shot_inputs(1, self.path, self.read(), self.read()['shots'][0])
        self.save_cast([])
        self.assertEqual(self.coverage()['status'], 'stale')

    def test_repeated_save_is_noop_and_revision_conflict_preserves_cast(self):
        cast = self.make_cast()
        rows = [{key: cast[key] for key in ('id', 'name', 'reference_id', 'constraints')}]
        before = copy.deepcopy(self.read())
        self.save_cast(rows)
        self.assertEqual(self.read(), before)
        response = self.client.put(self.base + '/project-cast', json={'revision': 1, 'characters': []})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.read(), before)

    def test_legacy_shot_declarations_ignore_even_an_unreadable_unlinked_cast(self):
        self.assertEqual(self.confirm([self.actor('Legacy A', 'opening')]).status_code, 200)
        record = self.read()
        review = record['shots'][0]['video_character_review']
        for character in review['characters']:
            character.pop('project_character_id', None)
        review.pop('project_characters', None)
        record['project_cast'] = {'version': 999, 'characters': 'damaged unrelated table'}
        studio.save(self.path, record)
        before = copy.deepcopy(self.read())
        self.assertEqual(self.coverage()['status'], 'covered')
        shot = before['shots'][0]
        selection, images = inputs.resolve_video_inputs(self.path, before, shot)
        frozen = generation._confirmed_character_inputs(1, self.path, before, shot, selection, images)
        self.assertEqual(frozen['status'], 'covered')
        self.assertEqual(frozen['characters'][0]['name'], 'Legacy A')
        self.assertEqual(frozen['characters'][0]['picture_labels'], ['<Picture 1>'])
        for field in ('project_character_id', 'identity_sha256', 'constraints'):
            self.assertNotIn(field, frozen['characters'][0])
        self.assertEqual(self.read(), before)

    def test_changing_one_cast_member_keeps_another_linked_shot_current(self):
        rows = self.save_cast([
            {'name': '米粒', 'reference_id': 'actor-a', 'constraints': '蓝色马甲'},
            {'name': '白耳', 'reference_id': 'actor-b', 'constraints': '白色围巾'},
        ])['characters']
        first, second = rows
        self.assertEqual(self.confirm([self.declaration(first)]).status_code, 200)
        response = self.client.put(self.base + '/shots/shot-B/character-coverage', json={
            'revision': self.read()['revision'], 'characters': [self.declaration(second)]})
        self.assertEqual(response.status_code, 200, response.text)
        before_shots = copy.deepcopy(self.read()['shots'])
        self.save_cast([self.editable_cast(first, constraints='蓝色马甲不能变色'), self.editable_cast(second)])
        self.assertEqual(self.read()['shots'], before_shots)
        self.assertEqual(self.coverage()['status'], 'stale')
        other = self.client.get(self.base + '/shots/shot-B/video-inputs')
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(other.json()['coverage']['status'], 'covered')
        self.assertEqual(other.json()['coverage']['characters'][0]['identity_sha256'], second['identity_sha256'])

    def test_damaged_cast_or_link_hash_table_is_stale_without_server_error(self):
        cast = self.make_cast()
        self.assertEqual(self.confirm([self.declaration(cast)]).status_code, 200)
        baseline = copy.deepcopy(self.read())
        cases = [('cast', {'version': 999}), ('cast', ['unreadable']),
                 ('bindings', ['bad hash table']), ('bindings', 'bad hash table'), ('bindings', 17),
                 ('bindings', {}), ('bindings', {cast['id']: '0' * 64})]
        for target, value in cases:
            with self.subTest(target=target, value=value):
                record = copy.deepcopy(baseline)
                if target == 'cast':
                    record['project_cast'] = value
                else:
                    record['shots'][0]['video_character_review']['project_characters'] = value
                studio.save(self.path, record)
                response = self.client.get(self.url + '/video-inputs')
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['coverage']['status'], 'stale')
                selection, images = inputs.resolve_video_inputs(self.path, record, record['shots'][0])
                frozen = generation._confirmed_character_inputs(1, self.path, record, record['shots'][0], selection, images)
                self.assertEqual(frozen, {'status': 'stale', 'characters': []})

    def test_cast_change_blocks_actual_fresh_regeneration_and_preserves_existing_media(self):
        cast = self.make_cast()
        self.assertEqual(self.confirm([self.declaration(cast)]).status_code, 200)
        record = self.read()
        record['status'] = 'completed'
        (self.path / 'existing.mp4').write_bytes(b'existing finished clip')
        (self.path / 'older.mp4').write_bytes(b'older historical clip')
        shot = record['shots'][0]
        shot.update(video='existing.mp4', video_status='completed', video_task_id='old-completed-task',
                    video_request={'backend': 'comfyui', 'prompt': 'previous frozen request'},
                    video_history=[{'video': 'older.mp4', 'reason': 'earlier attempt'}],
                    h3_prompt_source='previous cached h3 input')
        studio.save(self.path, record)
        self.save_cast([self.editable_cast(cast, constraints='蓝色马甲，右手不能提重物')])
        before = copy.deepcopy(self.read())
        files_before = {path.name: path.read_bytes() for path in self.path.glob('*.mp4')}
        with patch.object(generation, '_start_local_worker') as worker, \
                patch.object(generation, '_freeze_local_request') as freeze, \
                patch.object(generation, 'run_video_profile') as bridge:
            response = self.client.post(self.base + '/videos/generate', json={
                'revision': before['revision'], 'shot_ids': ['shot-A'], 'regenerate_completed': True})
            self.assertEqual(response.status_code, 400, response.text)
            worker.assert_not_called()
            freeze.assert_not_called()
            bridge.assert_not_called()
        self.assertEqual(self.read(), before)
        self.assertEqual({path.name: path.read_bytes() for path in self.path.glob('*.mp4')}, files_before)

    def test_stale_project_role_does_not_block_retrieval_of_an_accepted_frozen_task(self):
        frozen = self.frozen_failure()
        before = self.read()

        def retrieve(*args, **kwargs):
            self.assertEqual(kwargs['existing_prompt_id'], 'accepted-cast-task')
            kwargs['output_path'].write_bytes(b'recovered previously accepted output')

        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'video_task_state', return_value='completed'), \
                patch.object(generation, 'run_video_profile', side_effect=retrieve) as bridge, \
                patch.object(generation, '_freeze_local_request') as freeze:
            response = self.client.post(self.base + '/videos/generate', json={
                'revision': before['revision'], 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            bridge.assert_called_once()
            freeze.assert_not_called()
        current = self.read()['shots'][0]
        self.assertEqual(current['video_request'], frozen)
        self.assertEqual(current['video_attempt'], 1)
        self.assertEqual(current['video_status'], 'completed')
        self.assertEqual(current['video_request']['character_inputs']['characters'][0]['constraints'], '没有可见外耳，始终穿蓝色马甲')

    def test_missing_accepted_task_cannot_create_a_replacement_using_stale_project_role(self):
        frozen = self.frozen_failure()
        before = self.read()
        with patch.object(generation.threading, 'Thread') as thread, \
                patch.object(generation, 'video_task_state', return_value='missing'), \
                patch.object(generation, 'run_video_profile') as bridge, \
                patch.object(generation, '_freeze_local_request') as freeze:
            response = self.client.post(self.base + '/videos/generate', json={
                'revision': before['revision'], 'shot_ids': ['shot-A']})
            self.assertEqual(response.status_code, 200, response.text)
            thread.call_args.kwargs['target']()
            bridge.assert_not_called()
            freeze.assert_not_called()
        current = self.read()['shots'][0]
        self.assertEqual(current['video_request'], frozen)
        self.assertEqual(current['video_attempt'], 1)
        self.assertEqual(current['video_status'], 'failed')
        self.assertIn('原角色确认已过期', current['video_error'])

    def test_owner_and_revision_guards_cover_project_cast_and_per_shot_links(self):
        before = copy.deepcopy(self.read())
        cast = self.make_cast()
        current = copy.deepcopy(self.read())
        response = self.client.put(self.url + '/character-coverage', json={
            'revision': before['revision'], 'characters': [self.declaration(cast)]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.read(), current)
        with patch.object(studio, 'require_user', return_value={'id': 2}):
            for suffix, body in (('/project-cast', {'revision': current['revision'], 'characters': []}),
                                 ('/shots/shot-A/character-coverage', {'revision': current['revision'],
                                    'characters': [self.declaration(cast)]})):
                response = self.client.put(self.base + suffix, json=body)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertNotIn(cast['name'], response.text)
        with patch.object(studio, 'require_user', side_effect=HTTPException(401, '需要登录')):
            self.assertEqual(self.client.get(self.base + '/project-cast').status_code, 401)
            self.assertEqual(self.client.put(self.base + '/project-cast', json={
                'revision': current['revision'], 'characters': []}).status_code, 401)
        self.assertEqual(self.read(), current)


if __name__ == '__main__':
    unittest.main()
