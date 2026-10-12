"""Project identity bindings are explicit, scoped and based on real image bytes."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError

from backend.app.video_project_cast import (
    ProjectCastDeclaration, ProjectCastMutation, build_project_cast, project_cast_view,
)


class VideoProjectCastTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / '1' / ('a' * 32)
        self.path.mkdir(parents=True)
        for name, color in (('actor-a', 'red'), ('actor-b', 'blue')):
            Image.new('RGB', (18, 16), color).save(self.path / (name + '.png'))
        self.catalog = [{'id': name, 'name': '同名角色', 'file': name + '.png'} for name in ('actor-a', 'actor-b')]
        self.record = {'id': 'a' * 32, 'revision': 1, 'settings': {'characters': '不能作为图片证据'},
                       'context': {'characters': [{'character_id': 'actor-a', 'name': '同名角色'}]},
                       'shots': [{'id': 'shot-a', 'video_character_review': {'source': 'sentinel'}}]}

    @staticmethod
    def actor(**changes):
        return {'id': '', 'name': '阿宁', 'reference_id': 'actor-a', 'constraints': '始终穿蓝色围裙。', **changes}

    def build(self, declarations, **kwargs):
        return build_project_cast(kwargs.get('owner', 1), kwargs.get('path', self.path),
                                  kwargs.get('record', self.record), kwargs.get('catalog', self.catalog), declarations)

    def save(self, declarations):
        self.record['project_cast'] = self.build(declarations)
        return self.record['project_cast']

    def view(self, **kwargs):
        return project_cast_view(kwargs.get('owner', 1), kwargs.get('path', self.path),
                                 kwargs.get('record', self.record), kwargs.get('catalog', self.catalog))

    @staticmethod
    def editable(row, **changes):
        return {**{key: row[key] for key in ('id', 'name', 'reference_id', 'constraints')}, **changes}

    def test_explicit_binding_uses_real_bytes_and_does_not_mutate_the_project(self):
        before, catalog = copy.deepcopy(self.record), copy.deepcopy(self.catalog)
        declarations = [self.actor()]
        original_declarations = copy.deepcopy(declarations)
        saved = self.build(declarations)
        self.assertEqual(self.record, before)
        self.assertEqual(self.catalog, catalog)
        self.assertEqual(declarations, original_declarations)
        self.assertEqual(saved['version'], 1)
        self.assertEqual(saved['source'], 'user_confirmed_project_identity')
        self.assertEqual(saved['owner_id'], 1)
        self.assertEqual(saved['project_id'], self.record['id'])
        row = saved['characters'][0]
        self.assertRegex(row['id'], r'^pc_[a-f0-9]{32}$')
        self.assertEqual(row['reference_sha256'], hashlib.sha256((self.path / 'actor-a.png').read_bytes()).hexdigest())
        self.assertRegex(row['identity_sha256'], r'^[a-f0-9]{64}$')
        self.assertNotIn('presence', row)
        self.assertNotIn('covered', row)
        self.assertNotIn('file', row)
        self.assertEqual(sorted(item.name for item in self.path.iterdir()), ['actor-a.png', 'actor-b.png'])

    def test_models_require_explicit_array_and_valid_project_reference(self):
        invalid = [
            {}, {'characters': None}, {'characters': [self.actor(reference_id='')]},
            {'characters': [self.actor(reference_id='   ')]}, {'characters': [self.actor(name=' ')]},
            {'characters': [self.actor(constraints='字' * 2001)]},
            {'characters': [self.actor(id='agent0_character')]},
            {'characters': [self.actor(presence='opening')]},
            {'characters': [self.actor(file='outside.png')]},
            {'characters': [self.actor()] * 17},
            {'revision': True, 'characters': []}, {'revision': '1', 'characters': []},
            {'characters': [self.actor(constraints='bad\0text')]},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ProjectCastMutation.model_validate({'revision': 1, **changes})
        accepted = ProjectCastMutation(revision=1, characters=[self.actor(id=None, name=' 阿宁 ', constraints='  蓝围裙  ')])
        self.assertEqual(accepted.characters[0].id, '')
        self.assertEqual(accepted.characters[0].name, '阿宁')
        self.assertEqual(accepted.characters[0].constraints, '蓝围裙')
        self.assertTrue(self.build(accepted.characters)['characters'])

    def test_new_rows_receive_distinct_program_owned_ids(self):
        rows = self.save([self.actor(), self.actor(name='送信人', reference_id='actor-b')])['characters']
        self.assertNotEqual(rows[0]['id'], rows[1]['id'])
        self.assertTrue(all(row['status'] == 'current' for row in self.view()['characters']))

    def test_empty_and_missing_cast_do_not_infer_roles_from_agent_or_asset_names(self):
        self.assertEqual(self.view(), {'characters': []})
        saved = self.save([])
        self.assertEqual(saved['characters'], [])
        self.assertEqual(self.view(), {'characters': []})
        self.assertEqual(self.record['shots'][0]['video_character_review'], {'source': 'sentinel'})

    def test_empty_array_can_explicitly_clear_a_malformed_prior_record(self):
        self.record['project_cast'] = {'unexpected': 'old corrupted record'}
        self.save([])
        self.assertEqual(self.view(), {'characters': []})

    def test_names_do_not_resolve_assets_and_duplicate_names_are_allowed(self):
        for reference in ('同名角色', 'actor-a.png', 'unknown'):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                self.build([self.actor(reference_id=reference)])
        saved = self.save([self.actor(name='同名角色'), self.actor(name='同名角色', reference_id='actor-b')])
        self.assertEqual(len(saved['characters']), 2)
        self.assertNotEqual(saved['characters'][0]['reference_sha256'], saved['characters'][1]['reference_sha256'])

    def test_foreign_and_guessed_character_ids_are_rejected(self):
        foreign = self.build([self.actor()], record={'id': 'b' * 32})['characters'][0]['id']
        for identity in (foreign, 'pc_' + 'f' * 32):
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, '不属于本项目'):
                self.build([self.actor(id=identity)])

    def test_saved_id_is_reused_but_duplicate_submitted_ids_are_rejected(self):
        row = self.save([self.actor()])['characters'][0]
        updated = self.save([self.editable(row, name='阿宁的新称呼')])['characters'][0]
        self.assertEqual(updated['id'], row['id'])
        self.assertNotEqual(updated['identity_sha256'], row['identity_sha256'])
        with self.assertRaises(ValueError):
            self.build([self.editable(updated), self.editable(updated)])

    def test_removed_character_id_cannot_be_reintroduced_by_guessing_old_identity(self):
        row = self.save([self.actor()])['characters'][0]
        self.save([])
        with self.assertRaises(ValueError):
            self.build([self.editable(row)])

    def test_scope_mismatch_is_rejected_without_exposing_foreign_character_fields(self):
        self.save([self.actor()])
        for owner, record in ((2, self.record), (1, {**self.record, 'id': 'b' * 32})):
            with self.subTest(owner=owner, project=record['id']):
                with self.assertRaises(ValueError) as caught:
                    self.view(owner=owner, record=record)
                self.assertNotIn('阿宁', str(caught.exception))
                with self.assertRaises(ValueError):
                    self.build([self.actor()], owner=owner, record=record)

    def test_revision_shot_state_and_unrelated_reference_changes_do_not_expire_identity(self):
        saved = self.save([self.actor()])['characters'][0]
        self.record['revision'] = 987
        self.record['settings'] = {'name': 'Changed project label', 'style': 'watercolor'}
        self.record['shots'][0]['action'] = 'Different shot action'
        self.record['context'] = {'characters': []}
        self.catalog[1]['file'] = 'missing-unrelated-reference.png'
        self.catalog[0]['name'] = 'A changed asset label'
        current = self.view()['characters'][0]
        self.assertEqual(current['status'], 'current')
        self.assertEqual(current['identity_sha256'], saved['identity_sha256'])
        repeated = self.build([self.editable(saved)])['characters'][0]
        self.assertEqual(repeated['identity_sha256'], saved['identity_sha256'])

    def test_name_constraints_and_reference_identity_each_change_the_binding_token(self):
        original = self.save([self.actor()])['characters'][0]
        # Same bytes under another explicit asset ID are a different binding.
        self.catalog.append({'id': 'same-picture-alias', 'file': 'actor-a.png'})
        for changes in ({'name': '新的称呼'}, {'constraints': '右手不能提重物'}, {'reference_id': 'same-picture-alias'}):
            with self.subTest(changes=changes):
                updated = self.build([self.editable(original, **changes)])['characters'][0]
                self.assertNotEqual(updated['identity_sha256'], original['identity_sha256'])
                self.assertEqual(updated['reference_sha256'], original['reference_sha256'])

    def test_replaced_image_stales_only_its_character_until_explicitly_saved_again(self):
        saved = self.save([self.actor(), self.actor(name='送信人', reference_id='actor-b')])
        first, second = saved['characters']
        Image.new('RGB', (18, 16), 'green').save(self.path / 'actor-a.png')
        rows = self.view()['characters']
        self.assertEqual(rows[0]['status'], 'stale')
        self.assertEqual(rows[0]['identity_sha256'], '')
        self.assertEqual(rows[0]['reference_sha256'], first['reference_sha256'])
        self.assertEqual(rows[1]['status'], 'current')
        self.assertEqual(rows[1]['identity_sha256'], second['identity_sha256'])
        renewed = self.save([self.editable(first), self.editable(second)])['characters']
        self.assertEqual(renewed[0]['id'], first['id'])
        self.assertNotEqual(renewed[0]['identity_sha256'], first['identity_sha256'])
        self.assertTrue(all(row['status'] == 'current' for row in self.view()['characters']))

    def test_same_bytes_moved_inside_project_keep_the_same_identity(self):
        saved = self.save([self.actor()])['characters'][0]
        (self.path / 'nested').mkdir()
        (self.path / 'nested' / 'moved.png').write_bytes((self.path / 'actor-a.png').read_bytes())
        self.catalog[0]['file'] = 'nested/moved.png'
        row = self.view()['characters'][0]
        self.assertEqual(row['status'], 'current')
        self.assertEqual(row['identity_sha256'], saved['identity_sha256'])

    def test_missing_duplicate_and_invalid_assets_never_prove_identity(self):
        self.save([self.actor()])
        for catalog in ([], self.catalog + [copy.deepcopy(self.catalog[0])],
                        [{'id': 'actor-a', 'file': 'missing.png'}]):
            with self.subTest(catalog=catalog):
                row = self.view(catalog=catalog)['characters'][0]
                self.assertEqual(row['status'], 'stale')
                self.assertEqual(row['identity_sha256'], '')
                with self.assertRaises(ValueError):
                    self.build([self.actor()], catalog=catalog)
        (self.path / 'actor-a.png').write_bytes(b'this is not an image')
        self.assertEqual(self.view()['characters'][0]['status'], 'stale')
        with self.assertRaisesRegex(ValueError, '有效的图片'):
            self.build([self.actor()])

    def test_external_absolute_traversal_and_windows_drive_paths_are_rejected(self):
        outside = self.root / 'private-image.png'
        Image.new('RGB', (8, 8), 'white').save(outside)
        candidates = [str(outside), '../../private-image.png', '..\\..\\private-image.png',
                      'C:/private/image.png', 'C:private.png', '//server/share/image.png',
                      '/outside.png', 'actor-a.png:private-stream']
        for relative in candidates:
            with self.subTest(relative=relative), self.assertRaises(ValueError) as caught:
                self.build([self.actor()], catalog=[{'id': 'actor-a', 'file': relative}])
            self.assertNotIn(str(self.root), str(caught.exception))

    def test_symlink_escaping_project_cannot_bind_an_external_image(self):
        outside = self.root / 'outside.png'
        Image.new('RGB', (8, 8), 'white').save(outside)
        link = self.path / 'linked.png'
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('Creating symlinks is unavailable in this test environment')
        with self.assertRaises(ValueError):
            self.build([self.actor()], catalog=[{'id': 'actor-a', 'file': 'linked.png'}])

    def test_unreadable_image_errors_do_not_expose_filesystem_paths(self):
        self.save([self.actor()])
        secret = str(self.root / 'private-location')
        with patch.object(Path, 'read_bytes', side_effect=PermissionError(secret)):
            row = self.view()['characters'][0]
            self.assertEqual(row['status'], 'stale')
            self.assertNotIn(secret, row['message'])
            with self.assertRaises(ValueError) as caught:
                self.build([self.actor()])
            self.assertNotIn(secret, str(caught.exception))

    def test_saved_row_tampering_or_missing_hash_is_stale_not_current(self):
        saved = self.save([self.actor()])
        for changes in ({'name': '未经重新确认的改名'}, {'constraints': '新的身体结构'},
                        {'reference_sha256': ''}, {'identity_sha256': ''}, {'identity_sha256': '0' * 64}):
            with self.subTest(changes=changes):
                self.record['project_cast'] = copy.deepcopy(saved)
                self.record['project_cast']['characters'][0].update(changes)
                row = self.view()['characters'][0]
                self.assertEqual(row['status'], 'stale')
                self.assertEqual(row['identity_sha256'], '')

    def test_view_is_independent_and_omits_all_asset_filesystem_paths(self):
        saved = self.save([self.actor()])
        before = copy.deepcopy(saved)
        self.record['project_cast']['characters'][0]['file'] = 'caller-controlled/private.png'
        view = self.view()
        text = json.dumps(view, ensure_ascii=False)
        self.assertNotIn('caller-controlled', text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn('actor-a.png', text)
        self.assertNotIn('file', view['characters'][0])
        view['characters'][0]['name'] = 'local edit'
        self.assertEqual(self.record['project_cast']['characters'][0]['name'], before['characters'][0]['name'])


if __name__ == '__main__':
    unittest.main()
