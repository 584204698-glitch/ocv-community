import copy
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, Mock

from backend.app import voice_design_agent as agent, voice_design as voices


class VoiceDesignAgentTest(unittest.TestCase):
    def test_grounded_proposal_is_cached_but_new_preferences_are_independent(self):
        context = {'source': {'project_id': 'a'*32, 'character_id': 'pc_'+'b'*32},
                   'characters': [{'name': '小熊', 'constraints': '认真又害羞', 'dialogue_examples': ['我再试一次。']} ]}
        response = dict(name='温厚小熊', description='幼年声音，音区中低、圆润温和，标准普通话，语速适中，吐字清楚，自然交谈，不用播音腔。', reason='认真又害羞，区别于明亮活泼的小兔。')
        ask = Mock(return_value=response)
        request = agent.ProposalRequest(project_id='a'*32, character_id='pc_'+'b'*32)
        with TemporaryDirectory() as temp, patch.object(voices, 'ROOT', Path(temp)), patch.object(agent, 'project_context', return_value=(context, {})), patch('backend.app.language_routing.project_language_scope', return_value=nullcontext()):
            first = agent.propose(1, request, ask=ask)
            self.assertEqual(first, agent.propose(1, request, ask=ask))
            self.assertEqual(ask.call_count, 1)
            self.assertEqual(ask.call_args.args[1]['context'], context)
            self.assertEqual(first['reference_text'], agent.SAMPLE)
            request.preference = '更活泼一点'
            self.assertNotEqual(first['fingerprint'], agent.propose(1, request, ask=ask)['fingerprint'])
            self.assertEqual(ask.call_count, 2)
            agent.propose(2, request, ask=ask)
            self.assertEqual(ask.call_count, 3)

    def test_context_uses_only_server_owned_character_and_project(self):
        from backend.app import video_studio as studio
        actor = {'id':'pc_'+'b'*32, 'name':'小熊', 'constraints':'认真害羞'}
        record = {'id':'a'*32, 'settings':{'name':'找朋友'}, 'shots':[], 'native_story':{'text':'小熊想找朋友。'}}
        before = copy.deepcopy(record)
        with patch.object(studio, 'directory', return_value=Path('owned')), patch.object(studio, 'read', return_value=record), patch.object(studio, 'planning_parameters', return_value={}), patch('backend.app.video_dialogue._voice_characters', return_value=[actor]), patch('backend.app.video_dialogue._bindings', return_value={}):
            context, _ = agent.project_context(1, record['id'], actor['id'])
            self.assertEqual(context['source']['project_title'], '找朋友')
            self.assertEqual(context['story_excerpt'], '小熊想找朋友。')
            with self.assertRaisesRegex(ValueError, '角色不属于'):
                agent.project_context(1, record['id'], 'pc_'+'c'*32)
        self.assertEqual(record, before)

    def test_generated_voice_can_request_a_new_design_without_reference_id(self):
        from backend.app import video_studio as studio
        actor = {'id': 'pc_'+'b'*32, 'name': '奶奶', 'constraints': '老年女性'}
        record = {'id': 'a'*32, 'shots': []}
        with patch.object(studio, 'directory', return_value=Path('owned')), patch.object(studio, 'read', return_value=record), patch.object(studio, 'planning_parameters', return_value={}), patch('backend.app.video_dialogue._voice_characters', return_value=[actor]), patch('backend.app.video_dialogue._bindings', return_value={actor['id']: {'voice_mode': 'generated'}}):
            context, _ = agent.project_context(1, record['id'], actor['id'])
        self.assertEqual(context['characters'][0]['name'], '奶奶')
        self.assertNotIn('adopted_voice_description', context['characters'][0])

    def test_current_character_bible_source_and_scene_delivery_reach_designer_separately(self):
        from backend.app import video_studio as studio
        actor = {'id': 'pc_'+'b'*32, 'name': '守桥人', 'constraints': '成年女性，直率，重视承诺。'}
        other = 'pc_'+'c'*32
        turn = dict(character_id=actor['id'], text='请帮我一次。', delivery='Quietly, with restraint.',
                    emotion='sad', emotion_strength=.65, start=1, end=2)
        record = dict(id='a'*32, native_story={'text': '她一向独立，如今为了守住承诺向旅人求助。'},
            settings={'characters': '守桥人二十岁，平日果断，因失约而自责，想保护同伴。', 'world': '山城旧桥'},
            shots=[dict(id='shot_1', intent='放下防备，主动求助', action='她抬头看向旅人。',
                native_dialogue=dict(scene='桥边近景', speakers=[dict(character_id=actor['id'], appearance='年轻女性',
                    voice_description='旧方案：始终轻快含笑')], turns=[turn, dict(character_id=other, text='好。')],
                    performance_beats=[dict(start=0, end=3, action='She lowers her gaze.', action_zh='她垂下目光。')]))])
        parameters = {'story_handoff': {'status': 'current', 'title': '过桥',
            'source': {'story_id': 'd'*32}, 'brief': {'idea': '改编角色放下防备的片段', 'intent': 'manuscript'}}}
        before = copy.deepcopy(record)
        bindings = {actor['id']: {'voice_id': 'qwen3:'+'e'*32}}
        with patch.object(studio, 'directory', return_value=Path('owned')), \
             patch.object(studio, 'read', return_value=record), \
             patch.object(studio, 'planning_parameters', return_value=parameters), \
             patch('backend.app.video_dialogue._voice_characters', return_value=[actor]), \
             patch('backend.app.video_dialogue._bindings', return_value=bindings), \
             patch.object(voices, 'folder', return_value=Path('adopted')), \
             patch.object(voices, 'read', return_value={'description': '用户已采用的中低女声'}):
            context, _ = agent.project_context(1, record['id'], actor['id'])
        self.assertEqual(context['character_bible'], record['settings']['characters'])
        self.assertEqual(context['world_bible'], '山城旧桥')
        self.assertEqual(context['story_source']['brief'], parameters['story_handoff']['brief'])
        character = context['characters'][0]
        self.assertEqual(character['constraints'], actor['constraints'])
        self.assertEqual(character['adopted_voice_description'], '用户已采用的中低女声')
        performance = character['scene_performances'][0]
        self.assertEqual(performance['intent'], '放下防备，主动求助')
        self.assertEqual(performance['turns'], [{k: turn[k] for k in ('text', 'delivery', 'emotion', 'emotion_strength')}])
        self.assertEqual(performance['performance_beats'], record['shots'][0]['native_dialogue']['performance_beats'])
        self.assertNotIn('emotion', character['planned_traits'][0])
        context['characters'][0]['scene_performances'][0]['performance_beats'][0]['action'] = 'changed'
        self.assertEqual(record, before)
        self.assertEqual(bindings, {actor['id']: {'voice_id': 'qwen3:'+'e'*32}})

    def test_detached_story_brief_is_not_reused_as_current_character_authority(self):
        from backend.app import video_studio as studio
        actor = {'id': 'pc_'+'b'*32, 'name': '旅人'}
        record = {'id': 'a'*32, 'shots': [], 'native_story': {'text': '当前采用稿'}}
        handoff = {'status': 'detached', 'title': '旧稿', 'brief': {'idea': '旧人物设定'}}
        with patch.object(studio, 'directory', return_value=Path('owned')), \
             patch.object(studio, 'read', return_value=record), \
             patch.object(studio, 'planning_parameters', return_value={'story_handoff': handoff}), \
             patch('backend.app.video_dialogue._voice_characters', return_value=[actor]), \
             patch('backend.app.video_dialogue._bindings', return_value={}):
            context, _ = agent.project_context(1, record['id'], actor['id'])
        self.assertEqual(context['story_excerpt'], '当前采用稿')
        self.assertEqual(context['story_source']['status'], 'detached')
        self.assertNotIn('brief', context['story_source'])

    def test_changed_character_understanding_invalidates_proposal_without_replacing_adopted_voice(self):
        context = {'source': {}, 'character_bible': '年轻，直率，保护同伴',
                   'characters': [{'adopted_voice_description': '已经采用的声音'}]}
        response = dict(name='角色方案', description='年轻成年女声，中音区，声音结实清楚，标准普通话，语速自然平稳。', reason='符合角色身份；当前悲伤由对白表演表达。')
        request = agent.ProposalRequest(project_id='a'*32, character_id='pc_'+'b'*32)
        ask = Mock(return_value=response)
        with TemporaryDirectory() as temp, patch.object(voices, 'ROOT', Path(temp)), \
             patch.object(agent, 'project_context', side_effect=lambda *a: (copy.deepcopy(context), {})), \
             patch('backend.app.language_routing.project_language_scope', return_value=nullcontext()):
            adopted = Path(temp) / 'adopted.wav'; adopted.write_bytes(b'original adopted audio')
            first = agent.propose(1, request, ask=ask)
            context['character_bible'] = '老年，克制，保护同伴'
            second = agent.propose(1, request, ask=ask)
            self.assertNotEqual(first['fingerprint'], second['fingerprint'])
            self.assertEqual(ask.call_count, 2)
            self.assertEqual(adopted.read_bytes(), b'original adopted audio')
            self.assertEqual(context['characters'][0]['adopted_voice_description'], '已经采用的声音')

    def test_changed_direction_does_not_reuse_old_proposal_cache(self):
        context = {'source': {}, 'characters': []}
        response = dict(name='奶奶', description='苍老的女声，中低音，轻微沙哑和气息感，标准普通话，语速自然。', reason='符合角色年龄。')
        ask = Mock(return_value=response)
        request = agent.ProposalRequest(project_id='a'*32, character_id='pc_'+'b'*32)
        with TemporaryDirectory() as temp, patch.object(voices, 'ROOT', Path(temp)), patch.object(agent, 'project_context', return_value=(context, {})), patch('backend.app.language_routing.project_language_scope', return_value=nullcontext()):
            with patch.object(agent, 'REVISION', 'character-voice-director-v1'):
                old = agent.propose(1, request, ask=ask)
            new = agent.propose(1, request, ask=ask)
            self.assertNotEqual(old['fingerprint'], new['fingerprint'])
            self.assertEqual(ask.call_count, 2)


if __name__ == '__main__': unittest.main()
