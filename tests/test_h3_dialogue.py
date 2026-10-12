import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from backend.app.h3_dialogue import DialoguePlan, build_prompt, resolve_voices, normalize_dialogue, validate_dialogue_draft
from backend.app.comfyui_bridge import _patch_voice_references, WorkflowMappings

A, B = 'pc_' + 'a'*32, 'pc_' + 'b'*32


def data():
    return {'duration': 6, 'scene': 'Two women sit across a wooden table, both faces clearly visible. The camera holds a medium two-shot.', 'speakers': [
        {'character_id': A, 'name': '阿青', 'picture': 2, 'position': 'the left side of the frame', 'voice_id': 'qwen3:'+'1'*32},
        {'character_id': B, 'name': '阿蓝', 'picture': 3, 'position': 'the right side of the frame', 'voice_id': 'qwen3:'+'2'*32}],
        'turns': [{'character_id': B, 'text': '杯子还烫，小心一点。', 'start': .2, 'end': 2.7},
                  {'character_id': A, 'text': '知道了，我等一会儿。', 'start': 3, 'end': 5.5}]}


class DialogueTests(unittest.TestCase):
    def test_shared_core_picture_requires_explicit_compiler_confirmation(self):
        value=data()
        for speaker in value['speakers']:speaker['picture']=1
        plan=DialoguePlan.model_validate(value)
        with self.assertRaisesRegex(ValueError,'明确核对'):build_prompt(plan,picture_count=1)
        with self.assertRaisesRegex(ValueError,'明确核对'):build_prompt(plan,picture_count=1,core_identity_ids={A})
        prompt=build_prompt(plan,picture_count=1,core_identity_ids={A,B})
        self.assertIn('<Subject 1>',prompt);self.assertIn('<Subject 2>',prompt)
        for changed in ({'presence':'later'},{'on_screen':False}):
            candidate=copy.deepcopy(value);candidate['speakers'][0].update(changed)
            with self.assertRaises(ValueError):DialoguePlan.model_validate(candidate)
        for speaker in value['speakers']:speaker['picture']=2
        with self.assertRaises(ValueError):DialoguePlan.model_validate(value)

    def test_audio_guide_compiles_actions_once_without_repeating_actor_rules(self):
        value=data()
        value['performance_beats']=[
            {'start':0,'end':.2,'action':'Both women look at the cup.','action_zh':'两人看向杯子。'},
            {'start':.2,'end':2.7,'action':'A Lan points to the hot cup.','action_zh':'阿蓝指向烫杯子。'},
            {'start':2.7,'end':3,'action':'Both keep their hands still.','action_zh':'两人双手保持不动。'},
            {'start':3,'end':5.5,'action':'A Qing nods while answering.','action_zh':'阿青回答时点头。'},
            {'start':5.5,'end':6,'action':'Both wait beside the cup.','action_zh':'两人等待杯子冷却。'}]
        plan=DialoguePlan.model_validate(value)
        native=build_prompt(plan,picture_count=3)
        guide=build_prompt(plan,picture_count=3,dialogue_audio_guide=True)
        self.assertLess(len(guide),len(native))
        self.assertEqual(re.findall(r'<d>.*?</d>',guide),re.findall(r'<d>.*?</d>',native))
        for beat in value['performance_beats']:
            self.assertEqual(guide.count(beat['action']),1)
        self.assertEqual(guide.count('One continuous take, no cuts or shot transitions.'),1)
        self.assertIn('From 0.2 to 2.7 seconds: <Subject 2> (S1)',guide)
        self.assertIn('From 3 to 5.5 seconds: <Subject 1> (S2)',guide)
        self.assertIn('All mouths rest after their recording ends',guide)
        value['turns'][0]['mouth_visible']=False
        hidden=build_prompt(DialoguePlan.model_validate(value),picture_count=3,dialogue_audio_guide=True)
        self.assertIn("Keep the mouth concealed; never move a listener's mouth",hidden)
        self.assertIn('speech itself never reveals a concealed mouth',hidden)

    def test_camera_contract_is_once_and_preserves_planned_movement_and_words(self):
        value=data();value['scene']='The camera tracks the women walking along the table.'
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        self.assertEqual(prompt.count('One continuous take, no cuts or shot transitions.'),1)
        self.assertIn('Execute only explicitly described camera movement',prompt)
        self.assertIn(value['scene'],prompt)
        self.assertIn('camera position, focal length, framing and subject scale fixed',prompt)
        for turn in value['turns']:
            self.assertEqual(prompt.count(normalize_dialogue(turn['text'])),1)
        for heading in ('subject_definitions','summary','retention_analysis','detailed_description','overall_soundscape','non_diegetic_music'):
            self.assertEqual(prompt.count(heading+':\n'),1)

    def test_adopted_audio_overrides_stale_voice_description_without_changing_words(self):
        value=data();value['speakers'][0]['voice_description']='Old adult voice description'
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        self.assertNotIn('Old adult voice description',prompt)
        self.assertIn('natural speaking register heard in this recording',prompt)
        for turn in value['turns']:self.assertIn(normalize_dialogue(turn['text']),prompt)
        self.assertIn('brings its lips together and its jaw returns to rest',prompt)
        value['speakers'][0].update(voice_mode='generated',voice_id=None)
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        self.assertIn('Old adult voice description',prompt)

    def test_generated_voice_is_explicit_and_reference_audio_labels_are_compact(self):
        value=data()
        value['speakers'][1].update(voice_mode='generated', voice_id=None)
        plan=DialoguePlan.model_validate(value)
        prompt=build_prompt(plan,picture_count=3)
        self.assertIn('<Subject 2> (S1), using its native generated voice',prompt)
        self.assertIn('<Audio 1> is the voice-timbre reference for <Subject 1> (S2)',prompt)
        self.assertNotIn('<Audio 2>',prompt)
        from unittest.mock import patch
        with patch('backend.app.h3_dialogue.resolve_saved_voice', return_value={'voice_id':'fixed','path':'fixed.wav','sha256':'x'}) as resolve:
            voices=resolve_voices(1,plan)
        self.assertEqual(len(voices),1)
        self.assertEqual(voices[0]['speaker'],'S2')
        self.assertEqual(voices[0]['audio_label'],'<Audio 1>')
        resolve.assert_called_once()
        value['speakers'][0].update(voice_mode='generated', voice_id=None)
        plan=DialoguePlan.model_validate(value)
        self.assertEqual(resolve_voices(1,plan),[])
        self.assertNotIn('<Audio ',build_prompt(plan,picture_count=3))

    def test_missing_reference_voice_and_unknown_mode_are_rejected(self):
        value=data();value['speakers'][0]['voice_id']=None
        with self.assertRaises(ValueError): DialoguePlan.model_validate(value)
        value['speakers'][0]['voice_mode']='automatic'
        with self.assertRaises(ValueError): DialoguePlan.model_validate(value)
        self.assertTrue(all('voice_mode' not in s for s in DialoguePlan.model_validate(data()).model_dump()['speakers']))

    def test_empty_environment_shot_needs_no_fictitious_speaker_or_voice(self):
        value=data();value.update(speakers=[],turns=[],scene='Wind stirs leaves in an empty courtyard.')
        plan=DialoguePlan.model_validate(value)
        prompt=build_prompt(plan,picture_count=1)
        self.assertEqual(resolve_voices(1,plan),[])
        self.assertNotIn('<Subject ',prompt)
        self.assertNotIn('<Audio ',prompt)
        self.assertNotIn('<d>',prompt)
        value['turns']=data()['turns']
        with self.assertRaisesRegex(ValueError,'每条台词'):
            DialoguePlan.model_validate(value)

    def test_unseen_mouth_does_not_imply_a_hidden_speaker_or_face(self):
        value=data();value['turns'][0]['mouth_visible']=False
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        first=prompt.split('From 0.2 to 2.7 seconds,',1)[1].split('From 2.7 to 3 seconds',1)[0]
        self.assertIn('<Subject 2> (S1), using the timbre of <Audio 1>',first)
        self.assertIn('speaks in its assigned voice',first)
        self.assertIn("retain the speaker's planned visible body and face",first)
        self.assertNotIn('hiding place',prompt)
        self.assertNotIn('face and mouth remaining concealed',prompt)
        self.assertIn('no visible lip movement',first)
        self.assertNotIn('alone articulates',first)
        self.assertNotIn('Show its lip movement',first)
        self.assertIn('<d>[Chinese] 杯子还烫,小心一点.</d>',first)
        self.assertIn('<Subject 1> alone articulates',prompt)

    def test_visible_machine_without_mouth_keeps_its_planned_entrance(self):
        value=data()
        value['speakers'][1].update(appearance='A small machine with an indicator light and no mouth.',
                                   presence='later', position='Outside the opening frame, approaching from the right.')
        value['turns'][0]['mouth_visible']=False
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        self.assertIn('This turn follows the planned entrance or reveal.',prompt)
        self.assertIn('no visible lip movement',prompt)
        self.assertNotIn('hiding place',prompt)
        self.assertNotIn('mouth remains hidden',prompt)
        self.assertIn('<d>[Chinese] 杯子还烫,小心一点.</d>',prompt)

    def test_legacy_turn_serialization_does_not_add_visibility_assumptions(self):
        value=DialoguePlan.model_validate(data()).model_dump()
        self.assertTrue(all('mouth_visible' not in t for t in value['turns']))

    def test_listener_template_does_not_invent_gaze_or_posture_changes(self):
        prompt=build_prompt(DialoguePlan.model_validate(data()),picture_count=3)
        self.assertNotIn('reacting through gaze and posture',prompt)
        self.assertIn('retain its planned eye state, posture and facial occlusion',prompt)
        self.assertIn('following only its specified timed actions',prompt)

    def test_opening_extent_comes_from_scene_image_not_identity_portrait(self):
        value = data()
        # Partial visibility can be present in the image even when the draft
        # describes the position without the restrictive word "only".
        value['speakers'][1]['position'] = 'behind a curtain with one hand visible'
        value['scene'] = 'A fixed view of the curtain and the other woman.'
        original = copy.deepcopy(value)
        prompt = build_prompt(DialoguePlan.model_validate(value), picture_count=3)
        self.assertIn('show only the parts of <Subject 2> visible in <Picture 1>', prompt)
        self.assertIn('speaking by itself is not a reveal action', prompt)
        self.assertIn('full-body presentation are not part of this role', prompt)
        self.assertEqual(re.findall(r'<d>\[Chinese\] (.*?)</d>', prompt),
                         [normalize_dialogue(t['text']) for t in value['turns']])
        self.assertEqual(value, original)

    def test_extent_rule_does_not_freeze_planned_reveal_or_insert_later_actor(self):
        value = data()
        value['speakers'][0]['presence'] = 'later'
        value['speakers'][0]['position'] = 'outside the doorway until entering'
        value['performance_beats'] = [
            dict(start=0, end=.2, action='The woman waits.', action_zh='女子等待。'),
            dict(start=.2, end=2.7, action='The visible woman speaks.', action_zh='可见的女子说话。'),
            dict(start=2.7, end=3, action='The other woman enters through the doorway.', action_zh='另一名女子从门口走入。'),
            dict(start=3, end=5.5, action='The newcomer answers.', action_zh='新入画的女子回答。'),
            dict(start=5.5, end=6, action='They wait together.', action_zh='两人一起等待。')]
        prompt = build_prompt(DialoguePlan.model_validate(value), picture_count=3)
        self.assertNotIn('show only the parts of <Subject 1> visible in <Picture 1>', prompt)
        self.assertIn('show only the parts of <Subject 2> visible in <Picture 1>', prompt)
        self.assertIn('until a timed action explicitly changes the occlusion or framing', prompt)
        self.assertIn('The other woman enters through the doorway.', prompt)
        self.assertIn('This turn follows the planned entrance or reveal.', prompt)

    def test_partially_hidden_speaker_keeps_occlusion_and_exact_voice_binding(self):
        value = data()
        value['speakers'][1]['position'] = 'behind the tree trunk, only its ears visible'
        value['scene'] = 'A fixed view of a tree trunk and the listener. The speaking character remains behind the trunk.'
        original = copy.deepcopy(value)
        prompt = build_prompt(DialoguePlan.model_validate(value), picture_count=3)
        first = prompt.split('From 0.2 to 2.7 seconds,', 1)[1].split('From 2.7 to 3 seconds', 1)[0]
        self.assertIn('<Subject 2> (S1), using the timbre of <Audio 1>', first)
        self.assertIn('only where its mouth is visible in the planned view', first)
        self.assertIn('preserve the planned occlusion and framing', first)
        self.assertIn('Never transfer its lip movement to another character', first)
        self.assertIn('only its ears visible', prompt)
        self.assertIn(normalize_dialogue(value['turns'][0]['text']), first)
        self.assertEqual(value, original)

    def test_identical_prop_boundaries_compact_but_actual_changes_remain(self):
        from backend.app.h3_dialogue import _scene_description
        value = ('A woman reaches for the cup. Prop state continuity: '
                 'Opening physical state: The door is open. Ending physical state: The door is open. '
                 'Opening physical state: The cup is on the table. Ending physical state: The cup is in her hand.')
        result = _scene_description(value)
        self.assertEqual(result.count('The door is open.'), 1)
        self.assertIn('Opening and ending physical state: The door is open.', result)
        self.assertNotIn('State throughout the shot', result)
        self.assertIn('Intermediate changes follow the timed actions.', result)
        self.assertIn('Opening physical state: The cup is on the table.', result)
        self.assertIn('Ending physical state: The cup is in her hand.', result)
        self.assertIn('A woman reaches for the cup.', result)

    def test_story_wide_future_prop_states_do_not_enter_scoped_video_prompt(self):
        value = data()
        value['scene'] = ('Two people stand beside unbaked dough. Object identity constraints: '
                          'The dough later becomes golden bread decorated with raisins. Prop state continuity: '
                          'Opening physical state: Pale dough on the table. Ending physical state: Pale dough with a small hollow.')
        before = copy.deepcopy(value)
        prompt = build_prompt(DialoguePlan.model_validate(value), picture_count=3)
        self.assertNotIn('golden bread', prompt)
        self.assertNotIn('raisins', prompt)
        self.assertIn('Pale dough with a small hollow.', prompt)
        self.assertIn('Two people stand beside unbaked dough.', prompt)
        self.assertEqual(value, before)

    def test_actual_shot_transformation_is_not_removed_with_prop_catalog(self):
        from backend.app.h3_dialogue import _scene_description
        result = _scene_description('She decorates the bread. Object identity constraints: A loaf of bread. '
                                    'Prop state continuity: Opening physical state: Bare bread. '
                                    'Ending physical state: Bread decorated with raisins.')
        self.assertIn('She decorates the bread.', result)
        self.assertIn('Bread decorated with raisins.', result)
        legacy = 'A table. Object identity constraints: A blue ceramic cup.'
        self.assertEqual(_scene_description(legacy), legacy)

    def test_each_turn_keeps_identity_and_listener_without_repeating_appearance(self):
        value=data()
        value['speakers'][0]['appearance']='A woman wearing a yellow coat.'
        value['speakers'][1]['appearance']='A woman wearing a violet jacket.'
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        body=prompt.split('detailed_description:\n',1)[1].split('\n\noverall_soundscape:',1)[0]
        first=body.split('From 0.2 to 2.7 seconds,',1)[1].split('From 2.7 to 3 seconds',1)[0]
        second=body.split('From 3 to 5.5 seconds,',1)[1].split('From 5.5 seconds',1)[0]
        for event,speaker,listener in ((first,2,1),(second,1,2)):
            self.assertIn(f'<Subject {speaker}> alone articulates the entire utterance',event)
            self.assertIn(f'The listener is <Subject {listener}>',event)
            self.assertNotIn(f'The listener is <Subject {speaker}>',event)
        second_anchor=body.split('From 2.7 to 3 seconds',1)[1].split('From 3 to 5.5 seconds,',1)[0]
        self.assertIn('<Subject 1> is identified by <Picture 2>.',second_anchor)
        self.assertIn('the right side of the frame',body)
        self.assertEqual(prompt.count('A woman wearing a yellow coat.'),1)
        self.assertEqual(prompt.count('A woman wearing a violet jacket.'),1)
        self.assertEqual(body.count('Standard Mandarin Chinese (Putonghua)'),2)
        self.assertEqual(re.findall(r'<d>\[Chinese\] (.*?)</d>',body),
                         [normalize_dialogue(t['text']) for t in value['turns']])
        self.assertNotIn('fox',body);self.assertNotIn('hedgehog',body)

    def test_returning_speaker_reuses_voice_after_other_speaker_and_narrator(self):
        value=data();value['duration']=12
        identity='pc_'+'d'*32
        value['speakers'].append(dict(character_id=identity,name='旁白',kind='narrator',
                                     on_screen=False,voice_id='qwen3:'+'3'*32))
        value['turns'] += [dict(character_id=identity,text='她等了一会儿。',start=6,end=8),
                           dict(character_id=B,text='现在可以喝了。',start=9,end=11)]
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        final=prompt.split('From 9 to 11 seconds,',1)[1].split('From 11 seconds',1)[0]
        self.assertIn('<Subject 2> (S1), using the timbre of <Audio 1>',final)
        narrator=prompt.split('From 6 to 8 seconds,',1)[1].split('From 8 to 9 seconds',1)[0]
        self.assertIn('This voice has no visible speaking mouth',narrator)
        self.assertNotIn('alone articulates',narrator)
        self.assertNotIn('<Audio 4>',prompt)
        self.assertEqual(prompt.count('<d>'),4)

    def test_offscreen_actor_turn_does_not_request_visible_mouth_or_new_face(self):
        value=data();value['speakers'][1]['on_screen']=False
        value['speakers'][1]['position']='outside the window, unseen'
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        first=prompt.split('From 0.2 to 2.7 seconds,',1)[1].split('From 2.7 to 3 seconds',1)[0]
        self.assertIn('speaks off-screen',first)
        self.assertIn('This voice has no visible speaking mouth',first)
        self.assertNotIn('alone articulates',first)
        self.assertIn('do not show a face to represent this voice',prompt)

    def test_later_entry_preserves_identity_voice_and_does_not_claim_initial_visibility(self):
        value=data();value['speakers'][0]['presence']='later'
        value['speakers'][0]['position']='behind the closed door, then in the doorway'
        value['scene']='One woman waits at the table. The door opens and the other woman steps into view.'
        plan=DialoguePlan.model_validate(value)
        self.assertEqual(plan.model_dump()['speakers'][0]['presence'],'later')
        prompt=build_prompt(plan,picture_count=3)
        self.assertIn('<Subject 1> from <Picture 2> is initially outside the visible frame or occluded',prompt)
        self.assertNotIn('<Subject 1> from <Picture 2> is visible at',prompt)
        self.assertIn('<Audio 2> is the voice-timbre reference for <Subject 1> (S2)',prompt)
        self.assertIn('<Picture 2>',prompt)
        self.assertEqual(prompt.count('<d>'),2)

    def test_opening_presence_default_preserves_legacy_serialized_plan_and_prompt(self):
        old=DialoguePlan.model_validate(data())
        value=data()
        for s in value['speakers']:s['presence']='opening'
        explicit=DialoguePlan.model_validate(value)
        self.assertEqual(old.model_dump(),explicit.model_dump())
        self.assertTrue(all('presence' not in s for s in old.model_dump()['speakers']))
        self.assertEqual(build_prompt(old,picture_count=3),build_prompt(explicit,picture_count=3))

    def test_later_entry_cannot_be_assigned_to_narrator_or_permanently_offscreen_actor(self):
        for change in ({'on_screen':False},{'kind':'narrator','picture':None,'on_screen':False}):
            value=data();value['speakers'][0].update(presence='later',**change)
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'后入画计划'):
                DialoguePlan.model_validate(value)

    def test_action_only_beat_keeps_identities_without_fabricating_voice_or_words(self):
        value=data();value['turns']=[]
        value['scene']='One woman extends the cup while the other reaches for its handle.'
        for actor in value['speakers']:actor['voice_id']=None
        plan=DialoguePlan.model_validate(value)
        prompt=build_prompt(plan,picture_count=3)
        self.assertEqual(resolve_voices(1,plan),[])
        self.assertNotIn('<Audio',prompt);self.assertNotIn('<d>',prompt)
        self.assertNotIn('final line',prompt)
        self.assertIn('physical action sounds',prompt)
        self.assertIn('No speech',prompt)
        self.assertIn('<Picture 3>',prompt)

    def test_action_only_cast_waits_with_natural_resting_mouth_and_no_speaking_condition(self):
        value=data();value['turns']=[]
        value['scene']='Rabbit and Fox sit beside a table with an open doorway and an empty chair.'
        value['performance_beats']=[dict(start=0,end=6,
            action='Rabbit turns toward the doorway; Fox looks toward the empty chair. Both remain seated.',
            action_zh='兔子转头望向门口，狐狸看向空椅；两人保持坐姿。')]
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        self.assertEqual(prompt.count('keeps its mouth naturally closed'),2)
        self.assertNotIn('speaking by itself',prompt)
        self.assertNotIn('<Audio',prompt);self.assertNotIn('<d>',prompt)
        self.assertIn('Rabbit turns toward the doorway; Fox looks toward the empty chair.',prompt)
        self.assertIn('Preserve the planned location ambience and physical action sounds.',prompt)
        self.assertIn('Nonverbal vocal sounds occur only where explicitly described',prompt)

    def test_action_only_explicit_sigh_or_laugh_keeps_its_planned_sound_and_action(self):
        for action in ('The left woman sighs softly, then rests.', 'The left woman laughs briefly, then rests.'):
            value=data();value['turns']=[]
            value['performance_beats']=[dict(start=0,end=6,action=action,action_zh='左侧女子发出既定非语言声，随后恢复休息姿态。')]
            value['soundscape']=action
            prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
            self.assertIn(action,prompt)
            self.assertIn('Mouth movement occurs only when required by an explicitly planned nonverbal action',prompt)
            self.assertIn('overall_soundscape:\n'+action,prompt)

    def test_spoken_branch_preserves_exact_words_and_explicit_speech_end(self):
        import re
        value=data()
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        spoken=re.findall(r'<d>\[Chinese\] (.*?)</d>',prompt)
        words=lambda text:re.sub(r'[^\w]','',text)
        self.assertEqual([words(text) for text in spoken],
                         [words(turn['text']) for turn in value['turns']])
        self.assertIn('brings its lips together and its jaw returns to rest',prompt)
        self.assertIn('Only the defined speakers produce words.',prompt)

    def test_unbound_storyboard_survives_review_but_cannot_render_as_bound(self):
        from backend.app.video_plan import normalize_shots
        value=data()
        for speaker in value['speakers']:speaker['voice_id']=None
        draft=validate_dialogue_draft(value)
        scenes=[{'slide_id':'s1','start':0,'end':6,'text':'杯子还烫，小心一点。知道了，我等一会儿。'}]
        rows=normalize_shots([{'id':'shot-1','slide_ids':['s1'],'kind':'video','native_dialogue':draft}],scenes)
        self.assertEqual(rows[0]['native_dialogue'],draft)
        with self.assertRaisesRegex(ValueError,'固定音色'):DialoguePlan.model_validate(draft)
        scenes[0]['end']=7
        with self.assertRaisesRegex(ValueError,'时长不一致'):
            normalize_shots(rows,scenes)

    def test_speaker_order_follows_first_utterance_not_cast_order(self):
        prompt = build_prompt(DialoguePlan.model_validate(data()), picture_count=3)
        self.assertIn('<Subject 1> is the character whose facial identity comes from <Picture 2>', prompt)
        self.assertIn('<Picture 3>', prompt)
        self.assertIn('<Audio 1> is the voice-timbre reference for <Subject 2> (S1)', prompt)
        self.assertIn('<d>[Chinese] 杯子还烫,小心一点.</d>', prompt)
        self.assertEqual(prompt.count('<d>'), 2)
        self.assertNotIn('不生成任何语言的人声', prompt)
        self.assertTrue(prompt.endswith('N/A'))

    def test_official_sections_retention_language_and_verbatim_words(self):
        prompt = build_prompt(DialoguePlan.model_validate(data()), picture_count=3)
        names = re.findall(r'^([a-z_]+):$', prompt, re.M)
        self.assertEqual(names, ['subject_definitions', 'summary', 'retention_analysis',
                                'detailed_description', 'overall_soundscape', 'non_diegetic_music'])
        retention = prompt.split('retention_analysis:\n')[1].split('\n\ndetailed_description:')[0]
        self.assertIn('<Picture 1> ([Shot 1] composition anchor): fully_preserved', retention)
        self.assertIn('<Audio 1>: reference -', retention)
        self.assertIn('<Audio 2>: reference -', retention)
        self.assertNotRegex(retention, r'\(S\d')
        without_lines = re.sub(r'<d>.*?</d>', '', prompt)
        self.assertNotRegex(without_lines, r'[\u3400-\u9fff]')
        for original, rendered in zip(data()['turns'], re.findall(r'<d>\[Chinese\] (.*?)</d>', prompt)):
            self.assertEqual(re.sub(r'[^\w]', '', original['text']), re.sub(r'[^\w]', '', rendered))
        self.assertEqual(normalize_dialogue('你来吗？？'), '你来吗?')
        self.assertEqual(normalize_dialogue('“好呀！”'), '好呀!')

    def test_silent_character_has_subject_but_no_speaker_or_audio_id(self):
        value = data()
        value['speakers'].insert(0, {'character_id': 'pc_'+'c'*32, 'name': '路人',
                                    'picture': 4, 'position': 'the background'})
        prompt = build_prompt(DialoguePlan.model_validate(value), picture_count=4)
        self.assertIn('<Subject 1> is the character', prompt)
        self.assertNotIn('<Subject 1> (S', prompt)
        self.assertIn('<Audio 1> is the voice-timbre reference for <Subject 3> (S1)', prompt)
        self.assertNotIn('<Audio 3>', prompt)

    def test_mixed_voiceover_keeps_narrator_off_screen_and_original_cast_labels(self):
        value=data(); value['duration']=9
        identity='pc_'+'d'*32
        value['speakers'].append({'character_id':identity, 'name':'旁白', 'kind':'narrator',
            'on_screen':False, 'voice_id':'qwen3:'+'3'*32})
        value['turns'].append({'character_id':identity, 'text':'她终于笑了。', 'start':6, 'end':8})
        prompt=build_prompt(DialoguePlan.model_validate(value), picture_count=3)
        self.assertIn('<Audio 3> is the voice-timbre reference for the off-screen narrator (S3)', prompt)
        self.assertIn('says in an off-screen voiceover', prompt)
        self.assertIn("All on-screen characters' lips remain closed", prompt)
        self.assertNotIn('<Subject 3>', prompt)
        narration=prompt.split('From 6 to 8 seconds,',1)[1].split('From 8 seconds',1)[0]
        self.assertNotIn('The listener is',narration)
        self.assertIn('not a conversation heard or answered by this character',narration)
        self.assertIn('Narrator lines are an off-screen narration track outside the scene',prompt)
        # Actual character exchanges retain listener binding.
        self.assertIn('The listener is',prompt.split('From 6 to 8 seconds,',1)[0])
        value['speakers'][-1].update(voice_mode='generated',voice_id=None)
        generated=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        self.assertIn('Narrator lines are an off-screen narration track outside the scene',generated)
        self.assertNotIn('The listener is',generated.split('From 6 to 8 seconds,',1)[1])
        value['turns']=value['turns'][-1:]
        only=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        self.assertNotIn('<Audio ',only)
        self.assertNotIn('The listener is',only)
        self.assertIn('does not make the visible character speak or hear this narration',only)

    def test_three_speakers_and_fourth_voice_over_capacity(self):
        value=data();value['duration']=10
        for digit,start,picture in [('c',6,4),('d',8,5)]:
            identity='pc_'+digit*32
            value['speakers'].append({'character_id':identity,'name':digit,'picture':picture,
                'position':'behind the table','voice_id':'qwen3:'+digit*32})
            value['turns'].append({'character_id':identity,'text':'好。','start':start,'end':start+1})
            if digit=='c':
                prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=4)
                self.assertIn('<Subject 3> (S3)',prompt)
            else:
                with self.assertRaisesRegex(ValueError,'最多三个'):DialoguePlan.model_validate(value)

    def test_wrong_ids_overlap_overlong_lines_markup_and_missing_image_fail(self):
        for change in (lambda v:v['turns'][0].update(character_id='pc_'+'c'*32),
                       lambda v:v['turns'][1].update(start=2),
                       lambda v:v['turns'][0].update(text='长'*90),
                       lambda v:v['turns'][0].update(text='<d>你好</d>'),
                       lambda v:v['turns'][0].update(text='[Shot 2]'),
                       lambda v:v.update(scene='不应直接提交中文的内部描述'),
                       lambda v:v['speakers'][0].update(voice_id=None),
                       lambda v:v['speakers'][1].update(picture=2)):
            value=data();change(value)
            with self.assertRaises(ValueError): DialoguePlan.model_validate(value)
        with self.assertRaises(ValueError): build_prompt(DialoguePlan.model_validate(data()),picture_count=2)
        with self.assertRaises(ValueError): build_prompt(DialoguePlan.model_validate(data()),picture_count=4)

    def test_voice_bindings_cannot_cross_accounts_and_use_saved_exact_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for identity in ('1'*32,'2'*32):
                path=root/'1'/identity; (path/'design').mkdir(parents=True)
                (path/'record.json').write_text(json.dumps({'id':identity,'status':'saved'}))
                (path/'design/chunk-0001.wav').write_bytes(identity.encode())
            plan=DialoguePlan.model_validate(data())
            voices=resolve_voices(1,plan,root=root)
            self.assertEqual(voices[0]['character_id'],B)
            self.assertEqual(voices[1]['character_id'],A)
            self.assertNotEqual(voices[0]['sha256'],voices[1]['sha256'])
            with self.assertRaises(FileNotFoundError): resolve_voices(2,plan,root=root)
            (root/'1'/('1'*32)/'record.json').write_text(json.dumps({'status':'ready'}))
            with self.assertRaises(ValueError): resolve_voices(1,plan,root=root)

    def test_real_ref2va_audio_slots_order_and_no_old_voice_leak(self):
        graph={'7':{'class_type':'MiniMaxH3ReferenceToVideo','inputs':{'prompt':'x'}}}
        mappings=WorkflowMappings(prompt={'node_id':'7','input_name':'prompt'})
        result=_patch_voice_references(copy.deepcopy(graph),mappings,['voice-b.wav','voice-a.wav'])
        self.assertEqual(result['7']['inputs']['ref_audios.ref_audio_0'],['ocv_dialogue_voice_0',0])
        self.assertEqual(result['ocv_dialogue_voice_1']['inputs']['audio'],'voice-a.wav')
        with self.assertRaises(ValueError): _patch_voice_references(result,mappings,['new.wav'])
        graph['7']['class_type']='MiniMaxH3ImageToVideo'
        with self.assertRaises(ValueError): _patch_voice_references(graph,mappings,['voice.wav'])


if __name__ == '__main__': unittest.main()
