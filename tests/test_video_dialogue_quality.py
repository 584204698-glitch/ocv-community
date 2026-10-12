import copy
import json
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.app import video_dialogue_quality as quality
from tests.test_h3_dialogue import data


def transcript():
    return {'segments':[
        {'text':'杯子还烫，小心一点。','words':[{'text':'杯子还烫','start':.2,'end':1.3}, {'text':'小心一点','start':1.4,'end':2.5}]},
        {'text':'知道了，我等一会儿。','words':[{'text':'知道了','start':3,'end':3.9}, {'text':'我等一会儿','start':4,'end':6.2}]}]}


class NativeSpeechQualityTests(unittest.TestCase):
    def asr_adapter(self, start, end):
        text='杯子还烫，小心一点。'
        word=SimpleNamespace(word=text,start=start,end=end,probability=end)
        segment=SimpleNamespace(text=text,start=start,end=end,words=[word])
        with patch.object(quality,'_model') as model:
            model.return_value.transcribe.return_value=([segment],SimpleNamespace(language='zh'))
            return quality.transcribe('unused-no-audio','unused-no-model')

    def test_asr_adapter_normalizes_numpy_scalars_without_accepting_quality(self):
        import numpy as np
        plan=data();plan['turns']=plan['turns'][:1]
        for scalar in (np.float32,np.float64,np.int64):
            with self.subTest(scalar=scalar):
                heard=self.asr_adapter(scalar(1),scalar(2))
                segment=heard['segments'][0];word=segment['words'][0]
                for value in (segment['start'],segment['end'],word['start'],word['end'],word['probability']):
                    self.assertIs(type(value),float)
                self.assertEqual(json.loads(json.dumps(heard)),heard)
                review=quality.compare_transcript(plan,heard,6)
                self.assertTrue(review['text_matches']);self.assertEqual(review['issues'],[])
                for key in ('speaker_verified','timbre_verified','quality_accepted'):
                    self.assertFalse(review[key])

    def test_asr_adapter_keeps_invalid_times_rejected(self):
        import numpy as np
        for start,end in [(np.float64('nan'),2),(1,np.float64('inf')),
                          (np.float32(-.1),2),(1,np.float64(8)),(2,2),(2,1),
                          (True,2),(np.bool_(True),2),('1',2),(None,2)]:
            with self.subTest(start=start,end=end):
                heard=self.asr_adapter(start,end)
                with self.assertRaises(ValueError):quality.compare_transcript(data(),heard,6)

    def equivalent_case(self, expected='楼道屏幕滚动播，语音还会提醒。', actual='楼道屏幕滚动波，语音还会提醒。'):
        value=data();value['turns']=value['turns'][:1]
        value['turns'][0].update(text=expected,start=0,end=6)
        heard={'segments':[{'text':actual,'words':[{'text':actual,'start':.3,'end':5.5}]}]}
        return value,heard

    def test_modern_single_reading_homophone_keeps_spelling_difference_and_no_acceptance(self):
        plan,heard=self.equivalent_case()
        report=quality.compare_transcript(plan,heard,6)
        self.assertTrue(report['text_matches'])
        self.assertFalse(report['orthographic_text_matches'])
        self.assertEqual(report['matching_basis'],'equivalent_single_reading_toned_syllables')
        self.assertEqual(report['issues'],[])
        self.assertEqual(report['turns'][0]['text'],plan['turns'][0]['text'])
        self.assertEqual(report['turns'][0]['start'],.3)
        self.assertEqual(report['word_differences'][0]['expected'],'播')
        self.assertEqual(report['word_differences'][0]['recognized'],'波')
        for key in ('quality_accepted','speaker_verified','timbre_verified'):
            self.assertFalse(report[key])

    def test_missing_added_different_tone_polyphonic_numbers_and_english_remain_strict(self):
        cases=[('你真的把杯子放回去了？','你的把杯子放回去了？'),
               ('发到手机上？','发到机上？'),
               ('滚动播。','滚动波谢谢。'),
               ('杯子还烫。','杯子还汤。'),
               ('行动开始。','型动开始。'),
               ('重新播放。','仲新播放。'),
               ('播放2次。','波放两次。'),
               ('播放A。','波放B。')]
        for expected,actual in cases:
            with self.subTest(expected=expected,actual=actual):
                plan,heard=self.equivalent_case(expected,actual)
                report=quality.compare_transcript(plan,heard,6)
                self.assertFalse(report['text_matches'])
                self.assertEqual(report['matching_basis'],'mismatch')
                self.assertEqual(report['issues'][0]['code'],'speech_mismatch')

    def test_frozen_full_name_spelling_keeps_evidence_without_quality_acceptance(self):
        plan,heard=self.equivalent_case('路飞，帮帮我。','陸菲，帮帮我。')
        plan['speakers'][1]['name']='路飞'
        review=quality.compare_transcript(plan,heard,6)
        self.assertTrue(review['text_matches'])
        self.assertFalse(review['orthographic_text_matches'])
        self.assertEqual(review['matching_basis'],'frozen_character_name_readings')
        self.assertEqual(review['recognized'],'陸菲，帮帮我。')
        self.assertEqual(review['issues'],[])
        self.assertIn('供试听',review['matching_note'])
        for key in ('quality_accepted','speaker_verified','timbre_verified'):
            self.assertFalse(review[key])

    def test_name_equivalence_never_relaxes_other_text_or_guesses_adopted_polyphones(self):
        cases=[('路飞帮帮我','陸菲帮帮你',('路飞',)),
               ('路飞帮帮我','陸菲帮帮我谢谢',('路飞',)),
               ('路飞帮帮我','魯菲帮帮我',('路飞',)),
               ('路飞帮帮我','陸菲帮帮我',()),
               ('乐乐帮帮我','月月帮帮我',('乐乐',))]
        for expected,actual,names in cases:
            with self.subTest(actual=actual,names=names):
                self.assertIsNone(quality.spoken_match_basis(expected,actual,known_names=names))

    def test_missing_pinyin_dependency_fails_even_for_modern_dictionary_corrections(self):
        plan,heard=self.equivalent_case()
        with patch.dict(sys.modules,{'pypinyin':None}):
            report=quality.compare_transcript(plan,heard,6)
            self.assertFalse(report['text_matches'])
            self.assertTrue(report['issues'])
            exact=copy.deepcopy(heard);exact['segments'][0]['text']=plan['turns'][0]['text']
            exact['segments'][0]['words'][0]['text']=plan['turns'][0]['text']
            self.assertFalse(quality.compare_transcript(plan,exact,6)['issues'])

    def test_equivalent_words_still_require_complete_word_timestamps(self):
        plan,heard=self.equivalent_case()
        for words in ([],[dict(text='楼道屏幕滚动波',start=.3,end=3)]):
            with self.subTest(words=words):
                heard['segments'][0]['words']=words
                report=quality.compare_transcript(plan,heard,6)
                self.assertTrue(report['text_matches'])
                self.assertEqual(report['issues'][0]['code'],'alignment_missing')

    def test_equivalent_word_times_reject_nonfinite_outside_and_wrong_types(self):
        for start,end in [(float('nan'),5),(0,float('inf')),(-.1,5),(.3,6.5),
                          (True,5),('0.3',5),(2,1)]:
            with self.subTest(start=start,end=end):
                plan,heard=self.equivalent_case()
                heard['segments'][0]['words'][0].update(start=start,end=end)
                with self.assertRaises(ValueError):quality.compare_transcript(plan,heard,6)

    def test_equivalent_words_cannot_hide_overlap_inside_a_turn(self):
        plan,heard=self.equivalent_case()
        heard['segments'][0]['words']=[dict(text='楼道屏幕滚动波',start=.3,end=3),
                                      dict(text='语音还会提醒',start=2.5,end=5.5)]
        report=quality.compare_transcript(plan,heard,6)
        self.assertTrue(report['text_matches'])
        self.assertEqual(report['issues'][0]['code'],'alignment_overlap')

    def test_cached_homophone_transcript_is_reinterpreted_without_new_asr(self):
        plan,heard=self.equivalent_case()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'original.mp4').write_bytes(b'original')
            shot=dict(kind='video',video='original.mp4',video_request=dict(dialogue=dict(plan=plan)))
            with patch('backend.app.pipeline.probe_media_duration',return_value=6), \
                 patch.object(quality,'transcribe',return_value=heard) as recognize:
                first=quality.screen_dialogue(root,shot)
                cache=root/'assets/dialogue_reviews'/(first['fingerprint']+'.json')
                saved=json.loads(cache.read_text(encoding='utf8'))
                saved['review'].update(text_matches=False,matching_basis='mismatch',quality_accepted=True)
                cache.write_text(json.dumps(saved),encoding='utf8')
                second=quality.screen_dialogue(root,shot)
            self.assertEqual(recognize.call_count,1)
            self.assertTrue(second['review']['text_matches'])
            self.assertFalse(second['review']['quality_accepted'])
            self.assertFalse(second['review']['orthographic_text_matches'])

    def test_word_differences_localize_replacement_omission_and_extra_without_accepting(self):
        plan=data();plan['turns']=plan['turns'][:1]
        plan['turns'][0]['text']='你真的把杯子放回去了？'
        for heard,kind,a,b in [
            ('你真是把杯子放回去了','replace','的','是'),
            ('你真的把杯子放去了','delete','回',''),
            ('你真的把杯子放回去了谢谢','insert','','谢谢')]:
            report=quality.compare_transcript(plan,{'segments':[{'text':heard}]},6)
            self.assertFalse(report['text_matches']);self.assertFalse(report['quality_accepted'])
            diff=report['word_differences']
            self.assertEqual(len(diff),1)
            self.assertEqual((diff[0]['kind'],diff[0]['expected'],diff[0]['recognized']),(kind,a,b))
            self.assertEqual(report['issues'][0]['code'],'speech_mismatch')
            self.assertIn(a or b,report['issues'][0]['message'])
        empty=quality.compare_transcript(plan,{'segments':[]},6)
        self.assertFalse(empty['text_matches'])
        self.assertEqual(empty['word_differences'][0]['kind'],'delete')

    def test_spoken_pronoun_spelling_is_not_a_missing_or_wrong_syllable(self):
        plan=data();plan['turns'][0]['text']='它们还烫，小心一点。'
        value=transcript();value['segments'][0]['text']='他们还烫，小心一点。'
        value['segments'][0]['words'][0]['text']='他们还烫'
        result=quality.compare_transcript(plan,value,6.58)
        self.assertTrue(result['text_matches']);self.assertFalse(result['issues'])
        self.assertFalse(result['orthographic_text_matches'])
        self.assertEqual(result['matching_basis'],'equivalent_spoken_pronouns')
        self.assertIn('他们',result['recognized'])
        self.assertEqual(result['turns'][0]['text'],plan['turns'][0]['text'])
        self.assertFalse(result['speaker_verified']);self.assertFalse(result['quality_accepted'])

    def test_pronoun_equivalence_does_not_hide_extra_missing_or_other_changed_words(self):
        plan=data();plan['turns'][0]['text']='它们还烫，小心一点。'
        for heard in ('他们还烫，小心一点。再来一次。','他还烫，小心一点。',
                      '他们不烫，小心一点。','它们还汤，小心一点。'):
            with self.subTest(heard=heard):
                value=transcript();value['segments'][0]['text']=heard
                result=quality.compare_transcript(plan,value,6.58)
                self.assertFalse(result['text_matches']);self.assertTrue(result['issues'])

    def test_action_only_beat_still_flags_unexpected_speech_without_accepting_empty_asr_as_quality(self):
        plan=data();plan['turns']=[]
        result=quality.compare_transcript(plan,{'segments':[]},6.58)
        self.assertTrue(result['text_matches']);self.assertEqual(result['turns'],[])
        self.assertFalse(result['quality_accepted'])
        bad=quality.compare_transcript(plan,transcript(),6.58)
        self.assertEqual(bad['issues'][0]['code'],'speech_mismatch')

    def test_actual_tail_and_speaker_attribution_are_not_overclaimed(self):
        result=quality.compare_transcript(data(),transcript(),6.58)
        self.assertTrue(result['text_matches']);self.assertFalse(result['issues'])
        self.assertEqual(result['turns'][1]['end'],6.2)
        self.assertEqual(result['turns'][0]['character_id'],data()['turns'][0]['character_id'])
        self.assertFalse(result['speaker_verified']);self.assertFalse(result['timbre_verified'])

    def test_wrong_missing_extra_and_foreign_words_fail_without_forging_target(self):
        for text in ('几次还烫，小心一点。','杯子还烫。','杯子还烫，小心一点。嗯哼','The cup is hot.'):
            value=transcript();value['segments'][0]['text']=text
            result=quality.compare_transcript(data(),value,6.58)
            self.assertFalse(result['text_matches']);self.assertTrue(result['issues'])
            self.assertIn(text,result['recognized']);self.assertFalse(result['turns'])

    def test_word_evidence_missing_or_cross_turn_overlap_blocks_alignment(self):
        value=transcript();value['segments'][0]['words']=[]
        self.assertEqual(quality.compare_transcript(data(),value,6.58)['issues'][0]['code'],'alignment_missing')
        value=transcript();value['segments'][1]['words'][0]['start']=2
        self.assertEqual(quality.compare_transcript(data(),value,6.58)['issues'][0]['code'],'alignment_overlap')

    def test_cached_transcription_rechecked_and_invalidated_by_video_and_words(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);clip=path/'clip.mp4';clip.write_bytes(b'first-video')
            shot={'kind':'video','video':'clip.mp4','video_request':{'dialogue':{'plan':data()}}}
            with patch('backend.app.pipeline.probe_media_duration',return_value=6.58), \
                 patch.object(quality,'transcribe',return_value=transcript()) as recognize:
                first=quality.screen_dialogue(path,shot)
                cache=next((path/'assets/dialogue_reviews').glob('*.json'))
                tampered=json.loads(cache.read_text(encoding='utf8'));tampered['review']['quality_accepted']=True
                cache.write_text(json.dumps(tampered),encoding='utf8')
                self.assertFalse(quality.screen_dialogue(path,shot)['review']['quality_accepted'])
                self.assertEqual(recognize.call_count,1)
                clip.write_bytes(b'second-video')
                second=quality.screen_dialogue(path,shot)
                self.assertNotEqual(first['fingerprint'],second['fingerprint'])
                self.assertEqual(recognize.call_count,2)
                shot['video_request']['dialogue']['plan']['turns'][0]['text']='杯子不烫，小心一点。'
                self.assertTrue(quality.screen_dialogue(path,shot)['review']['issues'])

    def test_automatic_check_reports_shot_and_preserves_mismatch(self):
        report={'review':{'issues':[{'message':'错词待核对'}],'recognized':'几次还烫'}}
        with patch.object(quality,'screen_dialogue',return_value=report), self.assertRaisesRegex(ValueError,'第1镜.*几次还烫'):
            quality.check_native_speech(Path('.'),{'shots':[{}]})
