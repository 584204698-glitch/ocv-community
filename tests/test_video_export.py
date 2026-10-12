"""Export isolation and real, local FFmpeg timeline checks; no model calls."""
import array
import copy
import hashlib
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from backend.app import video_export as export, video_studio as studio


class ExportFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ocv-export-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'workspace' / '1' / ('a' * 32)
        (self.path / 'assets').mkdir(parents=True)
        self.record = {
            'id': self.path.name, 'revision': 1, 'status': 'video_review', 'logs': [],
            'settings': {'name': 'Same project name', 'ratio': '16:9'},
            'audio': 'assets/audio.wav', 'scenes': [{'start': .3, 'end': 3.3}],
            'shots': [
                {'id': 'red', 'kind': 'video', 'video': 'red.mp4', 'video_status': 'completed',
                 'start': .3, 'end': 1.2},
                {'id': 'green', 'kind': 'static', 'image': 'green.png', 'start': 1.6, 'end': 2.1},
                {'id': 'blue', 'kind': 'video', 'video': 'blue.mp4', 'video_status': 'completed',
                 'start': 2.4, 'end': 2.9},
            ],
        }
        for name in ('red.mp4', 'blue.mp4'):
            (self.path / name).write_bytes(b'synthetic fixture')
        Image.new('RGB', (160, 90), 'green').save(self.path / 'green.png')
        (self.path / self.record['audio']).write_bytes(b'audio fixture')
        (self.path / 'assets/subtitles.srt').write_text(
            '1\n00:00:00,300 --> 00:00:03,300\nTimeline check\n', encoding='utf-8')
        for target, name, value in ((export, 'PROJECT_ROOT', self.root), (studio, 'ROOT', self.root / 'workspace'),
                                    (export, '_size', lambda record: (160, 90))):
            mock = patch.object(target, name, value)
            mock.start()
            self.addCleanup(mock.stop)


class VideoExportContracts(ExportFixture):
    def test_worker_records_actual_check_counts_without_claiming_acceptance(self):
        from backend.app import video_quality, video_dialogue_quality, video_dialogue
        for automatic, native, visuals, speech in (
                (False, True, [], []), (True, False, [], []),
                (True, True, [{'shot_id':'red'}], [{'shot_id':'red'},{'shot_id':'blue'}])):
            with self.subTest(automatic=automatic,native=native):
                record=copy.deepcopy(self.record);record['status']='exporting'
                studio.save(self.path,record)
                studio.ACTIVE.add(str(self.path))
                self.addCleanup(studio.ACTIVE.discard,str(self.path))
                output=self.root/'output'/'mock-export';output.mkdir(parents=True,exist_ok=True)
                raw,subtitles=output/'raw.mp4',output/'subtitles.mp4'
                raw.write_bytes(b'export');subtitles.write_bytes(b'export with captions')
                with patch.object(video_dialogue,'native_mode',return_value=native), \
                     patch.object(video_quality,'check_automatic_export',return_value=visuals) as visual, \
                     patch.object(video_dialogue_quality,'check_native_speech',return_value=speech) as words, \
                     patch.object(export,'_render',return_value=(output,raw,subtitles)), \
                     patch.object(export,'reconcile_source_job') as reconcile:
                    export._worker(self.path,1,review_generated=automatic)
                result=studio.read(self.path)
                self.assertEqual(result['status'],'completed')
                self.assertEqual(result['export']['review'],dict(
                    mode='automatic' if automatic else 'observation', visual_shots_checked=len(visuals),
                    speech_shots_checked=len(speech),human_acceptance='pending',source_revision=1))
                self.assertEqual(visual.call_count,int(automatic))
                self.assertEqual(words.call_count,int(automatic and native))
                self.assertEqual(reconcile.call_args.args[0]['export'],result['export'])

    def test_narration_volume_is_bounded_and_omitted_requests_preserve_setting(self):
        for value in (-.1, 1.1, math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                export.ExportRequest(revision=1, narration_volume=value)
        self.record['export_settings'] = {'narration_volume': .5}
        studio.save(self.path, self.record)
        with patch.object(export.threading, 'Thread'):
            result = export.start_export_for_user(self.record['id'], export.ExportRequest(revision=1), {'id': 1})
        self.addCleanup(studio.ACTIVE.discard, str(self.path))
        self.assertEqual(result['export_settings']['narration_volume'], .5)

    def test_long_narration_extends_only_the_last_hold(self):
        original = copy.deepcopy(self.record)
        _, before = export._timeline(self.record)
        expected, after = export._timeline(self.record, narration_duration=3.55)
        self.assertAlmostEqual(expected, 107/30)
        self.assertEqual(after[:-1], before[:-1])
        self.assertEqual(after[-1]['content_duration'], before[-1]['content_duration'])
        self.assertEqual(sum(row['frames'] for row in after), 107)
        self.assertEqual(self.record, original)
        self.assertEqual(export._timeline(self.record, narration_duration=.2), export._timeline(self.record))

    def test_narration_extension_does_not_legalize_invalid_shot_boundaries(self):
        self.record['shots'][-1]['end'] = 3.8
        with self.assertRaises(ValueError):
            export._timeline(self.record, narration_duration=4)

    def test_timeline_preserves_absolute_gaps_and_rounds_boundaries(self):
        expected, segments = export._timeline(self.record)
        self.assertEqual(expected, 3.3)
        self.assertEqual([row['frames'] for row in segments], [48, 24, 27])
        self.assertEqual(segments[0]['lead'], .3)
        self.assertEqual(sum(row['frames'] for row in segments), 99)
        self.record['scenes'][-1]['end'] = 1.001
        self.record['shots'] = [dict(id=str(index), start=index / 7, end=(index+1) / 7)
                                for index in range(7)]
        _, segments = export._timeline(self.record)
        self.assertEqual(sum(row['frames'] for row in segments), round(1.001 * 30))

    def test_invalid_or_overlapping_timeline_fails_before_rendering(self):
        for field, value in [('start', -.1), ('start', 1.1), ('end', 4), ('end', math.nan)]:
            record = copy.deepcopy(self.record)
            record['shots'][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                export._timeline(record)

    def test_missing_audio_or_subtitles_fails_before_creating_output(self):
        for field in ('audio', 'subtitles'):
            path = self.path / (self.record['audio'] if field == 'audio' else 'assets/subtitles.srt')
            original = path.read_bytes()
            path.unlink()
            try:
                with self.subTest(field=field), patch.object(export, '_run') as run, \
                        self.assertRaisesRegex(ValueError, '配音文件' if field == 'audio' else '字幕文件'):
                    export._render(self.path, self.record)
                run.assert_not_called()
                self.assertFalse((self.root / 'output').exists())
            finally:
                path.write_bytes(original)

    def test_same_names_and_repeated_attempts_never_share_output(self):
        def fake_run(command, label):
            Path(command[-1]).write_bytes(b'[Script Info]\nPlayResX: 384\nPlayResY: 288\n' if label == '字幕样式准备'
                                          else label.encode('utf-8'))

        other = self.root / 'workspace' / '2' / ('b' * 32)
        shutil.copytree(self.path, other)
        other_record = copy.deepcopy(self.record)
        other_record['id'] = other.name
        with patch.object(export, '_run', side_effect=fake_run), \
                patch.object(export, '_has_audio_stream', return_value=True), \
                patch.object(export, '_video_duration', return_value=3.3), \
                patch.object(export, 'probe_media_duration', return_value=3.3):
            first = export._render(self.path, self.record)[0]
            second = export._render(other, other_record)[0]
            third = export._render(self.path, self.record)[0]
        self.assertEqual(len({first, second, third}), 3)
        self.assertIn('_1_' + self.path.name + '_', first.name)
        self.assertIn('_2_' + other.name + '_', second.name)
        self.assertEqual(first.parent, self.root / 'output')

    def test_failed_retry_keeps_previous_export_record_and_file_bytes(self):
        old = self.root / 'output' / 'previous-export'
        old.mkdir(parents=True)
        files = [old / 'raw.mp4', old / 'subtitles.mp4']
        for file in files:
            file.write_bytes(b'previous successful export: ' + file.name.encode())
        digests = [hashlib.sha256(file.read_bytes()).hexdigest() for file in files]
        saved_export = dict(directory=str(old), raw=str(files[0]), subtitles=str(files[1]),
                            review={'mode':'observation','human_acceptance':'pending'})
        self.record.update(status='exporting', export=saved_export)
        studio.save(self.path, self.record)
        studio.ACTIVE.add(str(self.path))
        self.addCleanup(studio.ACTIVE.discard, str(self.path))

        def fail_subtitle(command, label):
            Path(command[-1]).write_bytes(b'[Script Info]\nPlayResX: 384\nPlayResY: 288\n' if label == '字幕样式准备'
                                          else b'new incomplete attempt')
            if label == '字幕版合成':
                raise RuntimeError('synthetic subtitle failure')

        with patch.object(export, '_run', side_effect=fail_subtitle), \
                patch.object(export, '_has_audio_stream', return_value=True), \
                patch.object(export, 'probe_media_duration', return_value=3.3), \
                patch.object(export, 'reconcile_source_job') as reconcile:
            export._worker(self.path, 1)
        result = studio.read(self.path)
        self.assertEqual(result['status'], 'export_failed')
        self.assertEqual(result['export'], saved_export)
        self.assertEqual(digests, [hashlib.sha256(file.read_bytes()).hexdigest() for file in files])
        self.assertNotIn(str(self.path), studio.ACTIVE)
        reconcile.assert_not_called()

    def test_explicit_subtitle_layout_keeps_size_and_controls_box_opacity(self):
        record = copy.deepcopy(self.record)
        record['creation_parameters'] = {'subtitle_layouts': {'landscape': {
            'size': 48, 'position': 94, 'opacity': .5, 'outline_color': '#102030'}}}
        style = export._subtitle_style(record, 1080)
        self.assertIn('FontSize=48,', style)
        self.assertIn('MarginV=65,', style)
        self.assertIn('OutlineColour=&H80302010,', style)
        record['creation_parameters']['subtitle_layouts']['landscape']['background'] = False
        style = export._subtitle_style(record, 1080)
        self.assertIn('BorderStyle=1,', style)
        self.assertIn('OutlineColour=&H00302010,', style)

    def _source_job_fixture(self, binding):
        output = self.root / 'output' / 'finished'
        output.mkdir(parents=True, exist_ok=True)
        for name in ('raw.mp4', 'subtitles.mp4'):
            (output / name).write_bytes(b'completed export fixture')
        self.record.update(status='completed', source_project={'id': 'source-task'},
                           export=dict(directory=str(output), raw=str(output / 'raw.mp4'),
                                       subtitles=str(output / 'subtitles.mp4')))
        request = {'dynamic_video': True, '_step_mode_stage': 'audio_review'}
        if binding is not None:
            request['_dynamic_video_project_id'] = binding
        return SimpleNamespace(id='source-task', user_id=1, status='waiting_confirmation',
                               request=request, artifacts={'audio': '/original-audio'}, message='Waiting')

    def test_source_job_without_exact_child_binding_has_no_side_effects(self):
        for binding in (None, '', 'another-video-project'):
            job = self._source_job_fixture(binding)
            original = copy.deepcopy(vars(job))
            storage = Mock()
            storage.get.return_value = job
            with self.subTest(binding=binding), patch.object(export, 'store', storage), \
                    patch.object(export, 'persist_step_workflow_state') as persist, \
                    patch.object(export, 'register_job_asset') as register:
                self.assertFalse(export.reconcile_source_job(self.record, 1))
                self.assertEqual(vars(job), original)
                storage.update.assert_not_called()
                storage.log.assert_not_called()
                persist.assert_not_called()
                register.assert_not_called()

    def test_bound_child_updates_source_job_once_and_preserves_other_artifacts(self):
        job = self._source_job_fixture(self.record['id'])
        storage = Mock()
        storage.get.return_value = job

        def update(target, **fields):
            vars(target).update(fields)

        storage.update.side_effect = update
        with patch.object(export, 'store', storage), \
                patch.object(export, 'persist_step_workflow_state') as persist, \
                patch.object(export, 'register_job_asset') as register:
            self.assertTrue(export.reconcile_source_job(self.record, 1))
            self.assertEqual(job.status, 'completed')
            self.assertIn('没有质量检查记录',job.message)
            self.assertNotIn('_dynamic_video_export_review',job.request)
            self.assertEqual(job.request['_step_mode_stage'], 'completed')
            self.assertEqual(job.artifacts['audio'], '/original-audio')
            self.assertEqual(job.artifacts['video_raw'], f'/api/video-studio/{self.record["id"]}/export/raw')
            self.assertEqual(job.artifacts['video_with_subtitles'],
                             f'/api/video-studio/{self.record["id"]}/export/subtitles')
            self.assertEqual(register.call_count, 2)
            persist.assert_called_once()
            calls = storage.update.call_count
            self.assertTrue(export.reconcile_source_job(self.record, 1))
            self.assertEqual(storage.update.call_count, calls)
            self.assertEqual(register.call_count, 2)
            persist.assert_called_once()

    def test_already_synced_legacy_export_does_not_write_empty_review_on_restart(self):
        job=self._source_job_fixture(self.record['id'])
        job.status='completed'
        job.request.update(_step_mode_stage='completed',
                           _dynamic_video_output_dir=Path(self.record['export']['directory']).name,
                           _step_output_dir=Path(self.record['export']['directory']).name)
        job.artifacts.update(video_raw=f'/api/video-studio/{self.record["id"]}/export/raw',
            video_with_subtitles=f'/api/video-studio/{self.record["id"]}/export/subtitles')
        before=copy.deepcopy(vars(job));storage=Mock();storage.get.return_value=job
        with patch.object(export,'store',storage), \
             patch.object(export,'persist_step_workflow_state') as persist, \
             patch.object(export,'register_job_asset') as register:
            self.assertTrue(export.reconcile_source_job(self.record,1))
        self.assertEqual(vars(job),before)
        storage.update.assert_not_called();persist.assert_not_called();register.assert_not_called()

    def test_export_preserves_editable_audio_workspace_and_tracks_each_archive(self):
        job = self._source_job_fixture(self.record['id'])
        job.request.update(_dynamic_audio_source_dir='editable-audio', _step_output_dir='editable-audio')
        storage = Mock(); storage.get.return_value = job
        storage.update.side_effect = lambda target, **fields: vars(target).update(fields)
        with patch.object(export, 'store', storage), \
                patch.object(export, 'persist_step_workflow_state'), \
                patch.object(export, 'register_job_asset'):
            self.assertTrue(export.reconcile_source_job(self.record, 1))
            self.assertEqual(job.request['_step_output_dir'], 'editable-audio')
            self.assertEqual(job.request['_dynamic_video_output_dir'], 'finished')
            calls = storage.update.call_count
            self.assertTrue(export.reconcile_source_job(self.record, 1))
            self.assertEqual(storage.update.call_count, calls)
            # A second export changes its archive without changing the editor.
            other = self.root/'output'/'finished-again'; other.mkdir()
            for name in ('raw.mp4', 'subtitles.mp4'):
                (other/name).write_bytes(b'new export')
            self.record['export'] = dict(directory=str(other),raw=str(other/'raw.mp4'),subtitles=str(other/'subtitles.mp4'))
            self.assertTrue(export.reconcile_source_job(self.record, 1))
            self.assertEqual(job.request['_dynamic_video_output_dir'], 'finished-again')
            self.assertEqual(job.request['_step_output_dir'], 'editable-audio')

    def test_interrupted_export_recovers_once_and_keeps_previous_downloads(self):
        self._source_job_fixture(self.record['id'])
        self.record.update(status='exporting', revision=4)
        previous_export = copy.deepcopy(self.record['export'])
        original_shots = copy.deepcopy(self.record['shots'])
        asset_bytes = {str(file): file.read_bytes() for file in self.path.rglob('*') if file.is_file()}
        studio.save(self.path, self.record)
        self.assertNotIn(str(self.path), studio.ACTIVE)
        recovered = studio.read(self.path)
        self.assertEqual(recovered['status'], 'export_failed')
        self.assertEqual(recovered['revision'], 5)
        self.assertIn('合成已中断', recovered['error'])
        self.assertEqual(recovered['logs'].count(recovered['error']), 1)
        self.assertEqual(recovered['export'], previous_export)
        self.assertEqual(recovered['shots'], original_shots)
        self.assertEqual(recovered['audio'], self.record['audio'])
        persisted = (self.path / 'record.json').read_bytes()
        self.assertEqual(studio.read(self.path), recovered)
        self.assertEqual((self.path / 'record.json').read_bytes(), persisted)
        for name, content in asset_bytes.items():
            self.assertEqual(Path(name).read_bytes(), content)
        with patch.object(studio, 'require_user', return_value={'id': 1}):
            for variant in ('raw', 'subtitles'):
                response = export.download(self.record['id'], variant, None)
                self.assertEqual(Path(response.path).resolve(), Path(previous_export[variant]).resolve())
                self.assertEqual(Path(response.path).read_bytes(), b'completed export fixture')

    def test_start_export_keeps_active_guard_until_worker_finishes(self):
        self._source_job_fixture(self.record['id'])
        self.record.update(status='export_failed', revision=4)
        self.record.pop('source_project')
        studio.save(self.path, self.record)
        completed_export = self.record['export']

        def render(path, record):
            self.assertIn(str(path), studio.ACTIVE)
            current = studio.read(path)
            self.assertEqual(current['status'], 'exporting')
            self.assertEqual(current['revision'], 5)
            self.assertFalse(any('合成已中断' in line for line in current['logs']))
            return tuple(Path(completed_export[key]) for key in ('directory', 'raw', 'subtitles'))

        def thread(*, target, args, daemon):
            return SimpleNamespace(start=lambda: target(*args))

        with patch.object(studio, 'require_user', return_value={'id': 1}), \
                patch.object(export.threading, 'Thread', side_effect=thread), \
                patch.object(export, '_render', side_effect=render) as rendered:
            export.start_export(self.record['id'], export.ExportRequest(revision=4), None)
        rendered.assert_called_once()
        finished = studio.read(self.path)
        self.assertEqual(finished['status'], 'completed')
        self.assertEqual(finished['revision'], 6)
        self.assertFalse(any('合成已中断' in line for line in finished['logs']))
        self.assertNotIn(str(self.path), studio.ACTIVE)


@unittest.skipUnless(shutil.which(export.ffmpeg_binary()) and shutil.which(export.ffprobe_binary()),
                     'FFmpeg and FFprobe are required for the local render check')
class VideoExportRender(ExportFixture):
    def setUp(self):
        super().setUp()
        self.ffmpeg = export.ffmpeg_binary()
        self._command(['-f', 'lavfi', '-i', 'color=c=red:s=160x90:r=30:d=0.35',
                       '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=0.35',
                       '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(self.path / 'red.mp4')])
        self._command(['-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=30:d=0.2',
                       '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(self.path / 'blue.mp4')])
        self._command(['-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=0.2',
                       '-c:a', 'pcm_s16le', str(self.path / self.record['audio'])])

    def _command(self, args):
        result = subprocess.run([self.ffmpeg, '-v', 'error', '-y', *args], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        return result.stdout

    def _pixel(self, file, seconds):
        return tuple(self._command(['-ss', str(seconds), '-i', str(file), '-frames:v', '1',
                                    '-vf', 'scale=1:1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1']))

    def _rms(self, file, seconds):
        samples = array.array('f', self._command(['-ss', str(seconds), '-i', str(file), '-t', '0.08',
                              '-vn', '-ac', '1', '-ar', '48000', '-f', 'f32le', 'pipe:1']))
        self.assertGreater(len(samples), 100)
        return math.sqrt(sum(value * value for value in samples) / len(samples))

    def _check_versions(self, raw, subtitled):
        for file in (raw, subtitled):
            self.assertAlmostEqual(export.probe_media_duration(file), 3.3, delta=.08)
            self._command(['-i', str(file), '-f', 'null', '-'])
        for seconds, channel in [(.1, 0), (1.4, 0), (1.8, 1), (2.2, 1), (2.6, 2), (3.1, 2)]:
            with self.subTest(seconds=seconds):
                pixel = self._pixel(raw, seconds)
                self.assertEqual(len(pixel), 3)
                self.assertGreater(pixel[channel], max(value for index, value in enumerate(pixel) if index != channel) + 50)

    def test_mixed_sources_short_video_and_short_narration_preserve_timeline_and_audio(self):
        self.record['export_settings'] = {'use_video_audio': True}
        _, raw, subtitled = export._render(self.path, self.record)
        self._check_versions(raw, subtitled)
        self.assertGreater(self._rms(raw, .4), .005, 'source sound should start after the first-frame hold')
        self.assertLess(self._rms(raw, .9), .001, 'holding a short video must not loop its sound')
        self.assertLess(self._rms(raw, 1.4), .001, 'holding the last frame between shots must not loop its sound')
        self.assertLess(self._rms(raw, 3.1), .001)

    def test_explicit_narration_resync_slows_sound_only_within_shot(self):
        self.record['export_settings'] = {'use_video_audio': True}
        self.record['shots'][0]['audio_timing_adjustment'] = {'mode': 'trim_or_slow'}
        _, raw, subtitled = export._render(self.path, self.record)
        self._check_versions(raw, subtitled)
        self.assertLess(self._rms(raw, .21), .001, 'source sound must not fill the leading hold')
        self.assertGreater(self._rms(raw, .9), .005, 'source sound follows explicitly retimed video')
        self.assertLess(self._rms(raw, 1.4), .001, 'retiming must not fill the gap after the shot')

    def test_muting_source_audio_does_not_truncate_at_short_narration(self):
        self.record['export_settings'] = {'use_video_audio': False}
        _, raw, subtitled = export._render(self.path, self.record)
        self._check_versions(raw, subtitled)
        self.assertGreater(self._rms(raw, .06), .005)
        self.assertLess(self._rms(raw, .4), .001, 'source audio must stay muted')
        self.assertLess(self._rms(raw, 3.1), .001)

    def test_narration_after_last_subtitle_survives_both_export_audio_modes(self):
        self._command(['-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=3.55',
                       '-c:a', 'pcm_s16le', str(self.path / self.record['audio'])])
        original = copy.deepcopy(self.record)
        source_audio = (self.path / self.record['audio']).read_bytes()
        source_srt = (self.path / 'assets/subtitles.srt').read_bytes()
        for keep_effects in (True, False):
            with self.subTest(keep_effects=keep_effects):
                self.record['export_settings'] = {'use_video_audio': keep_effects}
                _, raw, subtitled = export._render(self.path, self.record)
                for file in (raw, subtitled):
                    self.assertAlmostEqual(export.probe_media_duration(file), 107/30, delta=.04)
                    self.assertGreater(self._rms(file, 3.4), .005, 'narration beyond the final subtitle must remain audible')
                    self._command(['-i', str(file), '-f', 'null', '-'])
                self.assertGreater(self._pixel(raw, 3.5)[2], 200, 'last image extends to cover the narration tail')
                self.assertGreater(self._pixel(raw, 1.4)[0], 200, 'earlier timing remains unchanged')
        self.assertEqual(self.record['shots'], original['shots'])
        self.assertEqual(self.record['scenes'], original['scenes'])
        self.assertEqual((self.path/self.record['audio']).read_bytes(), source_audio)
        self.assertEqual((self.path/'assets/subtitles.srt').read_bytes(), source_srt)

    def test_retaining_clip_audio_does_not_halve_the_narration_level(self):
        self._command(['-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=3.3',
                       '-ac', '2', '-c:a', 'pcm_s16le', str(self.path / self.record['audio'])])
        self.record['export_settings'] = {'use_video_audio': True}
        _, raw, _ = export._render(self.path, self.record)
        # At this instant the short source clip has ended, but narration is
        # still present. Keeping its padded effects track must not duck speech.
        original = self._rms(self.path / self.record['audio'], 1.4)
        mixed = self._rms(raw, 1.4)
        self.assertGreater(mixed / original, .95)
        self.assertLess(mixed / original, 1.15)

    def test_lowering_narration_reduces_only_narration_in_both_audio_modes(self):
        source = (self.path / self.record['audio']).read_bytes()
        for keep_effects in (True, False):
            self.record['export_settings'] = {'use_video_audio': keep_effects, 'narration_volume': 1.0}
            _, full, _ = export._render(self.path, self.record)
            self.record['export_settings']['narration_volume'] = .5
            _, half, _ = export._render(self.path, self.record)
            with self.subTest(keep_effects=keep_effects):
                # At .06s only narration plays; at .4s only the short clip's effect plays.
                self.assertAlmostEqual(self._rms(half, .06) / self._rms(full, .06), .5, delta=.04)
                if keep_effects:
                    self.assertAlmostEqual(self._rms(half, .4) / self._rms(full, .4), 1, delta=.04)
                else:
                    self.assertLess(self._rms(half, .4), .001)
                self.assertAlmostEqual(export.probe_media_duration(half), 3.3, delta=.08)
        self.assertEqual((self.path / self.record['audio']).read_bytes(), source)

    def test_subtitle_pixel_size_bottom_position_and_translucent_box(self):
        source = self.path / 'assets/subtitles.srt'
        source.write_text('1\n00:00:00,000 --> 00:00:01,000\n音乐会那天，朋友们刚刚坐好，\n', encoding='utf-8')
        original = source.read_bytes()
        ass = export._subtitle_ass(source, self.path / 'preview.ass', 1920, 1080)
        text = ass.read_text(encoding='utf-8')
        self.assertIn('PlayResX: 1920\nPlayResY: 1080\n', text)
        self.assertEqual(text.count('PlayResX:'), 1)
        self.assertEqual(source.read_bytes(), original)
        style = export._subtitle_style(self.record, 1080)
        video_filter = f"subtitles=filename='{export._subtitle_filter_path(ass)}':force_style='{style}'"
        pixels = self._command(['-f', 'lavfi', '-i', 'color=c=0x6060e0:s=1920x1080:r=1',
                                '-vf', video_filter, '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'])
        frame = Image.frombytes('RGB', (1920, 1080), pixels)
        red, green, blue = frame.split()
        from PIL import ImageChops
        white = ImageChops.darker(ImageChops.darker(red, green), blue).point(lambda value: 255 if value > 225 else 0)
        bounds = white.getbbox()
        self.assertIsNotNone(bounds)
        left, top, right, bottom = bounds
        self.assertGreater(top, 930, 'subtitle must stay near the bottom, in output-pixel units')
        self.assertLessEqual(bottom, 1040)
        self.assertGreaterEqual(bottom-top, 25)
        self.assertLessEqual(bottom-top, 70, 'default text must not grow to the old ~90px glyph height')
        self.assertLess(right-left, 1100)
        crop = frame.crop((max(0, left-20), max(0, top-20), min(1920, right+20), min(1080, bottom+20)))
        values = iter(crop.tobytes())
        translucent = sum(15 <= r <= 35 and 15 <= g <= 35 and 40 <= b <= 70 for r,g,b in zip(values, values, values))
        self.assertGreater(translucent, 100, 'configured box opacity should preserve some background color')


if __name__ == '__main__':
    unittest.main()
