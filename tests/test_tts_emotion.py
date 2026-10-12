import copy
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from backend.app.h3_dialogue import Turn
from backend.app import tts_emotion as emotion
from backend.app import video_external_narration as narration
from tests.test_video_external_narration import plan


class EmotionTests(unittest.TestCase):
    def test_legacy_shape_and_strict_limits(self):
        old = dict(character_id='pc_'+'a'*32, text='真的吗？', start=0, end=2,
                   delivery='naturally and clearly')
        self.assertEqual(Turn.model_validate(old).model_dump(), old)
        for bad in ('excited', None):
            with self.assertRaises(ValidationError):
                Turn.model_validate({**old, 'emotion': bad})
        for bad in (-.1, 1.1, float('nan'), float('inf')):
            with self.assertRaises(ValidationError):
                Turn.model_validate({**old, 'emotion_strength': bad})

    def test_automatic_manual_neutral_and_ambiguous_delivery(self):
        self.assertEqual(emotion.resolve({'delivery':'surprised but speaking quietly'})['emotion'], 'surprised')
        for delivery in ('naturally and clearly', 'not angry', 'without surprise', 'happy but sad', 'warm and mature'):
            self.assertEqual(emotion.resolve({'delivery':delivery})['emotion'], 'neutral')
        # Spoken words are not an instruction to the voice model.
        self.assertEqual(emotion.resolve({'text':'我很生气！','delivery':'naturally'})['emotion'], 'neutral')
        control=emotion.resolve({'emotion':'neutral','delivery':'angrily'})
        self.assertEqual(emotion.runner_arguments(control), [])
        self.assertEqual(emotion.resolve({'emotion':'angry','emotion_strength':0})['emotion'], 'neutral')
        self.assertEqual(emotion.runner_arguments(emotion.resolve({'emotion':'sad','emotion_strength':.4})),
                         ['--tts-emotion','sad','--tts-emotion-weight','0.4'])

    def test_synthesis_keeps_adopted_reference_and_passes_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'line';output=folder/'segments/segment_0001.wav'
            reference=root/'adopted.wav'
            def run(command, **kwargs):
                self.assertEqual(command[command.index('--tts-voice-path')+1],str(reference))
                self.assertEqual(command[command.index('--tts-emotion')+1],'surprised')
                self.assertEqual(command[command.index('--tts-emotion-weight')+1],'0.35')
                output.parent.mkdir(parents=True)
                with wave.open(str(output),'wb') as wav:
                    wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(b'\0\0'*24000)
                return type('Result',(),{'returncode':0})()
            with patch.object(narration.subprocess,'run',side_effect=run):
                narration.synthesize(1,root,'真的吗？',{'path':reference},folder,
                                     emotion=emotion.resolve({'emotion':'surprised'}))

    def test_edit_resynthesizes_only_changed_line_and_retimes_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);value=plan();original=copy.deepcopy(value)
            record={'id':'a'*32,'creation_parameters':{'narration_route':'external_tts'}}
            voices=[dict(character_id=value['speakers'][-1]['character_id'],voice_id='qwen3:'+'3'*32,sha256='a'*64)]
            def synth(owner,path,text,voice,folder,**kwargs):
                folder.mkdir(parents=True,exist_ok=True);file=folder/'voice.wav'
                samples=24000 * (3 if kwargs else 1)
                with wave.open(str(file),'wb') as wav:
                    wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(b'\0\0'*samples)
                return file
            with patch.object(narration,'synthesize',side_effect=synth) as call:
                first=narration.prepare(1,root,record,value,voices,allow_synthesis=True)
                self.assertEqual(call.call_count,2)
                value['turns'][0].update(emotion='surprised',emotion_strength=.35)
                self.assertIsNone(narration.prepare(1,root,record,value,voices))
                self.assertEqual(call.call_count,2) # Preflight is read only.
                second=narration.prepare(1,root,record,value,voices,allow_synthesis=True)
                self.assertEqual(call.call_count,3)
                self.assertNotEqual(first['clips'][0]['key'],second['clips'][0]['key'])
                self.assertEqual(first['clips'][1]['key'],second['clips'][1]['key'])
                self.assertGreater(second['clips'][1]['start'],first['clips'][1]['start'])
                self.assertEqual(second['clips'][0]['end']-second['clips'][0]['start'],3)
                self.assertEqual(value['turns'][0]['text'],original['turns'][0]['text'])
                value['turns'][0].update(emotion='neutral')
                narration.prepare(1,root,record,value,voices,allow_synthesis=True)
                self.assertEqual(call.call_count,3) # Explicit natural reuses original.


if __name__ == '__main__':
    unittest.main()
