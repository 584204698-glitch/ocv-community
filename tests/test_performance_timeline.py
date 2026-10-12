import copy
import unittest
from unittest.mock import Mock
from backend.app.h3_dialogue import DialoguePlan, build_prompt, performance_intervals, validate_dialogue_draft
from backend.app.video_native_staging import complete_performance
from tests.test_h3_dialogue import data


def beats(plan):
    return [dict(start=a,end=b,action='Both friends stay beside the table.',action_zh='两位朋友留在桌旁。')
            for a,b in performance_intervals(plan.duration,plan.turns)]


class PerformanceTest(unittest.TestCase):
    def test_silent_subactions_keep_timing_order_and_do_not_reassign_speech(self):
        value=data()
        value['turns']=value['turns'][:1]
        value['turns'][0].update(start=3,end=5)
        value['performance_beats']=[
            dict(start=0,end=1,action='The left woman steadies the sign.',action_zh='左侧女子扶稳路牌。'),
            dict(start=1,end=2,action='The right woman ties the vine.',action_zh='右侧女子系好藤条。'),
            dict(start=2,end=3,action='The left woman releases the sign.',action_zh='左侧女子松开路牌。'),
            dict(start=3,end=5,action='The right woman addresses her friend.',action_zh='右侧女子对朋友说话。'),
            dict(start=5,end=6,action='Both women look at the secured sign.',action_zh='两人看着系好的路牌。')]
        plan=DialoguePlan.model_validate(value)
        prompt=build_prompt(plan,picture_count=3)
        for fragment in ('From 0 to 1 seconds: The left woman steadies the sign.',
                         'From 1 to 2 seconds: The right woman ties the vine.',
                         'From 2 to 3 seconds: The left woman releases the sign.'):
            self.assertIn(fragment,prompt)
        self.assertLess(prompt.index('steadies the sign'),prompt.index('ties the vine'))
        self.assertLess(prompt.index('ties the vine'),prompt.index('releases the sign'))
        self.assertEqual(prompt.count('<d>'),1)
        self.assertIn('<Subject 2> alone articulates the entire utterance',prompt)
        self.assertEqual(plan.model_dump()['performance_beats'],value['performance_beats'])
        # The same real action subdivisions are valid without any dialogue.
        silent=copy.deepcopy(value);silent['turns']=[]
        silent['performance_beats'][3].update(action='The right woman looks at her friend.',action_zh='右侧女子看向朋友。')
        result=build_prompt(DialoguePlan.model_validate(silent),picture_count=3)
        self.assertIn('From 1 to 2 seconds: The right woman ties the vine.',result)
        self.assertNotIn('<d>',result)
        for kind in ('gap','overlap','cross_turn','split_turn','reverse','extra'):
            bad=copy.deepcopy(value);rows=bad['performance_beats']
            if kind=='gap':rows[1]['start']=1.1
            elif kind=='overlap':rows[1]['start']=0.9
            elif kind=='cross_turn':rows[2]['end']=3.1
            elif kind=='split_turn':
                rows[3]['end']=4;rows.insert(4,{**rows[3],'start':4,'end':5})
            elif kind=='reverse':rows[1]['end']=0.5
            else:rows.append({**rows[-1],'start':6,'end':7})
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                DialoguePlan.model_validate(bad)

    def test_post_speech_blowing_is_not_overridden_by_blanket_closed_mouth_rule(self):
        value=data(); value['performance_beats']=beats(DialoguePlan.model_validate(value))
        value['performance_beats'][-1].update(action='The woman on the left blows out the candle.',
                                            action_zh='左侧女子吹灭蜡烛。')
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        tail=prompt.split('From 5.5 seconds',1)[1]
        self.assertIn('blows out the candle',tail)
        self.assertIn('No articulated words',tail)
        self.assertNotIn('mouths stay closed',tail)
        self.assertNotIn('only vocal events',prompt)
        first=prompt.split('From 0.2 to 2.7 seconds,',1)[1].split('From 2.7',1)[0]
        self.assertIn('<Subject 1> listens with its mouth closed',first)
        self.assertIn('<Subject 2> alone articulates the entire utterance',first)
        self.assertEqual(prompt.count('<d>'),2)
        value['turns']=[]
        value['performance_beats']=[dict(start=0,end=6,action='The woman blows out the candle.',action_zh='女子吹灭蜡烛。')]
        silent=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        self.assertIn('blows out the candle',silent)
        self.assertIn('No speech, narration',silent)
        self.assertNotIn('closed lips',silent)
        self.assertNotIn('<d>',silent)

    def test_each_action_is_compiled_with_its_utterance_or_silent_gap(self):
        value=data();plan=DialoguePlan.model_validate(value)
        value['performance_beats']=beats(plan)
        value['performance_beats'][1].update(action='The woman on the right shows the cup.',action_zh='右侧女子举起杯子给对方看。')
        value['performance_beats'][-1].update(action='The woman on the left steps away from the table.',action_zh='左侧女子离开桌边。')
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        first=prompt.split('From 0.2 to 2.7 seconds,')[1].split('From 2.7')[0]
        self.assertIn('shows the cup',first);self.assertIn('杯子还烫',first)
        self.assertNotIn('steps away',first)
        self.assertIn('steps away',prompt.split('From 5.5 seconds')[1])
        self.assertEqual(prompt.count('<d>'),2)

    def test_gaps_overlaps_wrong_language_and_markup_are_rejected(self):
        original=data();original['performance_beats']=beats(DialoguePlan.model_validate(original))
        changes=[lambda v:v['performance_beats'].pop(),
                 lambda v:v['performance_beats'][1].update(start=0),
                 lambda v:v['performance_beats'][1].update(end=3),
                 lambda v:v['performance_beats'][0].update(action_zh='English only'),
                 lambda v:v['performance_beats'][0].update(action='中文内部指令'),
                 lambda v:v['performance_beats'][0].update(action_zh='<d>额外台词</d>')]
        for change in changes:
            value=copy.deepcopy(original);change(value)
            with self.subTest(value=value),self.assertRaises(ValueError):DialoguePlan.model_validate(value)

    def test_legacy_serialization_is_unchanged_and_silent_scene_has_one_interval(self):
        value=data();self.assertNotIn('performance_beats',validate_dialogue_draft(value))
        value['turns']=[];plan=DialoguePlan.model_validate(value)
        self.assertEqual(performance_intervals(plan.duration,plan.turns),[(0,6)])
        value['performance_beats']=beats(plan)
        prompt=build_prompt(DialoguePlan.model_validate(value),picture_count=3)
        self.assertIn('Both friends stay beside the table',prompt);self.assertNotIn('<d>',prompt)

    def test_preparation_changes_only_actions_and_reuses_saved_timeline(self):
        original=data();shot={'native_dialogue':original,'action':'已舍弃的动作。',
                              'video_prompt':'两位朋友看着杯子。'}
        ask=Mock(return_value={'scene':original['scene'], 'performance_beats':beats(DialoguePlan.model_validate(original))})
        result=complete_performance(shot,ask=ask)
        payload=ask.call_args.args[1]
        self.assertEqual(payload['approved_video_prompt'],shot['video_prompt'])
        self.assertNotIn('approved_action_zh',payload)
        self.assertNotIn('opening_zh',payload)
        self.assertEqual(shot['native_dialogue'],original)
        self.assertEqual(result['turns'],validate_dialogue_draft(original)['turns'])
        self.assertEqual(result['speakers'],validate_dialogue_draft(original)['speakers'])
        again=complete_performance({'native_dialogue':result},ask=ask)
        self.assertEqual(result,again);ask.assert_called_once()
        broken=Mock(return_value={'scene':original['scene'], 'performance_beats':[]})
        with self.assertRaises(ValueError):complete_performance(shot,ask=broken)
        self.assertNotIn('performance_beats',shot['native_dialogue'])

    def test_legacy_scene_actions_move_to_timeline_without_changing_prop_boundaries(self):
        value=data()
        suffix=(' Object identity constraints: A blue ceramic cup. Prop state continuity: '
                'Opening physical state: The cup is on the table. Ending physical state: The cup is in her hand.')
        value['scene']='Two women sit beside a table. The right woman lifts the cup.' + suffix
        planned=beats(DialoguePlan.model_validate(value))
        planned[1].update(action='The right woman lifts the cup.',action_zh='右侧女子拿起杯子。')
        original=copy.deepcopy(value)
        answer=dict(scene='Two women sit beside a table.',performance_beats=planned)
        result=complete_performance({'native_dialogue':value},ask=Mock(return_value=answer))
        self.assertEqual(result['scene'],answer['scene']+suffix)
        self.assertEqual(result['turns'],validate_dialogue_draft(value)['turns'])
        self.assertEqual(result['speakers'],validate_dialogue_draft(value)['speakers'])
        self.assertEqual(value,original)
        prompt=build_prompt(DialoguePlan.model_validate(result),picture_count=3)
        self.assertEqual(prompt.count('The right woman lifts the cup.'),1)
        for bad in ('Wrong '+suffix,'中文场景',None):
            with self.subTest(scene=bad),self.assertRaises(ValueError):
                complete_performance({'native_dialogue':value},ask=Mock(return_value={**answer,'scene':bad}))
        self.assertEqual(value,original)

if __name__=='__main__':unittest.main()
