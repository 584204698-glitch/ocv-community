"""Image protocol snapshots must never redirect the current credential to an old host."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app import image_profiles as profiles


class OpenAIImageProfilesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = self.root / '.env'
        self.documents = self.root / 'image_profiles.json'
        self.set_env(CUSTOM_LLM_API_BASE='https://relay.example.test/v1',
                     CUSTOM_LLM_API_KEY='fake-shared-credential')

        def save_env(values):
            current = profiles._parse_env_lines(self.env)
            self.set_env(**{**current, **values})

        self.save_env = Mock(side_effect=save_env)
        for name, value in [('ENV_PATH', self.env), ('PROFILE_PATH', self.documents),
                            ('require_user', lambda request: {'id': 1}),
                            ('save_project_env_values', self.save_env)]:
            handle = patch.object(profiles, name, value)
            handle.start()
            self.addCleanup(handle.stop)
        app = FastAPI()
        app.include_router(profiles.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def set_env(self, **values):
        self.env.write_text(''.join(f'{key}={value}\n' for key, value in values.items()), encoding='utf-8')

    def save(self, **changes):
        payload = {'id': 'native-a', 'name': 'Native image fixture', 'protocol': 'openai_images',
                   'credential_source': 'profile', 'base_url': 'https://images.example.test/v1',
                   'model_id': 'image-original', **changes}
        response = self.client.put('/api/image-profiles', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['profile']

    def shared(self, **changes):
        return self.save(credential_source='language_custom', base_url='', **changes)

    def snapshot(self):
        return profiles.profile_snapshot('native-a')

    def rewrite_document(self, **changes):
        payload = json.loads(self.documents.read_text(encoding='utf-8'))
        payload['profiles'][0].update(changes)
        self.documents.write_text(json.dumps(payload), encoding='utf-8')

    def test_native_schema_has_fixed_paths_single_resolution_and_literal_protocol(self):
        payload = profiles.ImageProfileRequest(name='Native', protocol='openai_images',
            base_url='https://images.example.test/v1', model_id='image-v1')
        self.assertEqual(payload.resolutions, ['1k'])
        self.assertEqual(payload.text_endpoint, '/images/generations')
        self.assertEqual(payload.reference_endpoint, '/images/edits')
        self.assertEqual(payload.query_endpoint, '')
        self.assertEqual(payload.credential_source, 'profile')
        for changes in ({'protocol': 'guessed-protocol'}, {'credential_source': 'unknown'},
                        {'resolutions': ['2k']}, {'resolutions': ['4k']}, {'resolutions': ['1k', '2k']},
                        {'resolutions': []}, {'text_endpoint': 'https://other.example/images/generations'},
                        {'reference_endpoint': '//other.example/images/edits'}, {'query_endpoint': '/tasks'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                profiles.ImageProfileRequest(**{'name': 'Native', 'protocol': 'openai_images',
                    'base_url': 'https://images.example.test/v1', 'model_id': 'image-v1', **changes})

    def test_async_defaults_and_credential_source_are_unchanged(self):
        payload = profiles.ImageProfileRequest(name='Async', base_url='https://async.example.test', model_id='v2')
        self.assertEqual(payload.protocol, 'async_task')
        self.assertEqual(payload.credential_source, 'profile')
        self.assertEqual(payload.resolutions, ['1k', '2k', '4k'])
        self.assertEqual(payload.query_endpoint, '/openapi/v2/query')
        with self.assertRaises(ValidationError):
            profiles.ImageProfileRequest(name='Async', model_id='v2', credential_source='language_custom')
        with self.assertRaises(ValidationError):
            profiles.ImageProfileRequest(name='Async', model_id='v2', base_url='')

    def test_profile_owned_key_keeps_existing_environment_name_and_no_document_secret(self):
        saved = self.save(api_key='fake-profile-key')
        self.assertTrue(saved['configured'])
        self.assertEqual(saved['key_count'], 1)
        self.assertEqual(profiles._parse_env_lines(self.env)['OCV_IMAGE_PROFILE_KEYS_NATIVE_A'], 'fake-profile-key')
        self.assertNotIn('fake-profile-key', self.documents.read_text(encoding='utf-8'))
        self.assertNotIn('api_keys', saved)
        config = profiles.profile_provider_configs(self.snapshot())[0]
        self.assertEqual(config['api_key'], 'fake-profile-key')
        self.assertEqual(config['base_url'], 'https://images.example.test/v1')

    def test_shared_key_is_resolved_without_copying_or_returning_hints(self):
        saved = self.shared()
        self.assertEqual(saved['base_url'], 'https://relay.example.test/v1')
        self.assertTrue(saved['configured'])
        self.assertEqual(saved['key_count'], 1)
        self.assertNotIn('key_hints', saved)
        self.assertNotIn('api_keys', saved)
        self.save_env.assert_not_called()
        self.assertNotIn('OCV_IMAGE_PROFILE_KEYS_NATIVE_A', profiles._parse_env_lines(self.env))
        for public in (saved, self.client.get('/api/image-profiles').json(), self.snapshot()):
            self.assertNotIn('fake-shared-credential', json.dumps(public))
        self.assertNotIn('fake-shared-credential', self.documents.read_text(encoding='utf-8'))
        self.assertEqual(profiles.profile_provider_configs(self.snapshot())[0]['api_key'], 'fake-shared-credential')

    def test_shared_profile_cannot_supply_a_key_or_an_unrelated_base(self):
        for changes in ({'api_key': 'unwanted-copy'}, {'api_keys': ['unwanted-copy']},
                        {'base_url': 'https://other.example.test/v1'},
                        {'base_url': 'https://relay.example.test/another-prefix'}):
            with self.subTest(changes=changes):
                response = self.client.put('/api/image-profiles', json={
                    'name': 'Shared', 'protocol': 'openai_images', 'credential_source': 'language_custom',
                    'base_url': '', 'model_id': 'image-v1', **changes})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertFalse(self.documents.exists())
        self.save_env.assert_not_called()

    def test_shared_base_change_disables_profile_even_when_origin_is_unchanged(self):
        self.shared()
        snapshot = self.snapshot()
        for base in ('https://relay.example.test/v2', 'https://other.example.test/v1',
                     'http://relay.example.test/v1', 'https://relay.example.test:444/v1'):
            with self.subTest(base=base):
                self.set_env(CUSTOM_LLM_API_BASE=base, CUSTOM_LLM_API_KEY='fake-new-shared-key')
                listed = profiles.list_profiles()[0]
                self.assertFalse(listed['configured'])
                self.assertEqual(listed['key_count'], 0)
                self.assertNotIn('key_hints', listed)
                self.assertIn('重新保存', listed['configuration_error'])
                with self.assertRaisesRegex(ValueError, '重新保存'):
                    profiles.profile_provider_configs(snapshot)

    def test_resaving_shared_base_does_not_make_an_old_task_use_new_credentials(self):
        self.shared()
        previous = self.snapshot()
        self.set_env(CUSTOM_LLM_API_BASE='https://new-relay.example.test/v1', CUSTOM_LLM_API_KEY='fake-new-key')
        saved = self.shared()
        self.assertTrue(saved['configured'])
        self.assertEqual(saved['base_url'], 'https://new-relay.example.test/v1')
        with self.assertRaisesRegex(ValueError, '地址已变化'):
            profiles.profile_provider_configs(previous)
        current = profiles.profile_provider_configs(self.snapshot())[0]
        self.assertEqual(current['base_url'], 'https://new-relay.example.test/v1')
        self.assertEqual(current['api_key'], 'fake-new-key')

    def test_shared_key_rotation_on_the_same_base_preserves_task_model(self):
        self.shared()
        snapshot = self.snapshot()
        self.set_env(CUSTOM_LLM_API_BASE='https://relay.example.test/v1/', CUSTOM_LLM_API_KEY='fake-rotated-key')
        self.shared(model_id='image-later')
        config = profiles.profile_provider_configs(snapshot)[0]
        self.assertEqual(config['api_key'], 'fake-rotated-key')
        self.assertEqual(config['model'], 'image-original')
        self.assertEqual(config['resolution'], '1k')

    def test_shared_missing_key_is_unconfigured_without_falling_back_to_profile_key(self):
        self.set_env(CUSTOM_LLM_API_BASE='https://relay.example.test/v1', CUSTOM_LLM_API_KEY='',
                     OCV_IMAGE_PROFILE_KEYS_NATIVE_A='fake-own-key-that-must-not-be-used')
        saved = self.shared()
        self.assertFalse(saved['configured'])
        self.assertEqual(saved['key_count'], 0)
        with self.assertRaises(ValueError):
            self.snapshot()

    def test_snapshot_has_source_but_never_secret_hints_or_runtime_keys(self):
        self.shared()
        snapshot = self.snapshot()
        self.assertEqual(snapshot['credential_source'], 'language_custom')
        self.assertEqual(snapshot['protocol'], 'openai_images')
        for field in ('api_key', 'api_keys', 'key_hints', 'configured', 'key_count'):
            self.assertNotIn(field, snapshot)
        self.assertEqual(snapshot['base_url'], 'https://relay.example.test/v1')

    def test_native_transport_is_fixed_even_if_task_snapshot_has_injected_endpoints(self):
        self.save(api_key='fake-profile-key', reference_images=False)
        snapshot = self.snapshot()
        snapshot.update(text_endpoint='https://evil.example.test/steal', reference_endpoint='//evil.example/edits',
                        query_endpoint='https://evil.example/query', reference_images=True)
        config = profiles.profile_provider_configs(snapshot)[0]
        self.assertEqual(config['protocol'], 'openai_images')
        self.assertEqual(config['endpoint'], '/images/generations')
        self.assertEqual(config['reference_endpoint'], '/images/edits')
        self.assertEqual(config['query_url'], '')
        self.assertFalse(config['reference_images'])

    def test_native_reference_capability_requires_both_snapshot_and_current_profile(self):
        for old, current in ((False, True), (True, False)):
            with self.subTest(snapshot=old, current=current):
                self.save(api_key='fake-profile-key', reference_images=old)
                snapshot = self.snapshot()
                self.save(reference_images=current)
                config = profiles.profile_provider_configs(snapshot)[0]
                self.assertFalse(config['reference_images'])
                self.assertEqual(profiles.profile_environment(snapshot)['OCV_IMAGE_REFERENCE_IMAGES'], '0')

    def test_native_runtime_rejects_snapshot_base_protocol_source_or_resolution_changes(self):
        self.shared()
        snapshot = self.snapshot()
        cases = [{'base_url': 'https://other.example.test/v1'}, {'base_url': 'https://relay.example.test/v2'},
                 {'protocol': 'async_task'}, {'credential_source': 'profile'},
                 {'resolution': '2k'}, {'resolution': '4k'}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                profiles.profile_provider_configs({**snapshot, **changes})

    def test_native_environment_preserves_subprocess_model_base_key_and_resolution(self):
        self.shared(reference_images=False)
        environment = profiles.profile_environment(self.snapshot())
        self.assertEqual(environment['OCV_IMAGE_PROTOCOL'], 'openai_images')
        self.assertEqual(environment['OCV_IMAGE_REFERENCE_IMAGES'], '0')
        self.assertEqual(environment['IMAGE_API_BASE_URL'], 'https://relay.example.test/v1')
        self.assertEqual(environment['RUNNINGHUB_BASE_URL'], environment['IMAGE_API_BASE_URL'])
        self.assertEqual(environment['IMAGE_MODEL_ID'], 'image-original')
        self.assertEqual(environment['IMAGE_RESOLUTION'], '1k')
        self.assertEqual(environment['RUNNINGHUB_API_KEY'], 'fake-shared-credential')
        self.assertEqual(environment['RUNNINGHUB_ENDPOINT'], '/images/generations')
        self.assertEqual(environment['RUNNINGHUB_IMAGE_TO_IMAGE_ENDPOINT'], '/images/edits')
        self.assertEqual(environment['OCV_IMAGE_QUERY_URL'], '')

    def test_invalid_native_document_cannot_masquerade_as_configured(self):
        self.save(api_key='fake-profile-key')
        original = self.documents.read_bytes()
        for changes in ({'text_endpoint': 'https://other.example.test/images/generations'},
                        {'query_endpoint': '/query'}, {'resolutions': ['2k']}, {'protocol': 'unknown'}):
            with self.subTest(changes=changes):
                self.documents.write_bytes(original)
                self.rewrite_document(**changes)
                self.assertFalse(profiles.list_profiles()[0]['configured'])
                with self.assertRaises(ValueError):
                    self.snapshot()

    def test_legacy_snapshot_without_protocol_is_async_and_cannot_pick_up_native_key(self):
        self.save(protocol='async_task', api_key='fake-original-key')
        snapshot = self.snapshot()
        snapshot.pop('protocol')
        snapshot.pop('credential_source')
        self.assertEqual(profiles.profile_provider_configs(snapshot)[0]['protocol'], 'async_task')
        self.save(api_key='fake-native-key')
        with self.assertRaisesRegex(ValueError, '协议或凭据来源'):
            profiles.profile_provider_configs(snapshot)

    def test_changing_credential_source_invalidates_existing_task(self):
        self.save(base_url='https://relay.example.test/v1', api_key='fake-owned-key')
        snapshot = self.snapshot()
        self.shared()
        with self.assertRaisesRegex(ValueError, '协议或凭据来源'):
            profiles.profile_provider_configs(snapshot)

    def test_async_old_paths_reference_setting_and_model_survive_profile_edit(self):
        self.save(protocol='async_task', api_key='fake-async-key', resolutions=['2k'],
                  text_endpoint='/old/{model}/generate', reference_endpoint='/old/{model}/edit',
                  query_endpoint='/old/query', reference_images=False)
        snapshot = profiles.profile_snapshot('native-a', '2k')
        self.save(protocol='async_task', model_id='image-new', resolutions=['1k'],
                  text_endpoint='/new/{model}/generate', reference_endpoint='/new/{model}/edit',
                  query_endpoint='/new/query', reference_images=True)
        config = profiles.profile_provider_configs(snapshot)[0]
        self.assertEqual(config['model'], 'image-original')
        self.assertEqual(config['resolution'], '2k')
        self.assertEqual(config['endpoint'], 'https://images.example.test/v1/old/image-original/generate')
        self.assertEqual(config['reference_endpoint'], 'https://images.example.test/v1/old/image-original/edit')
        self.assertEqual(config['query_url'], 'https://images.example.test/v1/old/query')
        self.assertFalse(config['reference_images'])
        environment = profiles.profile_environment(snapshot)
        self.assertEqual(environment['OCV_IMAGE_PROTOCOL'], 'async_task')
        self.assertEqual(environment['OCV_IMAGE_REFERENCE_IMAGES'], '0')

    def test_async_snapshot_endpoint_cannot_redirect_current_key_to_another_origin(self):
        self.save(protocol='async_task', api_key='fake-async-key')
        snapshot = self.snapshot()
        for field in ('text_endpoint', 'reference_endpoint', 'query_endpoint'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                profiles.profile_provider_configs({**snapshot, field: 'https://other.example.test/collect'})

    def test_async_same_origin_absolute_snapshot_path_stays_usable(self):
        self.save(protocol='async_task', api_key='fake-async-key',
                  text_endpoint='https://task.example.test/original/{model}')
        snapshot = self.snapshot()
        self.save(protocol='async_task', text_endpoint='https://task.example.test/revised/{model}')
        config = profiles.profile_provider_configs(snapshot)[0]
        self.assertEqual(config['endpoint'], 'https://task.example.test/original/image-original')
        self.save(protocol='async_task', text_endpoint='https://new-task.example.test/revised/{model}')
        with self.assertRaises(ValueError):
            profiles.profile_provider_configs(snapshot)

    def test_async_relative_query_parameters_keep_existing_behavior(self):
        self.save(protocol='async_task', api_key='fake-async-key', query_endpoint='/query?view=full')
        config = profiles.profile_provider_configs(self.snapshot())[0]
        self.assertEqual(config['query_url'], 'https://images.example.test/v1/query?view=full')

    def test_legacy_flag_cannot_make_native_profile_inherit_runninghub_credentials(self):
        self.save()
        self.rewrite_document(legacy=True)
        self.set_env(RUNNINGHUB_API_KEY='fake-legacy-key')
        self.assertFalse(profiles.list_profiles()[0]['configured'])
        with self.assertRaises(ValueError):
            self.snapshot()

    def test_invalid_shared_current_base_is_rejected_before_document_or_key_write(self):
        for base in ('', 'ftp://relay.example/v1', 'https://user:password@relay.example/v1',
                     'https://relay.example/v1?key=secret', 'https://relay.example:bad/v1'):
            with self.subTest(base=base):
                self.set_env(CUSTOM_LLM_API_BASE=base, CUSTOM_LLM_API_KEY='fake-shared-key')
                response = self.client.put('/api/image-profiles', json={'name': 'Shared',
                    'protocol': 'openai_images', 'credential_source': 'language_custom', 'model_id': 'image-v1'})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertFalse(self.documents.exists())
        self.save_env.assert_not_called()


if __name__ == '__main__':
    unittest.main()
