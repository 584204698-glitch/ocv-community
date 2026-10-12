"""Behavioral checks for persisted stories, without model or media calls.

The API is the main assertion boundary. Only starting/running the background
language task is controlled, so navigation and retry cases remain deterministic.
"""
import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app import story_workspace as stories


ORIGINAL_STORY = '米粒每天替森林里的邻居修伞。这天，她发现门外的小白兔一直抱着一把没有破损的旧伞，原来它只是想找一个一起等雨停的朋友。'
NEW_STORY = '米粒关上修伞铺，陪白耳走过雨后的石桥。两个人把旧伞倒过来，替桥下落水的小刺猬做成了一只小船。'


def sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def model_result(story=NEW_STORY, title='雨后的石桥'):
    return {
        'title': title, 'story': story, 'synopsis': '两个新朋友用旧伞救起小刺猬。',
        'characters': [
            {'character_id': 'mili', 'name': '米粒', 'role': '主角', 'description': '经营修伞铺，乐于帮助邻居。'},
            {'character_id': 'baier', 'name': '白耳', 'role': '同伴', 'description': '一只珍惜旧伞的小白兔。'},
        ],
        'scenes': [{'scene_id': 'bridge', 'title': '雨后石桥', 'summary': '两人用倒置的旧伞救助落水的小刺猬。'}],
        'story_sha256': sha256(story), 'derived_from_story_sha256': sha256(story),
        'character_identity_status': 'suggested',
    }


class StoryWorkspaceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = 'a' * 32
        self.base = '/api/stories'
        self.url = self.base + '/' + self.identity

        def test_user(request):
            if request.headers.get('x-test-owner') == 'anonymous':
                raise HTTPException(401, '需要登录')
            return {'id': int(request.headers.get('x-test-owner', '1'))}

        for name, value in [('ROOT', self.root), ('ACTIVE', set()), ('require_user', test_user)]:
            handle = patch.object(stories, name, value)
            handle.start()
            self.addCleanup(handle.stop)
        starter = patch.object(stories, '_start_generation')
        self.start = starter.start()
        self.addCleanup(starter.stop)
        generator = patch.object(stories, '_generate_story', side_effect=AssertionError('A test attempted an unmocked language call'))
        self.model = generator.start()
        self.addCleanup(generator.stop)
        app = FastAPI()
        app.include_router(stories.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    @staticmethod
    def payload(**changes):
        return {'id': 'a' * 32, 'intent': 'manuscript', 'idea': '', 'preset': 'general',
                'duration_seconds': 60, 'title': '等雨停的朋友', 'story': ORIGINAL_STORY, **changes}

    def ok(self, response):
        self.assertIn(response.status_code, (200, 201, 202), response.text)
        return response.json()

    def create(self, **changes):
        return self.ok(self.client.post(self.base, json=self.payload(**changes)))

    def get(self, *, owner=1, identity=None):
        return self.ok(self.client.get(self.base + '/' + (identity or self.identity), headers={'x-test-owner': str(owner)}))

    def update(self, record=None, **changes):
        record = record or self.get()
        payload = {key: value for key, value in self.payload().items() if key != 'id'}
        payload.update({key: record['brief'][key] for key in ('intent', 'idea', 'preset', 'duration_seconds')})
        payload.update(title=record['title'], story=record['story'], revision=record['revision'])
        payload.update(changes)
        return self.client.put(self.url, json=payload)

    def queue(self, *, request_id='1' * 32, revision=None):
        if revision is None:
            revision = self.get()['revision']
        return self.client.post(self.url + '/generate', json={'revision': revision, 'request_id': request_id})

    def complete(self, *, request_id='1' * 32, result=None, error=None):
        self.model.side_effect = error
        self.model.return_value = model_result() if result is None else result
        stories._run_generation(1, self.identity, request_id)
        return self.get()

    def generated(self):
        self.create(intent='idea', idea='两个朋友用一把旧伞帮助邻居。')
        self.ok(self.queue())
        return self.complete()

    def disk_text(self):
        return '\n'.join(path.read_text(encoding='utf-8') for path in self.root.rglob('*.json'))

    def test_presets_are_listed_separately_from_owned_drafts(self):
        presets = self.ok(self.client.get(self.base + '/presets'))
        rows = presets.get('presets', presets.get('items', []))
        self.assertIn('general', [row['id'] for row in rows])
        self.assertEqual(self.ok(self.client.get(self.base))['stories'], [])
        created = self.create()
        self.assertEqual([row['id'] for row in self.ok(self.client.get(self.base))['stories']], [created['id']])

    def test_pasted_manuscript_and_manual_edits_preserve_outer_and_paragraph_whitespace(self):
        original = '\n  ' + ORIGINAL_STORY + '\n\n第二段保持作者自己的分行。  \n'
        record = self.create(story=original)
        self.assertEqual(record['story'], original)
        updated = original + '\n末尾新增一句，并且保留这个换行。\n'
        record = self.ok(self.update(record, story=updated))
        self.assertEqual(record['story'], updated)
        adopted = self.ok(self.client.post(self.url + '/adopt', json={'revision': record['revision']}))
        self.assertEqual(adopted['story'], updated)
        self.assertEqual(stories.resolve_story_source(1, adopted['source'], updated)['story'], updated)
        self.assertEqual(record['versions'][0]['story'], original)

    def test_manuscript_survives_new_client_and_has_exact_content_identity(self):
        created = self.create()
        self.assertEqual(created['story'], ORIGINAL_STORY)
        self.assertEqual(created['story_sha256'], sha256(ORIGINAL_STORY))
        self.assertGreaterEqual(created['story_revision'], 1)
        replacement = TestClient(self.client.app)
        try:
            recovered = self.ok(replacement.get(self.url))
        finally:
            replacement.close()
        self.assertEqual(recovered, created)
        self.start.assert_not_called()
        self.model.assert_not_called()

    def test_create_retry_is_idempotent_and_different_payload_is_a_conflict(self):
        created = self.create()
        self.assertEqual(self.create(), created)
        response = self.client.post(self.base, json=self.payload(story=NEW_STORY))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.get(), created)
        self.assertEqual(len(self.ok(self.client.get(self.base))['stories']), 1)

    def test_every_story_operation_is_scoped_to_authenticated_owner(self):
        original = self.create()
        foreign = {'x-test-owner': '2'}
        operations = [
            ('GET', self.url, None),
            ('PUT', self.url, {**{k: v for k, v in self.payload().items() if k != 'id'}, 'revision': original['revision']}),
            ('POST', self.url + '/generate', {'revision': original['revision'], 'request_id': '1' * 32}),
            ('POST', self.url + '/adopt', {'revision': original['revision']}),
        ]
        for method, url, body in operations:
            with self.subTest(method=method, url=url):
                response = self.client.request(method, url, json=body, headers=foreign)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertNotIn(ORIGINAL_STORY, response.text)
        self.assertEqual(self.ok(self.client.get(self.base, headers=foreign))['stories'], [])
        # The same public identifier in another account names a different draft.
        other = self.ok(self.client.post(self.base, json=self.payload(title='第二个账户', story=NEW_STORY), headers=foreign))
        self.assertEqual(self.get(owner=2)['story'], other['story'])
        self.assertEqual(self.get(), original)
        self.start.assert_not_called()

    def test_unauthenticated_requests_do_not_create_or_reveal_stories(self):
        self.create()
        anonymous = {'x-test-owner': 'anonymous'}
        for method, url, body in [
            ('GET', self.base, None), ('GET', self.url, None),
            ('POST', self.base, self.payload(id='b' * 32)),
            ('POST', self.url + '/adopt', {'revision': 1}),
            ('POST', self.url + '/generate', {'revision': 1, 'request_id': '1' * 32}),
        ]:
            with self.subTest(method=method, url=url):
                response = self.client.request(method, url, json=body, headers=anonymous)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(ORIGINAL_STORY, response.text)
        self.assertEqual(len(self.ok(self.client.get(self.base))['stories']), 1)
        self.start.assert_not_called()

    def test_invalid_identity_or_enums_do_not_write_a_draft(self):
        for changes in [
            {'id': '../outside'}, {'id': '..\\outside'}, {'id': 'z' * 32},
            {'intent': 'unknown'}, {'preset': 'unknown'}, {'duration_seconds': -1},
        ]:
            with self.subTest(changes=changes):
                response = self.client.post(self.base, json=self.payload(**changes))
                self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.ok(self.client.get(self.base))['stories'], [])
        self.assertEqual(list(self.root.rglob('*.json')), [])

    def test_stale_revision_cannot_save_generate_or_adopt(self):
        original = self.create(intent='idea', idea='两位朋友帮助邻居。')
        edited = self.ok(self.update(original, story=NEW_STORY))
        self.assertGreater(edited['revision'], original['revision'])
        stale_payload = {key: value for key, value in self.payload().items() if key != 'id'}
        for method, suffix, body in [
            ('PUT', '', {**stale_payload, 'revision': original['revision']}),
            ('POST', '/generate', {'revision': original['revision'], 'request_id': '1' * 32}),
            ('POST', '/adopt', {'revision': original['revision']}),
        ]:
            with self.subTest(suffix=suffix):
                response = self.client.request(method, self.url + suffix, json=body)
                self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.get(), edited)
        self.start.assert_not_called()

    def test_concurrent_retries_start_only_one_language_task(self):
        created = self.create(intent='idea', idea='米粒和白耳在雨中成为朋友。')
        payload = {'revision': created['revision'], 'request_id': '1' * 32}
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.client.post(self.url + '/generate', json=payload), range(4)))
        for response in responses:
            self.ok(response)
        self.start.assert_called_once_with(1, self.identity, '1' * 32)
        self.model.assert_not_called()
        self.assertEqual(self.get()['story'], ORIGINAL_STORY)

    def test_running_task_survives_navigation_and_cannot_be_replaced_by_an_edit(self):
        created = self.create(intent='idea', idea='米粒和白耳修好一把伞。')
        running = self.ok(self.queue())
        self.assertEqual(running['task']['request_id'], '1' * 32)
        self.assertEqual(self.get()['task'], running['task'])
        conflict = self.update(running, story='用户另写的正文不会在后台生成期间悄悄覆盖正在使用的来源内容。')
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(self.queue(request_id='2' * 32).status_code, 409)
        done = self.complete()
        self.assertEqual(done['story'], NEW_STORY)
        self.assertEqual(done['story_sha256'], sha256(NEW_STORY))
        self.assertGreater(done['story_revision'], created['story_revision'])
        self.assertEqual(done['derived_status'], 'current')
        self.assertEqual(self.get(), done)
        self.start.assert_called_once()

    def test_old_request_ids_do_not_generate_again_after_success_or_later_edit(self):
        created = self.create(intent='idea', idea='朋友们用一把伞展开冒险。')
        self.ok(self.queue())
        finished = self.complete()
        replay = self.ok(self.queue(revision=created['revision']))
        self.assertEqual(replay, finished)
        edited = self.ok(self.update(finished, title='手动修改的标题'))
        replay = self.ok(self.queue(revision=created['revision']))
        self.assertEqual(replay, edited)
        self.start.assert_called_once()
        self.model.assert_called_once()

    def test_old_worker_cannot_run_or_overwrite_a_newer_task(self):
        self.create(intent='idea', idea='朋友们修伞。')
        self.ok(self.queue())
        self.complete()
        self.ok(self.queue(request_id='2' * 32))
        running = self.get()
        self.model.reset_mock()
        stories._run_generation(1, self.identity, '1' * 32)
        self.assertEqual(self.get(), running)
        self.model.assert_not_called()
        done = self.complete(request_id='2' * 32, result=model_result(title='第二次生成'))
        self.assertEqual(done['title'], '第二次生成')
        self.assertEqual(self.ok(self.queue(request_id='1' * 32)), done)
        self.assertEqual(self.start.call_count, 2)

    def test_failure_keeps_good_story_and_never_returns_or_persists_provider_secrets(self):
        good = self.generated()
        self.ok(self.queue(request_id='2' * 32))
        secret = 'TEST-SECRET-fake-key-for-redaction-83'
        detail = f'POST https://vendor.invalid/generate?api_key={secret}; Authorization: Bearer {secret}; internal stack'
        failed = self.complete(request_id='2' * 32, error=RuntimeError(detail))
        for key in ('title', 'story', 'story_revision', 'story_sha256', 'versions', 'derived_status'):
            self.assertEqual(failed[key], good[key], key)
        self.assertNotEqual(failed['task']['status'], 'running')
        self.assertNotIn(secret, json.dumps(failed, ensure_ascii=False))
        self.assertNotIn('vendor.invalid', json.dumps(failed, ensure_ascii=False))
        self.assertNotIn(secret, self.disk_text())
        self.assertEqual(self.ok(self.queue(request_id='2' * 32)), failed)
        self.assertEqual(self.start.call_count, 2)

    def test_blank_generated_result_is_a_failure_and_preserves_the_previous_draft(self):
        before = self.create(intent='idea', idea='米粒和白耳开始冒险。')
        self.ok(self.queue())
        invalid = model_result(story='   ')
        after = self.complete(result=invalid)
        self.assertEqual(after['story'], before['story'])
        self.assertEqual(after['story_sha256'], before['story_sha256'])
        self.assertEqual(after['story_revision'], before['story_revision'])
        self.assertEqual(after['versions'], before['versions'])
        self.assertNotEqual(after['task']['status'], 'running')

    def test_body_edits_invalidate_derived_data_but_title_edits_keep_its_source_identity(self):
        generated = self.generated()
        renamed = self.ok(self.update(generated, title='用户取的新标题'))
        self.assertEqual(renamed['story_sha256'], generated['story_sha256'])
        self.assertEqual(renamed['story_revision'], generated['story_revision'])
        self.assertEqual(renamed['derived_status'], 'current')
        body = NEW_STORY + '\n小刺猬平安回家后，把一片叶子送给了两位朋友。'
        rewritten = self.ok(self.update(renamed, story=body))
        self.assertEqual(rewritten['story'], body)
        self.assertEqual(rewritten['story_sha256'], sha256(body))
        self.assertEqual(rewritten['story_revision'], generated['story_revision'] + 1)
        self.assertEqual(rewritten['derived_status'], 'stale')
        self.assertEqual(rewritten['derived_from_story_sha256'], generated['story_sha256'])
        self.assertNotEqual(rewritten['derived_from_story_sha256'], rewritten['story_sha256'])
        self.assertEqual(self.get()['derived_status'], 'stale')

    def test_noop_save_does_not_create_a_new_content_version(self):
        current = self.generated()
        saved = self.ok(self.update(current))
        self.assertEqual(saved['story_revision'], current['story_revision'])
        self.assertEqual(saved['story_sha256'], current['story_sha256'])
        self.assertEqual(saved['versions'], current['versions'])
        self.assertEqual(saved['derived_status'], 'current')

    def test_process_restart_marks_incomplete_task_interrupted_without_retry(self):
        self.create(intent='idea', idea='下雨时两个邻居相遇。')
        self.ok(self.queue())
        running = self.get()
        with patch.object(stories, 'PROCESS_ID', 'different-process-for-test'):
            recovered = self.get()
        self.assertNotEqual(recovered['task']['status'], 'running')
        self.assertIn('interrupted', (recovered.get('status'), recovered['task']['status']))
        self.assertEqual(recovered['story'], running['story'])
        self.assertEqual(recovered['story_sha256'], running['story_sha256'])
        self.assertEqual(recovered['versions'], running['versions'])
        self.start.assert_called_once()
        self.model.assert_not_called()

    def test_adoption_returns_exact_text_snapshot_without_starting_generation(self):
        created = self.create()
        adopted = self.ok(self.client.post(self.url + '/adopt', json={'revision': created['revision']}))
        self.assertEqual(adopted['story'], ORIGINAL_STORY)
        self.assertEqual(adopted['title'], created['title'])
        self.assertEqual(adopted['content_mode'], 'general')
        self.assertRegex(adopted['source']['adoption_id'], r'^[a-f0-9]{32}$')
        self.assertEqual({key: adopted['source'][key] for key in ('story_id', 'story_revision', 'story_sha256')}, {'story_id': self.identity,
                                           'story_revision': created['story_revision'],
                                           'story_sha256': sha256(ORIGINAL_STORY)})
        current = self.get()
        self.assertEqual(current['story'], ORIGINAL_STORY)
        self.assertEqual(current['story_revision'], created['story_revision'])
        self.assertEqual(current['story_sha256'], created['story_sha256'])
        self.assertEqual(current['versions'], created['versions'])
        for forbidden in ('audio_task_id', 'video_task_id', 'shots', 'images', 'video'):
            self.assertNotIn(forbidden, current)
        self.start.assert_not_called()
        self.model.assert_not_called()

    def test_blank_draft_is_saveable_but_cannot_be_adopted(self):
        blank = self.create(intent='topics', title='', story='')
        response = self.client.post(self.url + '/adopt', json={'revision': blank['revision']})
        self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.get()['story'], '')
        self.start.assert_not_called()

    def test_task_start_failure_preserves_the_draft_and_frees_a_retry_slot(self):
        current = self.create(intent='idea', idea='米粒和白耳在雨天相遇。')
        secret = 'TEST-SECRET-fake-start-failure-92'
        self.start.side_effect = RuntimeError(secret)
        failed = self.ok(self.queue())
        self.assertEqual(failed['task']['status'], 'failed')
        for key in ('story', 'story_revision', 'story_sha256', 'versions'):
            self.assertEqual(failed[key], current[key], key)
        self.assertNotIn(secret, json.dumps(failed, ensure_ascii=False))
        self.assertNotIn(secret, self.disk_text())
        self.start.side_effect = None
        retry = self.ok(self.queue(request_id='2' * 32))
        self.assertEqual(retry['task']['status'], 'running')
        self.assertEqual(self.start.call_count, 2)

    def test_running_task_blocks_adoption_and_model_receives_the_saved_brief(self):
        saved = self.create(intent='idea', idea='一只小兔想结交修伞的邻居。', preset='warm_relationship', duration_seconds=90)
        running = self.ok(self.queue())
        adoption = self.client.post(self.url + '/adopt', json={'revision': running['revision']})
        self.assertEqual(adoption.status_code, 409, adoption.text)
        self.complete()
        self.model.assert_called_once_with(saved['brief'])

    def test_parallel_accounts_keep_background_results_in_their_own_drafts(self):
        self.create(intent='idea', idea='米粒和白耳修伞。')
        foreign = {'x-test-owner': '2'}
        other = self.ok(self.client.post(self.base, headers=foreign,
                                        json=self.payload(intent='idea', idea='一只小猫寻找丢失的铃铛。', title='另一账户的故事')))
        self.ok(self.queue())
        self.ok(self.client.post(self.url + '/generate', headers=foreign,
                                 json={'revision': other['revision'], 'request_id': '1' * 32}))
        self.assertEqual(self.start.call_count, 2)
        before_other = self.get(owner=2)
        completed = self.complete()
        self.assertEqual(completed['story'], NEW_STORY)
        self.assertEqual(self.get(owner=2), before_other)
        self.assertEqual(before_other['task']['status'], 'running')

    def test_adopted_historical_body_stays_resolvable_after_a_later_body_edit(self):
        original = self.create()
        adopted = self.ok(self.client.post(self.url + '/adopt', json={'revision': original['revision']}))
        newer = self.ok(self.update(adopted['record'], story=NEW_STORY))
        resolved = stories.resolve_story_source(1, adopted['source'], ORIGINAL_STORY)
        self.assertEqual(resolved['story'], ORIGINAL_STORY)
        self.assertEqual(resolved['story_sha256'], sha256(ORIGINAL_STORY))
        self.assertEqual(resolved['story_revision'], original['story_revision'])
        self.assertEqual(self.get(), newer)
        self.start.assert_not_called()
        self.model.assert_not_called()

    def test_source_resolution_rejects_foreign_unadopted_or_changed_content(self):
        original = self.create()
        source = {'story_id': self.identity, 'story_revision': original['story_revision'],
                  'story_sha256': original['story_sha256']}
        with self.assertRaises(HTTPException) as error:
            stories.resolve_story_source(1, source, ORIGINAL_STORY)
        self.assertEqual(error.exception.status_code, 409)
        self.ok(self.client.post(self.url + '/adopt', json={'revision': original['revision']}))
        for owner, candidate, body, status in [
            (2, source, ORIGINAL_STORY, 404),
            (1, source, NEW_STORY, 409),
            (1, {**source, 'story_sha256': sha256(NEW_STORY)}, NEW_STORY, 409),
            (1, {**source, 'story_revision': original['story_revision'] + 1}, ORIGINAL_STORY, 409),
        ]:
            with self.subTest(owner=owner, status=status, source=candidate):
                with self.assertRaises(HTTPException) as error:
                    stories.resolve_story_source(owner, candidate, body)
                self.assertEqual(error.exception.status_code, status)
        self.start.assert_not_called()
        self.model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
