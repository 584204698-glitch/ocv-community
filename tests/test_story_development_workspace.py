"""API invariants for staged writing and explicit, bounded rewrite proposals.

Runner aliases are controlled; no language service, browser, or media worker is
started. Assertions focus on persisted author content and observable API rules.
"""
import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app import story_workspace as stories
from tests.test_story_workspace import model_result
from backend.app.story_writer import story_text_sha256


PREFIX = '\n  🐾米粒来到桥边，发现雨水已经漫过最后一级石阶。\n'
SELECTION = '白耳把旧伞递给米粒，轻声说：“这次换我陪你走。”'
SUFFIX = '\n她们一起走向树下的修伞铺，把湿漉漉的鞋子放在门口。  \n'
BODY = PREFIX + SELECTION + SUFFIX
REPLACEMENT = '白耳把旧伞撑在两人头顶，笑着说：“这次让我送你回家。”'
REWRITTEN = PREFIX + REPLACEMENT + SUFFIX


def plan_result():
    return {'directions': [
        {'id': f'direction_{index}', 'title': f'桥边的朋友{index}',
         'premise': f'两个邻居在雨中相遇，方向{index}。', 'opening': '旧伞挂在门外。',
         'action': '米粒主动陪白耳过桥。', 'turn': '旧伞其实没有损坏。', 'ending': '两人成为朋友。'}
        for index in range(1, 4)
    ], 'selected_direction_id': 'direction_1', 'selection_reason': '用具体行动建立两位邻居的关系。'}


def ready_review():
    return {'verdict': 'ready', 'issues': []}


def issue_review(quote=SELECTION):
    return {'verdict': 'needs_revision', 'issues': [{'severity': 'minor', 'dimension': '行动动机',
            'quote': quote, 'reason': '递伞前缺少一个能看见的动作。', 'suggestion': '补充主动靠近的细节。'}]}


def developed_result():
    return {'draft': model_result(), 'plan': plan_result(), 'reviews': [ready_review()],
            'revision_rounds': 0, 'editorial_review': 'ready'}


def proposal_result():
    return {'story': REWRITTEN, 'replacement': REPLACEMENT, 'review': ready_review()}


class StoryDevelopmentWorkspaceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = 'd' * 32
        self.url = '/api/stories/' + self.identity

        def user(request):
            value = request.headers.get('x-test-owner', '1')
            if value == 'anonymous':
                raise HTTPException(401, '需要登录')
            return {'id': int(value)}

        for key, value in [('ROOT', self.root), ('ACTIVE', set()), ('require_user', user)]:
            handle = patch.object(stories, key, value)
            handle.start()
            self.addCleanup(handle.stop)
        starter = patch.object(stories, '_start_generation')
        self.start = starter.start()
        self.addCleanup(starter.stop)
        self.runners = {}
        for operation, name in [('directions', 'plan_story'), ('develop', 'develop_story'),
                                ('review', 'review_story'), ('rewrite', 'propose_rewrite'),
                                ('generate', '_generate_story')]:
            handle = patch.object(stories, name, side_effect=AssertionError('An unconfigured runner was called'), create=True)
            self.runners[operation] = handle.start()
            self.addCleanup(handle.stop)
        application = FastAPI()
        application.include_router(stories.router)
        self.client = TestClient(application)
        self.addCleanup(self.client.close)

    def ok(self, response):
        self.assertIn(response.status_code, (200, 201, 202), response.text)
        return response.json()

    def create(self, **changes):
        payload = {'id': self.identity, 'intent': 'idea', 'idea': '两个邻居在雨中通过一把旧伞成为朋友。',
                   'preset': 'general', 'duration_seconds': 60, 'title': '桥边的朋友', 'story': BODY, **changes}
        return self.ok(self.client.post('/api/stories', json=payload))

    def get(self):
        return self.ok(self.client.get(self.url))

    def update(self, record=None, **changes):
        record = record or self.get()
        payload = {key: record['brief'][key] for key in ('intent', 'idea', 'preset', 'duration_seconds')}
        payload.update(revision=record['revision'], title=record['title'], story=record['story'])
        payload.update(changes)
        return self.client.put(self.url, json=payload)

    def payload(self, operation, *, request_id='request-one', revision=None, **changes):
        payload = {'revision': self.get()['revision'] if revision is None else revision, 'request_id': request_id}
        if operation == 'rewrite':
            payload.update(start=len(PREFIX), end=len(PREFIX + SELECTION), instruction='让递伞的动作更主动，保留人物关系。')
        payload.update(changes)
        return payload

    def queue(self, operation, **changes):
        return self.client.post(self.url + '/' + operation, json=self.payload(operation, **changes))

    def complete(self, operation, result=None, *, request_id='request-one', effect=None):
        defaults = {'directions': plan_result, 'develop': developed_result, 'review': ready_review,
                    'rewrite': proposal_result, 'generate': model_result}
        self.runners[operation].side_effect = effect
        self.runners[operation].return_value = defaults[operation]() if result is None else result
        stories._run_generation(1, self.identity, request_id)
        return self.get()

    def proposal(self):
        original = self.create()
        self.ok(self.queue('rewrite'))
        proposed = self.complete('rewrite')
        return original, proposed

    def action(self, operation, record, proposal_id=None, **changes):
        payload = {'revision': record['revision'],
                   'proposal_id': proposal_id or record['rewrite_proposal']['id'], **changes}
        return self.client.post(self.url + '/rewrite/' + operation, json=payload)

    def assert_body_unchanged(self, current, original):
        for key in ('title', 'story', 'story_revision', 'story_sha256', 'versions'):
            self.assertEqual(current[key], original[key], key)

    def test_all_new_operations_are_authenticated_and_owner_scoped(self):
        original = self.create()
        for owner, status in [('2', 404), ('anonymous', 401)]:
            for operation in ('directions', 'develop', 'review', 'rewrite'):
                with self.subTest(owner=owner, operation=operation):
                    response = self.client.post(self.url + '/' + operation, headers={'x-test-owner': owner},
                                                json=self.payload(operation))
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertNotIn(BODY, response.text)
            for action in ('accept', 'discard'):
                with self.subTest(owner=owner, action=action):
                    response = self.client.post(self.url + '/rewrite/' + action, headers={'x-test-owner': owner},
                                                json={'revision': original['revision'], 'proposal_id': 'foreign-proposal'})
                    self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(self.get(), original)
        self.start.assert_not_called()

    def test_direct_picturebook_result_is_saved_with_versions_and_no_fake_directions(self):
        original = self.create(preset='picturebook')
        self.ok(self.queue('develop'))
        result = developed_result()
        result['plan'] = None
        completed = self.complete('develop', result)
        self.assertEqual(completed['task']['status'], 'completed')
        self.assertIsNone(completed['development']['plan'])
        self.assertEqual(completed['story'], result['draft']['story'])
        self.assertTrue(any(v['story'] == original['story'] for v in completed['versions']))

    def test_missing_required_plan_fails_without_replacing_body(self):
        original = self.create()
        self.ok(self.queue('develop'))
        result = developed_result()
        result['plan'] = None
        completed = self.complete('develop', result)
        self.assertEqual(completed['task']['status'], 'failed')
        self.assert_body_unchanged(completed, original)

    def test_rejected_directions_show_editorial_message_and_keep_original(self):
        original = self.create()
        self.ok(self.queue('directions'))
        completed = self.complete('directions', effect=stories.StoryDirectionsRejected())
        self.assertEqual(completed['task']['status'], 'failed')
        self.assertIn('暂未选出可用稿', completed['task']['error'])
        self.assertNotIn('格式', completed['task']['error'])
        self.assert_body_unchanged(completed, original)

    def test_stale_revision_cannot_start_any_development_operation(self):
        original = self.create()
        current = self.ok(self.update(original, title='新的标题'))
        for operation in ('directions', 'develop', 'review', 'rewrite'):
            with self.subTest(operation=operation):
                self.assertEqual(self.queue(operation, revision=original['revision']).status_code, 409)
        self.assertEqual(self.get(), current)
        self.start.assert_not_called()

    def test_identical_concurrent_retries_start_one_task_and_replay_after_completion(self):
        original = self.create()
        payload = self.payload('directions', revision=original['revision'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.client.post(self.url + '/directions', json=payload), range(4)))
        for response in responses:
            self.ok(response)
        self.start.assert_called_once_with(1, self.identity, 'request-one')
        completed = self.complete('directions')
        # Revision changes are a retry detail, not a different writing request.
        self.assertEqual(self.ok(self.queue('directions')), completed)
        self.assertEqual(self.ok(self.queue('directions', revision=original['revision'])), completed)
        self.start.assert_called_once()

    def test_request_id_cannot_be_reused_across_operations_after_the_first_task_finishes(self):
        self.create()
        self.ok(self.queue('directions'))
        current = self.complete('directions')
        for operation in ('develop', 'review', 'rewrite', 'generate'):
            with self.subTest(operation=operation):
                self.assertEqual(self.queue(operation).status_code, 409)
        self.assertEqual(self.get(), current)
        self.start.assert_called_once()

    def test_rewrite_request_id_binds_range_and_instruction(self):
        _, current = self.proposal()
        for changes in ({'start': len(PREFIX) + 1}, {'end': len(PREFIX + SELECTION) - 1},
                        {'instruction': '让语气更克制。'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.queue('rewrite', **changes).status_code, 409)
        self.assertEqual(self.get(), current)
        self.start.assert_called_once()

    def test_legacy_generate_request_ids_share_the_namespace_with_new_operations(self):
        self.create()
        record = stories._load(1, self.identity)
        record['generation_requests'] = ['legacy-request']
        stories._write(1, record)
        current = self.get()
        self.assertEqual(self.ok(self.queue('generate', request_id='legacy-request')), current)
        for operation in ('directions', 'develop', 'review', 'rewrite'):
            with self.subTest(operation=operation):
                self.assertEqual(self.queue(operation, request_id='legacy-request').status_code, 409)
        self.start.assert_not_called()

    def test_directions_only_save_a_plan_and_never_replace_author_text(self):
        original = self.create()
        running = self.ok(self.queue('directions'))
        self.assertEqual(running['task']['operation'], 'directions')
        self.assertEqual(running['task']['status'], 'running')
        self.assert_body_unchanged(running, original)
        completed = self.complete('directions')
        self.assert_body_unchanged(completed, original)
        self.assertEqual(completed['development']['plan'], plan_result())
        self.assertEqual(completed['task']['status'], 'completed')
        self.assertEqual(self.get(), completed)

    def test_selected_direction_is_passed_to_development_and_business_payload_is_bound(self):
        original = self.create()
        self.ok(self.queue('directions'))
        self.complete('directions')
        self.ok(self.queue('develop', request_id='request-two', direction_id='direction_2'))
        result = developed_result()
        result['plan']['selected_direction_id'] = 'direction_2'
        completed = self.complete('develop', result, request_id='request-two')
        call = self.runners['develop'].call_args
        self.assertEqual(call.args[0], original['brief'])
        self.assertEqual(call.kwargs['direction_id'], 'direction_2')
        self.assertEqual(call.kwargs['plan'], plan_result())
        self.assertEqual(completed['story'], model_result()['story'])
        self.assertEqual(completed['story_revision'], original['story_revision'] + 1)
        self.assertEqual(self.queue('develop', request_id='request-two', direction_id='direction_3').status_code, 409)
        self.assertEqual(self.get(), completed)

    def test_complete_development_replaces_text_once_and_keeps_the_old_version(self):
        original = self.create()
        self.ok(self.queue('develop'))
        completed = self.complete('develop')
        self.assertEqual(completed['story'], model_result()['story'])
        self.assertEqual(completed['story_revision'], original['story_revision'] + 1)
        self.assertEqual(len(completed['versions']), len(original['versions']) + 1)
        self.assertEqual(completed['versions'][0]['story'], BODY)
        self.assertEqual(completed['editorial_review'], 'ready')
        self.assertEqual(completed['task']['status'], 'completed')
        self.assertEqual(self.ok(self.queue('develop')), completed)
        self.runners['develop'].assert_called_once()

    def test_audience_evidence_checkpoint_survives_without_changing_current_text(self):
        original=self.create()
        self.ok(self.queue('review'))
        checks=[{'key':'interest','decision':'keep','quote':BODY[:20],
                 'reason':'本轮试读依据。','suggestion':''}]
        def examined(brief, body, *, progress, **kwargs):
            progress('reviewing',{'event':'completed','review':ready_review(),
                                 'audience_checks':checks,'story_sha256':story_text_sha256(body)})
            return ready_review()
        completed=self.complete('review',effect=examined)
        self.assert_body_unchanged(completed,original)
        self.assertEqual(completed['task']['checkpoints'][0]['audience_checks'],checks)

    def test_revised_development_does_not_claim_the_old_synopsis_matches_the_new_body(self):
        self.create()
        self.ok(self.queue('develop'))
        result = developed_result()
        initial = copy.deepcopy(result['draft'])
        revised_text = initial['story'] + '小刺猬平安到家后，把最后一片干燥的叶子送给了两位朋友。'
        result['draft'].update(story=revised_text, story_sha256=story_text_sha256(revised_text),
                               derived_status='stale', derived_from_story_sha256=initial['story_sha256'])
        result['revision_rounds'] = 1
        result['reviews'] = [issue_review(initial['story'][:20]), ready_review()]
        completed = self.complete('develop', result)
        self.assertEqual(completed['story'], revised_text)
        self.assertEqual(completed['derived_status'], 'stale')
        self.assertEqual(completed['derived_from_story_sha256'], initial['story_sha256'])
        self.assertNotEqual(completed['derived_from_story_sha256'], completed['story_sha256'])

    def test_failure_after_writing_saves_checkpoints_without_overwriting_a_good_draft_or_leaking_secrets(self):
        original = self.create()
        self.ok(self.queue('develop'))
        secret = 'TEST-SECRET-fake-provider-key-451'

        def fail_after_draft(brief, *, progress, **kwargs):
            progress('planning', {'event': 'completed', 'plan': plan_result(), 'message': '方向已整理。'})
            progress('writing', {'event': 'completed', 'draft': model_result(), 'message': '候选正文已完成。'})
            raise RuntimeError(f'https://private-provider.invalid/?key={secret} Authorization: Bearer {secret}')

        failed = self.complete('develop', effect=fail_after_draft)
        self.assertEqual(failed['task']['status'], 'failed')
        self.assert_body_unchanged(failed, original)
        checkpoints = json.dumps(failed['task']['checkpoints'], ensure_ascii=False)
        self.assertIn(model_result()['story'], checkpoints)
        self.assertIn('direction_1', checkpoints)
        persisted = '\n'.join(path.read_text(encoding='utf-8') for path in self.root.rglob('*.json'))
        self.assertNotIn(secret, json.dumps(failed, ensure_ascii=False))
        self.assertNotIn(secret, persisted)
        self.assertNotIn('private-provider.invalid', persisted)
        self.assertEqual(self.ok(self.queue('develop')), failed)
        self.start.assert_called_once()

    def test_read_only_review_is_bound_to_real_quoted_text_without_changing_the_body(self):
        original = self.create(intent='manuscript', idea='')
        self.ok(self.queue('review'))
        reviewed = self.complete('review', issue_review())
        self.assert_body_unchanged(reviewed, original)
        self.assertEqual(reviewed['review']['verdict'], 'needs_revision')
        self.assertEqual(reviewed['review']['issues'][0]['quote'], SELECTION)
        self.assertEqual(reviewed['task']['status'], 'completed')

    def test_review_with_a_fabricated_quote_is_rejected_without_replacing_the_story(self):
        original = self.create()
        self.ok(self.queue('review'))
        fabricated = '原文完全不存在的评语引文。'
        failed = self.complete('review', issue_review(fabricated))
        self.assert_body_unchanged(failed, original)
        self.assertEqual(failed['task']['status'], 'failed')
        self.assertNotIn(fabricated, json.dumps(failed.get('review'), ensure_ascii=False))

    def test_title_only_edit_keeps_a_current_review_but_body_edit_invalidates_it(self):
        self.create()
        self.ok(self.queue('review'))
        reviewed = self.complete('review')
        titled = self.ok(self.update(reviewed, title='标题变化不会改变正文事实'))
        self.assertEqual(titled['review']['status'], 'current')
        self.assertEqual(titled['review']['story_sha256'], reviewed['story_sha256'])
        edited = self.ok(self.update(titled, story=REWRITTEN))
        self.assertEqual(edited['review']['status'], 'stale')
        self.assertEqual(edited['editorial_review'], 'stale')

    def test_brief_change_invalidates_a_review_even_when_the_body_is_identical(self):
        self.create()
        self.ok(self.queue('review'))
        reviewed = self.complete('review')
        edited = self.ok(self.update(reviewed, duration_seconds=180))
        self.assertEqual(edited['story'], reviewed['story'])
        self.assertEqual(edited['story_revision'], reviewed['story_revision'])
        self.assertEqual(edited['review']['status'], 'stale')
        self.assertEqual(edited['editorial_review'], 'stale')

    def test_unicode_rewrite_is_a_proposal_until_accepted_and_uses_codepoint_offsets(self):
        original, proposed = self.proposal()
        self.assert_body_unchanged(proposed, original)
        proposal = proposed['rewrite_proposal']
        self.assertEqual(proposal['status'], 'pending')
        self.assertEqual(proposal['base_story_revision'], original['story_revision'])
        self.assertEqual(proposal['base_story_sha256'], original['story_sha256'])
        self.assertEqual(proposal['start'], len(PREFIX))
        self.assertEqual(proposal['end'], len(PREFIX + SELECTION))
        self.assertEqual(proposal['original'], SELECTION)
        self.assertEqual(proposal['replacement'], REPLACEMENT)
        self.assertEqual(proposal['story'], REWRITTEN)
        call = self.runners['rewrite'].call_args
        self.assertEqual(call.args[1:4], (BODY, len(PREFIX), len(PREFIX + SELECTION)))
        self.assertEqual(self.get(), proposed)

    def test_accepting_proposal_changes_only_selected_codepoints_and_preserves_outer_whitespace(self):
        original, proposed = self.proposal()
        accepted = self.ok(self.action('accept', proposed))
        self.assertEqual(accepted['story'], REWRITTEN)
        self.assertEqual(accepted['story'][:len(PREFIX)], PREFIX)
        self.assertEqual(accepted['story'][len(PREFIX + REPLACEMENT):], SUFFIX)
        self.assertEqual(accepted['story_revision'], original['story_revision'] + 1)
        self.assertEqual(accepted['versions'][0]['story'], BODY)
        self.assertEqual(len(accepted['versions']), len(original['versions']) + 1)
        repeated = self.action('accept', accepted, proposed['rewrite_proposal']['id'])
        if repeated.status_code in (200, 201, 202):
            self.assertEqual(repeated.json()['story_revision'], accepted['story_revision'])
        else:
            self.assertIn(repeated.status_code, (400, 409))
        self.assertEqual(self.get()['story_revision'], accepted['story_revision'])

    def test_a_runner_cannot_smuggle_changes_outside_the_selected_range(self):
        original = self.create()
        self.ok(self.queue('rewrite'))
        malicious = {**proposal_result(), 'story': '开头被模型偷偷换掉。' + REWRITTEN}
        result = self.complete('rewrite', malicious)
        self.assert_body_unchanged(result, original)
        proposal = result.get('rewrite_proposal')
        if proposal and proposal.get('status') == 'pending':
            self.assertEqual(proposal['story'], REWRITTEN, 'The server must reconstruct the proposal from its saved source range')
        else:
            self.assertEqual(result['task']['status'], 'failed')

    def test_invalid_ranges_and_empty_instruction_are_rejected_before_starting_a_runner(self):
        original = self.create()
        for changes in ({'start': -1}, {'start': 8, 'end': 8}, {'start': 9, 'end': 8},
                        {'end': len(BODY) + 1}, {'start': 0.5}, {'end': True}, {'instruction': '   '}):
            with self.subTest(changes=changes):
                response = self.queue('rewrite', **changes)
                self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.get(), original)
        self.start.assert_not_called()

    def test_discarding_or_using_the_wrong_proposal_id_never_replaces_the_body(self):
        original, proposed = self.proposal()
        for action in ('accept', 'discard'):
            with self.subTest(action=action):
                self.assertIn(self.action(action, proposed, 'unrelated-proposal').status_code, (400, 404, 409))
        self.assertEqual(self.get(), proposed)
        discarded = self.ok(self.action('discard', proposed))
        self.assert_body_unchanged(discarded, original)
        self.assertIn(self.action('accept', discarded, proposed['rewrite_proposal']['id']).status_code, (400, 409))
        self.assert_body_unchanged(self.get(), original)

    def test_title_only_change_preserves_proposal_but_old_api_revision_cannot_accept_it(self):
        original, proposed = self.proposal()
        titled = self.ok(self.update(proposed, title='只修改了标题'))
        self.assertEqual(self.action('accept', proposed).status_code, 409)
        accepted = self.ok(self.action('accept', titled, proposed['rewrite_proposal']['id']))
        self.assertEqual(accepted['title'], '只修改了标题')
        self.assertEqual(accepted['story'], REWRITTEN)
        self.assertEqual(accepted['story_revision'], original['story_revision'] + 1)

    def assert_edit_invalidates_proposal(self, **changes):
        _, proposed = self.proposal()
        edited = self.ok(self.update(proposed, **changes))
        response = self.action('accept', edited, proposed['rewrite_proposal']['id'])
        self.assertIn(response.status_code, (400, 409), response.text)
        self.assertEqual(self.get(), edited)

    def test_manual_body_edit_invalidates_a_pending_rewrite(self):
        self.assert_edit_invalidates_proposal(story=BODY + '作者又在末尾亲自补充了一句话。')

    def test_manual_brief_edit_invalidates_a_pending_rewrite(self):
        self.assert_edit_invalidates_proposal(idea='这次改为两个朋友互相告别的故事。')

    def test_whitespace_only_body_edit_also_invalidates_the_old_range_snapshot(self):
        self.assert_edit_invalidates_proposal(story='\n' + BODY)

    def test_late_progress_from_an_old_task_cannot_mutate_a_new_task(self):
        self.create()
        self.ok(self.queue('develop'))
        callbacks = {}

        def fail_after_capture(brief, *, progress, **kwargs):
            callbacks['progress'] = progress
            raise RuntimeError('Synthetic first runner failure')

        self.complete('develop', effect=fail_after_capture)
        running = self.ok(self.queue('review', request_id='request-two'))
        callbacks['progress']('writing', {'event': 'completed', 'draft': model_result(), 'message': '旧任务的迟到结果。'})
        self.assertEqual(self.get(), running)

    def test_background_operation_remains_readable_and_restart_does_not_repeat_it(self):
        original = self.create()
        running = self.ok(self.queue('develop'))
        self.assertEqual(self.get()['task'], running['task'])
        self.assertEqual(self.update(running, story=REWRITTEN).status_code, 409)
        with patch.object(stories, 'PROCESS_ID', 'new-process-for-test'):
            interrupted = self.get()
        self.assertEqual(interrupted['task']['status'], 'interrupted')
        self.assert_body_unchanged(interrupted, original)
        self.start.assert_called_once()
        for runner in self.runners.values():
            runner.assert_not_called()

    def test_duplicate_worker_does_not_release_the_real_workers_owner_slot(self):
        self.create()
        other_id = 'e' * 32
        other = self.ok(self.client.post('/api/stories', json={
            'id': other_id, 'intent': 'topics', 'idea': '', 'preset': 'general',
            'duration_seconds': 60, 'title': '同账号的另一篇草稿', 'story': '',
        }))
        self.ok(self.queue('develop'))
        observations = []

        def with_duplicate_worker(brief, **kwargs):
            stories._run_generation(1, self.identity, 'request-one')
            response = self.client.post('/api/stories/' + other_id + '/directions',
                                        json={'revision': other['revision'], 'request_id': 'request-other'})
            observations.append(((1, self.identity, 'request-one') in stories.ACTIVE, response.status_code))
            return developed_result()

        completed = self.complete('develop', effect=with_duplicate_worker)
        self.assertEqual(observations, [(True, 409)])
        self.assertEqual(completed['task']['status'], 'completed')
        self.assertNotIn((1, self.identity, 'request-one'), stories.ACTIVE)
        self.start.assert_called_once()

    def test_changing_the_brief_expires_selected_directions_before_another_task_can_start(self):
        self.create()
        self.ok(self.queue('directions'))
        planned = self.complete('directions')
        edited = self.ok(self.update(planned, idea='改为两个朋友互相道别的故事。'))
        self.assertEqual(edited['development']['status'], 'stale')
        response = self.queue('develop', request_id='request-two', direction_id='direction_1')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.get(), edited)
        self.start.assert_called_once()

    def test_unknown_direction_is_rejected_without_consuming_a_request(self):
        self.create()
        self.ok(self.queue('directions'))
        planned = self.complete('directions')
        response = self.queue('develop', request_id='request-two', direction_id='not-in-this-plan')
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.get(), planned)
        valid = self.ok(self.queue('develop', request_id='request-two', direction_id='direction_2'))
        self.assertEqual(valid['task']['status'], 'running')
        self.assertEqual(self.start.call_count, 2)

    def test_blank_topics_can_be_developed_but_have_no_body_to_review_or_rewrite(self):
        blank = self.create(intent='topics', idea='', story='')
        for operation in ('review', 'rewrite'):
            with self.subTest(operation=operation):
                self.assertEqual(self.queue(operation).status_code, 422)
        self.assertEqual(self.get(), blank)
        self.start.assert_not_called()
        self.ok(self.queue('develop'))
        completed = self.complete('develop')
        self.assertEqual(completed['story_revision'], 1)
        self.assertEqual(completed['story'], model_result()['story'])

    def test_manuscript_cannot_be_replanned_but_explicit_selection_rewrite_is_allowed(self):
        original = self.create(intent='manuscript', idea='')
        for operation in ('directions', 'develop'):
            with self.subTest(operation=operation):
                self.assertEqual(self.queue(operation).status_code, 409)
        self.assertEqual(self.get(), original)
        self.start.assert_not_called()
        self.ok(self.queue('rewrite'))
        proposed = self.complete('rewrite')
        self.assert_body_unchanged(proposed, original)
        self.assertEqual(proposed['rewrite_proposal']['story'], REWRITTEN)


if __name__ == '__main__':
    unittest.main()
