import tempfile
import unittest
import wave
from pathlib import Path
from backend.app.subtitle_timing import bound_subtitle_times, wav_duration
from backend.app.tts_editor import _commit_canonical_subtitle_timeline, _srt_entries


class SubtitleTimingTests(unittest.TestCase):
    def test_real_failure_preserves_earlier_cues_and_caps_only_final_end(self):
        rows=[{'start':0.14,'end':25.9,'text':'earlier'},{'start':25.9,'end':57.76,'text':'我的嘴都装满啦！'}]
        actual=bound_subtitle_times(rows,27.91)
        self.assertEqual(actual[0],rows[0]);self.assertEqual(actual[-1]['end'],27.91)
        self.assertEqual(rows[-1]['end'],57.76)
        self.assertEqual(actual[-1]['text'],rows[-1]['text'])
        with self.assertRaisesRegex(ValueError,'超过实际配音'):
            bound_subtitle_times(rows,27.91,repair_final_end=False)

    def test_invalid_start_or_middle_cue_does_not_drop_text_or_redistribute_time(self):
        for rows in [[{'start':28,'end':30}], [{'start':0,'end':30},{'start':30,'end':40}],
                     [{'start':0,'end':float('nan')}],[{'start':-1,'end':1}]]:
            with self.assertRaises(ValueError):bound_subtitle_times(rows,27.91)

    def test_review_repairs_old_srt_keeps_backup_and_builds_matching_timeline(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'input').mkdir();(root/'other').mkdir()
            with wave.open(str(root/'input/配音.wav'),'wb') as stream:
                stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(b'\0\0'*32000)
            original='1\n00:00:00,100 --> 00:00:32,000\n保留这句\n'
            srt=root/'other/最终字幕.srt';srt.write_text(original,encoding='utf-8')
            _commit_canonical_subtitle_timeline(root)
            entries=_srt_entries(srt);self.assertEqual(entries[0]['end'],2)
            self.assertEqual(wav_duration(root/'input/配音.wav'),2)
            saved=json.loads((root/'other/画面时间线.json').read_text(encoding='utf-8'))
            self.assertEqual(saved[0]['end'],2);self.assertEqual(saved[0]['text_content'],'保留这句')
            backups=list((root/'other').glob('字幕时间修复前-*.srt'))
            self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_text(encoding='utf-8'),original)
            _commit_canonical_subtitle_timeline(root)
            self.assertEqual(len(list((root/'other').glob('字幕时间修复前-*.srt'))),1)


if __name__=='__main__':unittest.main()
