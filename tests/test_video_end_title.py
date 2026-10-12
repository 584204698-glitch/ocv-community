"""Explicit end titles must not modify earlier frames or extend a film."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from backend.app import video_export as export


class EndTitleTests(unittest.TestCase):
    def test_default_does_not_add_title_or_work_files(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(export._end_title_filter({}, folder, 480, 270, 5), '')
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_literal_text_cannot_inject_ass_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            record = {'export_settings': {'end_title_text': r'{\pos(0,0)}梦镜·小林'}}
            value = export._end_title_filter(record, folder, 480, 270, 5)
            self.assertIn('gte(t,3.500000000)', value)
            text = (Path(folder) / 'end-title.ass').read_text(encoding='utf8')
            self.assertIn('0:00:03.50,0:00:05.00', text)
            self.assertNotIn(r'{\pos', text)
            self.assertIn('梦镜·小林', text)

    def test_real_frames_keep_original_opening_and_show_title_at_end(self):
        binary = export.ffmpeg_binary()
        if not Path(binary).is_file() and not shutil.which(binary):
            self.skipTest('FFmpeg unavailable')
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'title.mp4'
            record = {'export_settings': {'end_title_text': 'End title'}}
            vf = 'format=yuv420p' + export._end_title_filter(record, folder, 480, 270, 3)
            subprocess.run([binary, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                            'color=c=red:s=480x270:r=30:d=3', '-vf', vf,
                            '-threads', '2', '-c:v', 'libx264', str(output)], check=True,
                           capture_output=True)
            self.assertAlmostEqual(export.probe_media_duration(output), 3, places=1)
            for time in (.5, 2.5):
                target = Path(folder) / f'{time}.png'
                subprocess.run([binary, '-v', 'error', '-y', '-ss', str(time), '-i',
                                str(output), '-frames:v', '1', '-threads', '2', str(target)],
                               check=True, capture_output=True)
            with Image.open(Path(folder) / '0.5.png') as opening:
                r, g, b = opening.convert('RGB').getpixel((10, 10))
                self.assertGreater(r, 200)
                self.assertLess(g+b, 30)
            with Image.open(Path(folder) / '2.5.png') as ending:
                pixels = list(ending.convert('RGB').getdata())
                self.assertLess(sum(pixels[10*480+10]), 30)
                self.assertGreater(sum(1 for r,g,b in pixels if min(r,g,b)>180), 30)
