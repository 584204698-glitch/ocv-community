import copy
import unittest
from unittest.mock import Mock

from backend.app import video_native_story as native, video_prop_states as states
from backend.app import video_native_staging as staging
from backend.app.video_plan import design_snapshot
from backend.app.video_scene_references import scene_mapping
from tests.test_video_prop_continuity import PROP
from tests import test_video_native_story as story_fixtures
from tests import test_video_native_staging as staging_fixtures


class PropStateTests(unittest.TestCase):
    def setUp(self):
        self.f = story_fixtures.NativeDirectorTests(); self.f.setUp()
        self.raw = copy.deepcopy(self.f.raw)
        self.raw['props'] = [copy.deepcopy(PROP)]
        self.raw['prop_state_tracks'] = [dict(prop_id=PROP['id'], initial_state='state_door', states=[
            dict(id='state_door', description='从屋外面向门看，雨伞靠在门左侧墙上，收拢。',
                 description_en='The closed umbrella leans against the wall left of the door when viewed from outside.'),
            dict(id='state_bear', description='小熊握着已经打开的雨伞，站在门外。',
                 description_en='The bear holds the open umbrella outside the doorway.')])]
        for shot in self.raw['shots']:
            shot['prop_timeline'] = [dict(prop_id=PROP['id'], opening_state='state_door', ending_state='state_door',
                visibility='opening', visibility_basis='本镜能看到门左侧的整片墙面。', change_basis='')]

    def compile(self):
        return native.compile_storyboard(self.raw, self.f.record, self.f.cast, self.f.refs)[1]

    def test_boundary_conflict_recovers_old_empty_scope_without_whole_story_rewrite(self):
        previous=copy.deepcopy(self.raw)
        previous['shots'][1]['prop_timeline'][0].update(opening_state='state_bear',ending_state='state_bear')
        with self.assertRaises(states.LocalPropBasisError) as error:
            states.compile_timeline(previous,previous['props'],previous['shots'])
        self.assertEqual(error.exception.shot_ids,['shot_001','shot_002'])
        repair=dict(previous=previous,shot_ids=[],problems='legacy error')
        fixed=dict(replacements=[dict(id=f'shot_{i:03d}',shots=[copy.deepcopy(s)])
                                 for i,s in enumerate(self.raw['shots'],1)])
        ask=Mock(return_value=fixed)
        result=native._generate_candidate(ask,'system',{},repair)
        self.assertEqual(ask.call_args.args[1]['repair']['shot_ids'],['shot_001','shot_002'])
        self.assertEqual(result,self.raw)
        self.assertEqual(repair['shot_ids'],[])

    def test_first_shot_boundary_conflict_never_targets_shot_zero(self):
        raw=copy.deepcopy(self.raw)
        raw['shots'][0]['prop_timeline'][0]['opening_state']='state_bear'
        with self.assertRaises(states.LocalPropBasisError) as error:
            states.compile_timeline(raw,raw['props'],raw['shots'])
        self.assertEqual(error.exception.shot_ids,['shot_001'])

    def test_partial_track_patch_preserves_omitted_props_and_order(self):
        previous=copy.deepcopy(self.raw)
        extra=copy.deepcopy(previous['prop_state_tracks'][0]);extra['prop_id']='prop_spare'
        previous['props'].append({**copy.deepcopy(PROP),'id':'prop_spare'})
        previous['prop_state_tracks'].append(extra)
        for shot in previous['shots']:
            shot['prop_timeline'].append({**copy.deepcopy(shot['prop_timeline'][0]),'prop_id':'prop_spare'})
        before=copy.deepcopy(previous)
        for patches in ([],[extra],list(reversed(previous['prop_state_tracks']))):
            result=states.merge_repaired_tracks(previous,previous,patches,['shot_002'])
            self.assertEqual(result,previous)
        self.assertEqual(previous,before)

    def test_track_patch_rejects_unknown_duplicate_and_missing_timeline(self):
        original=copy.deepcopy(self.raw['prop_state_tracks'][0])
        for tracks,message in (([original,original],'重复'),
                               ([{**original,'prop_id':'prop_unknown'}],'未知')):
            with self.assertRaisesRegex(ValueError,message):
                states.merge_repaired_tracks(self.raw,self.raw,tracks,['shot_002'])
        candidate=copy.deepcopy(self.raw);candidate['shots'][1]['prop_timeline']=[]
        with self.assertRaisesRegex(ValueError,'所有关键道具'):
            states.merge_repaired_tracks(self.raw,candidate,[],['shot_002'])

    def test_return_to_opening_state_keeps_action_and_repairs_only_invalid_basis(self):
        raw = story_fixtures.with_performance(self.raw)
        first = raw['shots'][0]
        first['action'] = '小兔拿起收拢的伞看了一眼，再把它靠回原位。'
        first['native_dialogue']['performance_beats'][0].update(
            action='The rabbit lifts the closed umbrella, looks at it, and returns it to its original place.',
            action_zh=first['action'])
        first['prop_timeline'][0]['change_basis'] = first['action']
        fixed = copy.deepcopy(first)
        fixed['prop_timeline'][0]['change_basis'] = ''
        ask = Mock(side_effect=[raw,
            dict(replacements=[dict(id='shot_001', shots=[fixed])]),
            story_fixtures.good_story_review(),
            dict(reviews=[dict(id=f'shot_{i:03d}', problems=[]) for i in (1, 2)])])
        original = copy.deepcopy(raw)
        _, _, shots = native.plan_native_story(self.f.record, self.f.refs,
            dict(speech_mode='dialogue', project_cast=self.f.cast),
            progress=lambda _:None, persist=lambda:None, ask=ask)
        repair = ask.call_args_list[1].args[1]['repair']
        self.assertEqual(repair['shot_ids'], ['shot_001'])
        self.assertEqual(repair['previous'], original)
        self.assertEqual(self.f.record['native_planning_state']['candidate']['shots'][1], original['shots'][1])
        self.assertEqual(shots[0]['native_dialogue']['performance_beats'], first['native_dialogue']['performance_beats'])
        self.assertEqual([{k:t[k] for k in ('character_id','text','start','end')}
                          for t in shots[0]['native_dialogue']['turns']], first['native_dialogue']['turns'])
        self.assertEqual(shots[0]['continuity_states'][0]['change_basis'], '')
        self.assertNotIn('validation_shot_ids', self.f.record['native_planning_state'])
        self.assertEqual(ask.call_count, 4)

    def test_targeted_repair_corrects_shared_state_without_changing_unrelated_inputs(self):
        first, second = self.raw['shots']
        first['action']='小兔把伞递给小熊，小熊撑开伞。'
        first['prop_timeline'][0].update(ending_state='state_bear',change_basis=first['action'])
        second['prop_timeline'][0].update(opening_state='state_bear',ending_state='state_bear')
        # The same faulty definition reaches both prompts: repair must fix its source.
        track=self.raw['prop_state_tracks'][0]
        track['states'][1].update(description='小熊持伞，随后走向门外。',
                                  description_en='The bear holds the umbrella and then walks outside.')
        before=copy.deepcopy(self.raw)
        repair=dict(previous=self.raw,shot_ids=['shot_001','shot_002'],problems='状态包含后续动作')
        response=dict(replacements=[dict(id=f'shot_{i:03d}',shots=[copy.deepcopy(s)])
                                    for i,s in enumerate(self.raw['shots'],1)],
                      prop_state_tracks=copy.deepcopy(self.raw['prop_state_tracks']))
        response['prop_state_tracks'][0]['states'][1].update(
            description='小熊在门外握着已经打开的雨伞。',
            description_en='The bear holds the open umbrella outside the doorway.')
        ask=Mock(return_value=response)
        result=native._generate_candidate(ask,'system',{},repair)
        self.assertEqual(ask.call_args.args[1]['state_repair_scope'],{PROP['id']:['state_bear']})
        shots=native.compile_storyboard(result,self.f.record,self.f.cast,self.f.refs)[1]
        self.assertIn('outside the doorway',shots[0]['native_dialogue']['scene'])
        self.assertIn('小熊在门外',shots[1]['image_prompt'])
        self.assertNotIn('then walks',shots[1]['native_dialogue']['scene'])
        self.assertEqual(self.raw,before)
        # Without the first shot in scope, its ending state is immutable.
        repair['shot_ids']=['shot_002'];response['replacements']=response['replacements'][1:]
        with self.assertRaisesRegex(ValueError,'未修订镜头'):
            native._generate_candidate(Mock(return_value=response),'system',{},repair)
        self.assertEqual(self.raw,before)

    def test_repair_may_add_transition_state_but_not_teleport_or_change_initial_world(self):
        previous=copy.deepcopy(self.raw)
        response=dict(replacements=[dict(id='shot_002',shots=[copy.deepcopy(self.raw['shots'][1])])],
                      prop_state_tracks=copy.deepcopy(self.raw['prop_state_tracks']))
        response['prop_state_tracks'][0]['states'].append(dict(id='state_floor',description='收拢的伞平放在门边地面上。',
            description_en='The closed umbrella lies on the floor beside the doorway.'))
        response['prop_state_tracks'][0]['states'].append(dict(id='unused_spare',description='未采用的备用状态。',
            description_en='An unused spare state for the umbrella.'))
        shot=response['replacements'][0]['shots'][0]
        shot['action']='小兔把收拢的伞放在地上。'
        shot['prop_timeline'][0].update(ending_state='state_floor',change_basis=shot['action'])
        repair=dict(previous=previous,shot_ids=['shot_002'],problems='补足放伞动作')
        result=native._generate_candidate(Mock(return_value=response),'system',{},repair)
        self.assertNotIn('unused_spare',[s['id'] for s in result['prop_state_tracks'][0]['states']])
        oldshots=self.compile()
        newshots=native.compile_storyboard(result,self.f.record,self.f.cast,self.f.refs)[1]
        self.assertEqual(oldshots[0],newshots[0])
        for kind in ('teleport','initial','protected','removed'):
            bad=copy.deepcopy(response)
            if kind=='teleport':bad['replacements'][0]['shots'][0]['prop_timeline'][0]['opening_state']='state_floor'
            elif kind=='initial':bad['prop_state_tracks'][0]['initial_state']='state_floor'
            elif kind=='protected':bad['prop_state_tracks'][0]['states'][0]['description_en']='The umbrella is in another room.'
            else:bad['prop_state_tracks'][0]['states'].pop(0)
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                native._generate_candidate(Mock(return_value=bad),'system',{},repair)
        self.assertEqual(previous,self.raw)

    def test_existing_prop_is_present_before_first_verbal_mention_and_reaches_real_inputs(self):
        shots = self.compile()
        legacy = native.compile_storyboard(self.f.raw, self.f.record, self.f.cast, self.f.refs)[1]
        for shot, original in zip(shots, legacy):
            self.assertIn(PROP['appearance'], shot['image_prompt'])
            self.assertIn('从屋外面向门看', shot['image_prompt'])
            self.assertIn('left of the door when viewed from outside', shot['native_dialogue']['scene'])
            self.assertEqual(shot['continuity_states'][0]['opening']['id'], 'state_door')
            self.assertEqual(shot['native_dialogue']['turns'], original['native_dialogue']['turns'])
        self.assertEqual(scene_mapping({'shots': shots})[0]['visual_design']['continuity_states'], shots[0]['continuity_states'])
        saved = design_snapshot(shots[0])
        shots[0]['continuity_states'][0]['opening']['description'] = 'changed'
        self.assertNotEqual(saved['continuity_states'], shots[0]['continuity_states'])

    def test_missing_state_unknown_state_duplicate_or_teleport_cannot_reach_images(self):
        for mode in ('missing', 'unknown', 'duplicate', 'teleport', 'no_basis'):
            with self.subTest(mode=mode):
                before = copy.deepcopy(self.raw)
                if mode == 'missing':
                    self.raw['shots'][0].pop('prop_timeline')
                elif mode == 'unknown':
                    self.raw['shots'][0]['prop_timeline'][0]['ending_state'] = 'state_missing'
                elif mode == 'duplicate':
                    self.raw['prop_state_tracks'].append(copy.deepcopy(self.raw['prop_state_tracks'][0]))
                elif mode == 'teleport':
                    self.raw['shots'][1]['prop_timeline'][0]['opening_state'] = 'state_bear'
                else:
                    self.raw['shots'][0]['prop_timeline'][0]['ending_state'] = 'state_bear'
                with self.assertRaises(ValueError): self.compile()
                self.raw = before

    def test_move_is_allowed_and_next_shot_receives_new_position_without_affecting_words(self):
        first, second = self.raw['shots']
        first['action'] = '小兔把伞递给小熊，小熊撑开伞。'
        first['prop_timeline'][0].update(ending_state='state_bear', change_basis=first['action'])
        second['prop_timeline'][0].update(opening_state='state_bear', ending_state='state_bear')
        shots = self.compile()
        self.assertIn('靠在门左侧', shots[0]['image_prompt'])
        self.assertNotIn('小熊握着已经打开', shots[0]['image_prompt'])
        self.assertIn('小熊握着已经打开', shots[1]['image_prompt'])
        self.assertNotIn('靠在门左侧', shots[1]['image_prompt'])
        self.assertIn('Ending physical state: The bear holds', shots[0]['native_dialogue']['scene'])

    def test_offscreen_and_later_preserve_physical_state_without_forcing_prop_into_frame(self):
        for visibility in ('offscreen', 'later'):
            self.raw['shots'][0]['prop_timeline'][0].update(visibility=visibility, visibility_basis='开场为人物面部特写，门边墙面不在取景内。')
            shots = self.compile()
            self.assertNotIn(PROP['appearance'], shots[0]['image_prompt'])
            self.assertIn('不画入开场', shots[0]['image_prompt'])
            self.assertEqual(shots[0]['continuity_states'][0]['ending']['id'], 'state_door')
            self.assertIn(PROP['appearance'], shots[1]['image_prompt'])

    def test_local_action_keeps_boundaries_and_rejects_incompatible_edit_without_mutation(self):
        shot = self.compile()[0]; before = copy.deepcopy(shot)
        f = staging_fixtures.NativeStagingTest(); f.setUp()
        answer = {**f.result, 'prop_boundaries_compatible': True}
        updated, _ = staging.refresh({}, '水彩', shot, self.f.refs, basis='action', action='小兔走到小熊身边。', image_prompt='', ask=Mock(return_value=answer))
        self.assertEqual(updated['continuity_states'], shot['continuity_states'])
        self.assertIn('left of the door when viewed from outside', updated['native_dialogue']['scene'])
        self.assertEqual(states.apply(updated, updated['continuity_states']), updated)
        for compatible in (None, False):
            answer['prop_boundaries_compatible'] = compatible
            with self.assertRaisesRegex(ValueError, '开场与结束状态'):
                staging.refresh({}, '水彩', shot, self.f.refs, basis='action', action='把伞移走。', image_prompt='', ask=Mock(return_value=answer))
            self.assertEqual(shot, before)

    def test_legacy_plans_remain_unchanged_and_new_missing_tracks_are_rejected(self):
        legacy = self.f.raw
        self.assertIsNone(states.compile_timeline(legacy, [], legacy['shots']))
        candidate = copy.deepcopy(self.raw); candidate.pop('prop_state_tracks')
        ask = Mock(return_value=candidate)
        with self.assertRaisesRegex(ValueError, '缺少关键道具'):
            native.plan_native_story(self.f.record, self.f.refs, {'speech_mode':'dialogue','project_cast':self.f.cast},
                progress=lambda _: None, persist=lambda: None, ask=ask)
        self.assertEqual(ask.call_count, 2)

    def test_semantic_state_ids_do_not_require_an_arbitrary_prefix_or_extra_model_call(self):
        track = self.raw['prop_state_tracks'][0]
        track['initial_state'] = 'umbrella_by_door'
        track['states'][0]['id'] = 'umbrella_by_door'
        for shot in self.raw['shots']:
            shot['prop_timeline'][0].update(opening_state='umbrella_by_door', ending_state='umbrella_by_door')
        self.assertEqual(self.compile()[0]['continuity_states'][0]['opening']['id'], 'umbrella_by_door')
        for invalid in ('../outside', '<Audio 1>', 'x' * 81):
            track['states'][0]['id'] = invalid
            with self.assertRaises(ValueError): self.compile()
