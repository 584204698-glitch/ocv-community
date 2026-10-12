import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.app import video_external_alignment as alignment
from backend.app import video_external_dialogue as exact
from tests.test_video_external_narration import plan
from tests.test_video_external_dialogue import audio


class AlignmentTests(unittest.TestCase):
    def failed_fixture(self, path):
        source=path/'raw.mp4';source.write_bytes(b'raw')
        visual,clips=exact.retime(plan(),{i:dict(duration=1.5) for i in range(3)})
        data=dict(plan=visual,clips=clips)
        raw=dict(segments=[dict(text='漏词',words=[dict(text='漏词',start=1,end=2)])])
        return source,data,raw

    def test_unverified_cache_reuses_raw_transcript_and_rejects_forged_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);source,data,raw=self.failed_fixture(path)
            with patch('backend.app.video_dialogue_quality.transcribe',return_value=raw) as recognize, \
                 patch('backend.app.video_export._run') as render:
                alignment.calibrate(path,source,data,8)
                receipt=next((path/'timing').glob('*.json'))
                saved=json.loads(receipt.read_text(encoding='utf8'))
                saved['review'].update(issues=[],text_matches=True,quality_accepted=True)
                receipt.write_text(json.dumps(saved),encoding='utf8')
                output,report=alignment.calibrate(path,source,data,8)
            self.assertEqual(recognize.call_count,1)
            self.assertEqual(output,source);render.assert_not_called()
            self.assertEqual(report['status'],'unverified')
            self.assertTrue(report['native_transcript_reused'])
            self.assertFalse(report['review']['text_matches'])
            self.assertFalse(report['review']['quality_accepted'])
            self.assertNotIn('anchors',report)

    def test_current_comparator_rechecks_saved_evidence(self):
        from backend.app import video_dialogue_quality as quality
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);source,data,raw=self.failed_fixture(path)
            alignment.calibrate(path,source,data,8,recognize=lambda *_:raw)
            with patch.object(quality,'compare_transcript',wraps=quality.compare_transcript) as compare, \
                 patch.object(quality,'transcribe',side_effect=AssertionError('reuse evidence')):
                _,report=alignment.calibrate(path,source,data,8)
            compare.assert_called_once_with(data['plan'],raw,8)
            self.assertEqual(report['transcript'],raw)

    def test_explicit_recognizer_can_refresh_unverified_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);source,data,raw=self.failed_fixture(path)
            alignment.calibrate(path,source,data,8,recognize=lambda *_:raw)
            replacement=dict(segments=[dict(text='新的识别',words=[])])
            with patch('backend.app.video_dialogue_quality.transcribe') as default:
                _,report=alignment.calibrate(path,source,data,8,recognize=lambda *_:replacement)
            default.assert_not_called()
            self.assertEqual(report['transcript'],replacement)
            self.assertNotIn('native_transcript_reused',report)

    def test_missing_invalid_or_failed_recognition_can_retry(self):
        for defect in ('missing','malformed','bad_timestamp','recognizer_error'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp);source,data,raw=self.failed_fixture(path)
                if defect=='recognizer_error':
                    with patch('backend.app.video_dialogue_quality.transcribe',side_effect=RuntimeError('unavailable')):
                        alignment.calibrate(path,source,data,8)
                else:
                    alignment.calibrate(path,source,data,8,recognize=lambda *_:raw)
                    receipt=next((path/'timing').glob('*.json'))
                    saved=json.loads(receipt.read_text(encoding='utf8'))
                    if defect=='missing':saved.pop('transcript')
                    elif defect=='malformed':saved['transcript']={'segments':'bad'}
                    else:saved['transcript']['segments'][0]['words'][0]['end']=99
                    receipt.write_text(json.dumps(saved),encoding='utf8')
                with patch('backend.app.video_dialogue_quality.transcribe',return_value=raw) as recognize:
                    _,report=alignment.calibrate(path,source,data,8)
                recognize.assert_called_once()
                self.assertEqual(report['transcript'],raw)
                self.assertEqual(report['status'],'unverified')

    def test_changed_basis_never_reuses_original_transcript(self):
        for changed in ('source','plan','clips','model','duration'):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp);source,data,raw=self.failed_fixture(path)
                with patch('backend.app.video_dialogue_quality.transcribe',return_value=raw) as recognize:
                    alignment.calibrate(path,source,data,8)
                    if changed=='source':source.write_bytes(b'changed original')
                    elif changed=='plan':data['plan']['soundscape']='Changed room ambience.'
                    elif changed=='clips':data['clips'][0]['start']+=.1
                    with patch.dict(alignment.os.environ,{'OCV_DIALOGUE_ASR_MODEL':'different-model'} if changed=='model' else {}):
                        alignment.calibrate(path,source,data,9 if changed=='duration' else 8)
                    self.assertEqual(recognize.call_count,2)

    def test_shorter_first_line_does_not_overstretch_the_next_silent_gap(self):
        anchors=[(2.,6.12,2.,5.48),(6.58,8.48,6.71,8.43)]
        result=alignment.bounded_gap_anchors(13,anchors)
        self.assertAlmostEqual(result[0][2],2)
        self.assertAlmostEqual(result[1][2],6.4)
        self.assertAlmostEqual(result[1][3]-result[1][2],1.72)
        self.assertTrue(all(.5<=p['scale']<=2 for p in alignment.intervals(13,result)))
        with self.assertRaises(ValueError):
            alignment.bounded_gap_anchors(13,[(2,3,2,10)])
    def test_late_reply_moves_audio_without_speeding_up_silent_picture(self):
        clips=[dict(kind='character',character_id='child',start=.5,duration=3.04),
               dict(kind='character',character_id='grandma',start=4.29,duration=1.36)]
        before=copy.deepcopy(clips)
        visible=[(0,dict(character_id='child',start=.5)),(1,dict(character_id='grandma',start=4.29))]
        anchors=[(.72,3.66,.6,3.55),(6.21,7.41,4.39,5.65)]
        with self.assertRaises(ValueError):alignment.intervals(8,anchors)
        starts=alignment.place_audio(clips,visible,anchors,8)
        self.assertEqual(starts,[.62,6.11])
        self.assertEqual(clips,before)
        with self.assertRaises(ValueError):
            alignment.place_audio(clips,visible,[(.72,3.66,.6,3.55),(6.21,6.6,4.39,5.65)],8)
        with self.assertRaises(ValueError):
            alignment.place_audio(clips,visible,anchors,7)
        narration=dict(kind='narrator',character_id='narrator',start=6,duration=1)
        with self.assertRaises(ValueError):alignment.place_audio(clips+[narration],visible,anchors,8)

    def test_piecewise_mapping_aligns_each_boundary_keeps_full_duration(self):
        pieces=alignment.intervals(10,[(2,4,3,5),(7,8,6.5,7.5)])
        self.assertEqual(pieces[0]['start'],0)
        self.assertEqual(pieces[-1]['target_end'],10)
        self.assertEqual(pieces[2]['target_end'],6.5)
        self.assertEqual(alignment.intervals(10,[(0,2,0,3)])[-1]['end'],10)
        for anchors in ([(3,2,3,4)],[(1,2,7,8)],[(2,3,2,3),(2.5,4,4,5)]):
            with self.assertRaises(ValueError):alignment.intervals(10,anchors)

    def test_tts_speech_span_excludes_silence_without_rewriting_words(self):
        value={'segments':[dict(text='真的吗',words=[dict(text='真的',start=.1,end=.5),dict(text='吗',start=.5,end=.8)])]}
        self.assertEqual(alignment.speech_span(value,'真的吗？',1),(.1,.8))
        with self.assertRaises(ValueError):alignment.speech_span(value,'真的吗真的吗',1)

    def test_tts_written_homophone_is_only_timing_evidence(self):
        value={'segments':[dict(text='这片指正合适',words=[dict(text='这片指正合适',start=.3,end=1.6)])]}
        evidence={}
        self.assertEqual(alignment.speech_span(value,'这片纸正合适',2,evidence=evidence),(.3,1.6))
        self.assertEqual(evidence['matching_basis'],'phonetic_timing_only')
        self.assertFalse(evidence['text_verified']);self.assertFalse(evidence['quality_accepted'])
        self.assertIsNone(alignment.tts_timing_match('省的吗','真的吗'))
        self.assertIsNone(alignment.tts_timing_match('楚岭','竹林'))
        self.assertIsNone(alignment.tts_timing_match('来','赖'))
        self.assertIsNone(alignment.tts_timing_match('这片纸合适','这片纸正合适'))
        self.assertIsNone(alignment.tts_timing_match('你好啊','你好'))
        self.assertIsNone(alignment.tts_timing_match('A1','A2'))

    def test_missing_phonetic_library_keeps_strict_tts_gate(self):
        import sys
        with patch.dict(sys.modules,{'pypinyin':None}):
            self.assertIsNone(alignment.tts_timing_match('纸','指'))

    def test_matching_late_speech_reuses_video_and_cached_audio_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);source=path/'raw.mp4';source.write_bytes(b'raw')
            visual,clips=exact.retime(plan(),{i:dict(duration=1.5) for i in range(3)})
            character=next(c for c in clips if c['kind']=='character')
            wav=audio(path/'tts',1.5)
            character.update(file=wav.relative_to(path).as_posix(),sha256=hashlib.sha256(wav.read_bytes()).hexdigest())
            data=dict(plan=visual,clips=[character]);before=copy.deepcopy(data)
            turn=visual['turns'][0]
            raw=dict(segments=[dict(text=turn['text'],start=5,end=6.3,words=[dict(text=turn['text'],start=5,end=6.3)])])
            tts=dict(segments=[dict(text=turn['text'],words=[dict(text=turn['text'],start=.1,end=1.4)])])
            with patch('backend.app.video_export._run') as render:
                output,report=alignment.calibrate(path,source,data,8,recognize=lambda file,_:raw if file==source else tts)
                self.assertEqual(report['status'],'audio_placed')
                self.assertEqual(report['audio_starts'],[4.9])
                self.assertEqual(output,source);render.assert_not_called()
                self.assertFalse(report['lip_sync_verified'])
                _,cached=alignment.calibrate(path,source,data,8,recognize=lambda *_:self.fail('cached receipt must be reused'))
                self.assertEqual(cached['audio_starts'],[4.9])
            self.assertEqual(data,before);self.assertEqual(source.read_bytes(),b'raw')

    def test_late_speech_with_different_length_preserves_starts_and_bounds_picture_speed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);source=path/'raw.mp4';source.write_bytes(b'raw')
            visual,clips=exact.retime(plan(),{i:dict(duration=1.5) for i in range(3)})
            character=next(c for c in clips if c['kind']=='character')
            wav=audio(path/'tts',1.5)
            character.update(file=wav.relative_to(path).as_posix(),sha256=hashlib.sha256(wav.read_bytes()).hexdigest())
            data=dict(plan=visual,clips=[character]);turn=visual['turns'][0]
            raw=dict(segments=[dict(text=turn['text'],start=5,end=6.9,words=[dict(text=turn['text'],start=5,end=6.9)])])
            tts=dict(segments=[dict(text=turn['text'],words=[dict(text=turn['text'],start=.1,end=1.4)])])
            def run(command,_):Path(command[-1]).write_bytes(b'aligned')
            with patch('backend.app.video_export._run',side_effect=run),patch('backend.app.video_export.probe_media_duration',return_value=8):
                output,report=alignment.calibrate(path,source,data,8,recognize=lambda file,_:raw if file==source else tts)
            self.assertEqual(report['status'],'calibrated')
            self.assertEqual(report['audio_starts'],[4.9])
            self.assertEqual(report['anchors'][0][2],5)
            self.assertTrue(all(.5<=piece['scale']<=2 for piece in report['intervals']))
            self.assertNotEqual(output,source);self.assertEqual(source.read_bytes(),b'raw')

    def test_calibrate_uses_actual_native_speech_and_exact_tts_with_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);source=path/'raw.mp4';source.write_bytes(b'raw')
            value=plan();voices={i:dict(duration=1.5) for i in range(3)}
            visual,clips=exact.retime(value,voices)
            character=next(c for c in clips if c['kind']=='character')
            wav=audio(path/'tts',1.5)
            character.update(file=wav.relative_to(path).as_posix(),sha256=hashlib.sha256(wav.read_bytes()).hexdigest())
            data=dict(plan=visual,clips=clips);before=copy.deepcopy(data)
            turn=visual['turns'][0];actual_start=turn['start']+.3;actual_end=turn['end']+.2
            transcript=dict(segments=[dict(text=turn['text'],start=actual_start,end=actual_end,words=[dict(text=turn['text'],start=actual_start,end=actual_end)])])
            tts=dict(segments=[dict(text=turn['text'],words=[dict(text=turn['text'],start=.1,end=1.4)])])
            def recognize(file,model):return transcript if file==source else tts
            def run(command,label):Path(command[-1]).write_bytes(b'aligned')
            with patch('backend.app.video_export._run',side_effect=run) as render,patch('backend.app.video_export.probe_media_duration',return_value=visual['duration']):
                output,report=alignment.calibrate(path,source,data,visual['duration'],recognize=recognize)
                self.assertEqual(report['status'],'calibrated')
                self.assertEqual(report['anchors'][0][2],turn['start']+.1)
                self.assertFalse(report['lip_sync_verified'])
                self.assertEqual(render.call_count,1)
                self.assertEqual(alignment.calibrate(path,source,data,visual['duration'],recognize=lambda *_:self.fail('must reuse evidence'))[0],output)
                self.assertEqual(render.call_count,1)
                output.write_bytes(b'corrupt')
                with self.assertRaises(ValueError):alignment.calibrate(path,source,data,visual['duration'])
            self.assertEqual(source.read_bytes(),b'raw');self.assertEqual(data,before)

    def test_missing_recognition_preserves_original_and_exposes_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);source=path/'raw.mp4';source.write_bytes(b'raw')
            visual,clips=exact.retime(plan(),{i:dict(duration=1.5) for i in range(3)})
            output,report=alignment.calibrate(path,source,dict(plan=visual,clips=clips),visual['duration'],recognize=lambda *_:(_ for _ in ()).throw(RuntimeError('model unavailable')))
            self.assertEqual(output,source);self.assertEqual(report['status'],'unverified')
            self.assertIn('model unavailable',report['warning'])
