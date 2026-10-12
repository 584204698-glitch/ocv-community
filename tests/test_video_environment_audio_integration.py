import array
import copy
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

from backend.app import video_environment_audio as env, video_external_dialogue as exact
from backend.app import video_export as export


def wav(path, frequency, *, burst=False):
    values=array.array('h',(round(32767*.1*math.sin(2*math.pi*frequency*i/48000))
        if not burst or 48000<=i<96000 else 0 for i in range(192000)))
    with wave.open(str(path),'wb') as out:
        out.setparams((1,2,48000,0,'NONE','not compressed'));out.writeframes(values.tobytes())
    return path


class EnvironmentSettingsTests(unittest.TestCase):
    def test_off_does_not_require_machine_paths(self):
        with patch.dict(os.environ,{'OCV_ENVIRONMENT_AUDIO':'off',
                                    'OCV_ENVIRONMENT_AUDIO_CHECKPOINT':'invalid'}):
            self.assertIsNone(env.settings())
            track={'unchanged':True}
            self.assertEqual(env.mix_environment('.',None,None,track,0,{}),(track,None))

    def test_opt_in_requires_explicit_existing_paths_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint=Path(tmp)/'weights.pt';checkpoint.write_bytes(b'weights')
            settings={'OCV_ENVIRONMENT_AUDIO':'hdemucs_cpu','OCV_ENVIRONMENT_AUDIO_PYTHON':sys.executable,
                      'OCV_ENVIRONMENT_AUDIO_CHECKPOINT':str(checkpoint),
                      'OCV_ENVIRONMENT_AUDIO_SHA256':env.sha256(checkpoint)}
            with patch.dict(os.environ,settings):
                self.assertEqual(env.settings()['checkpoint'],checkpoint.resolve())
                for key,value in [('OCV_ENVIRONMENT_AUDIO','unknown'),
                                  ('OCV_ENVIRONMENT_AUDIO_PYTHON','relative/python'),
                                  ('OCV_ENVIRONMENT_AUDIO_CHECKPOINT','missing'),
                                  ('OCV_ENVIRONMENT_AUDIO_SHA256','not a hash')]:
                    with self.subTest(key=key),patch.dict(os.environ,{key:value}),self.assertRaises(ValueError):
                        env.settings()

    def test_settings_preserve_python_symlink_entrypoint_but_resolve_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp).resolve()
            python=path/'venv-python';checkpoint=path/'weights.pt'
            weights=path/'actual-weights.pt';weights.write_bytes(b'weights')
            try:
                python.symlink_to(Path(sys.executable).resolve())
                checkpoint.symlink_to(weights)
            except OSError as exc:
                self.skipTest(f'Symbolic links unavailable: {exc}')
            with patch.dict(os.environ,{
                'OCV_ENVIRONMENT_AUDIO':'hdemucs_cpu',
                'OCV_ENVIRONMENT_AUDIO_PYTHON':str(python),
                'OCV_ENVIRONMENT_AUDIO_CHECKPOINT':str(checkpoint),
                'OCV_ENVIRONMENT_AUDIO_SHA256':env.sha256(weights),
            }):
                config=env.settings()
            self.assertEqual(config['python'],python)
            self.assertNotEqual(config['python'],python.resolve())
            self.assertEqual(config['checkpoint'],weights.resolve())

    def test_worker_is_cpu_offline_hash_bound_and_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp).resolve();original=path/'raw.mp4';original.write_bytes(b'raw')
            checkpoint=path/'model.pt';checkpoint.write_bytes(b'weights')
            config=dict(python=Path(sys.executable),checkpoint=checkpoint,sha256=env.sha256(checkpoint))
            basis=env._basis(env.sha256(original),config['sha256'])
            folder=path/'assets/environment_audio/stems'/env._digest(basis)
            def run(command,**kwargs):
                self.assertEqual(command[0],sys.executable)
                self.assertEqual(kwargs['env']['CUDA_VISIBLE_DEVICES'],'')
                self.assertEqual(kwargs['env']['OMP_NUM_THREADS'],'4')
                self.assertEqual(kwargs['env']['HF_HUB_OFFLINE'],'1')
                self.assertEqual(command[command.index('--source')+1],str(original))
                artifacts={}
                for label in ('non_vocal_candidate','vocals'):
                    file=folder/(label+'.wav');file.write_bytes(label.encode())
                    artifacts[label]=dict(file=file.name,sha256=env.sha256(file))
                (folder/'receipt.json').write_text(json.dumps(dict(basis=basis,artifacts=artifacts)))
                return type('Result',(),dict(returncode=0))()
            with patch.object(env.subprocess,'run',side_effect=run) as worker:
                first=env._separated(path,original,config,env.sha256(original),'ffmpeg')
                second=env._separated(path,original,config,env.sha256(original),'ffmpeg')
            worker.assert_called_once();self.assertEqual(first,second)
            first[0].write_bytes(b'changed stem')
            with self.assertRaises(ValueError):env._separated(path,original,config,env.sha256(original),'ffmpeg')


@unittest.skipUnless(shutil.which(export.ffmpeg_binary()) and shutil.which(export.ffprobe_binary()),'FFmpeg required')
class EnvironmentMixTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.path=Path(temporary.name).resolve()
        self.original=self.path/'raw.mp4';self.original.write_bytes(b'original speech must never enter mixer')
        self.rendered=self.path/'calibrated.mp4';self.rendered.write_bytes(b'calibrated video without audio')
        self.stem=wav(self.path/'candidate.wav',220)
        tts=wav(self.path/'tts.wav',880,burst=True)
        self.track=dict(file=tts.name,sha256=env.sha256(tts),duration=4)
        self.separation=dict(basis=env._basis(env.sha256(self.original),'a'*64),limitations=['Human review pending'])
        receipt=self.path/'separation.json';receipt.write_text('{}')
        patcher=patch.object(env,'settings',return_value={'enabled':True});patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(env,'_separated',return_value=(self.stem,self.separation,receipt))
        self.separate=patcher.start();self.addCleanup(patcher.stop)

    def mix(self,alignment=None,rendered=None):
        return env.mix_environment(self.path,self.original,rendered or self.original,self.track,4,
                                   alignment or dict(status='unverified'))

    def test_real_audio_mix_keeps_tts_samples_timing_and_nonvocal_energy(self):
        with patch.object(export,'_run',wraps=export._run) as run:
            mixed,diagnostic=self.mix()
        self.assertEqual(self.separate.call_args.args[1],self.original)
        command=run.call_args.args[0]
        self.assertNotIn(str(self.original),command)
        self.assertIn(str(self.stem),command)
        self.assertIn('normalize=0',command[command.index('-filter_complex')+1])
        def samples(file):
            with wave.open(str(file),'rb') as source:
                return array.array('h',source.readframes(source.getnframes()))
        actual=samples(self.path/mixed['file']);tts=samples(self.path/self.track['file']);stem=samples(self.stem)
        self.assertEqual(len(actual),len(tts))
        self.assertLess(max(abs(a-b-c) for a,b,c in zip(actual,tts,stem)),5)
        self.assertTrue(any(actual[:48000]));self.assertTrue(any(actual[96000:]))
        self.assertFalse(diagnostic['raw_audio_mixed']);self.assertFalse(diagnostic['quality_accepted'])
        self.assertFalse(diagnostic['speaker_verified']);self.assertFalse(diagnostic['lip_sync_verified'])

    def test_calibrated_stem_uses_identical_intervals_and_original_source(self):
        pieces=[dict(start=0,end=2,target_start=0,target_end=1,scale=.5),
                dict(start=2,end=4,target_start=1,target_end=4,scale=1.5)]
        with patch.object(export,'_run',wraps=export._run) as run:
            mixed,diagnostic=self.mix(dict(status='calibrated',intervals=pieces),self.rendered)
        self.assertEqual(run.call_count,2)
        command=run.call_args_list[0].args[0]
        self.assertEqual(command[command.index('-filter_complex')+1],env.timing_filter(pieces,4))
        self.assertEqual(self.separate.call_args.args[1],self.original)
        saved=json.loads((self.path/diagnostic['receipt']).read_text(encoding='utf8'))
        self.assertEqual(saved['basis']['intervals'],pieces)
        self.assertAlmostEqual(export.probe_media_duration(self.path/mixed['file']),4,places=3)

    def test_manual_and_audio_placed_leave_environment_timing_unchanged(self):
        for status in ('manual','audio_placed','already_aligned','unverified'):
            with self.subTest(status=status),patch.object(export,'_run',wraps=export._run) as run:
                _,diagnostic=self.mix(dict(status=status,intervals=[{'must':'ignore'}]))
                saved=json.loads((self.path/diagnostic['receipt']).read_text(encoding='utf8'))
                self.assertIsNone(saved['basis']['intervals'])
                for call in run.call_args_list:
                    self.assertNotIn('atempo=',str(call))

    def test_cache_reuses_mix_checks_hashes_and_binds_tts_and_source(self):
        first,_=self.mix()
        with patch.object(export,'_run',side_effect=AssertionError('cached mix')):
            self.assertEqual(self.mix()[0],first)
        wav(self.path/'tts.wav',660,burst=True);self.track['sha256']=env.sha256(self.path/'tts.wav')
        second,_=self.mix();self.assertNotEqual(first['file'],second['file'])
        self.original.write_bytes(b'new original');third,_=self.mix()
        self.assertNotEqual(second['file'],third['file'])
        (self.path/third['file']).write_bytes(b'changed mixed audio')
        with self.assertRaises(ValueError):self.mix()

    def test_enabled_failure_never_falls_back_to_raw_audio(self):
        self.separate.side_effect=ValueError('weights unavailable')
        with patch.object(export,'_run') as run,self.assertRaisesRegex(ValueError,'weights unavailable'):
            self.mix()
        run.assert_not_called()
        failure=json.loads((self.path/'environment_audio/last_failure.json').read_text(encoding='utf8'))
        self.assertFalse(failure['raw_audio_mixed']);self.assertFalse(failure['quality_accepted'])

    def test_missing_calibration_map_or_unexplained_video_change_fails(self):
        with self.assertRaises(ValueError):self.mix(dict(status='calibrated'),self.rendered)
        with self.assertRaises(ValueError):self.mix(dict(status='manual'),self.rendered)
        self.separate.assert_not_called()

    def test_preview_passes_original_and_full_alignment_then_checks_output_hash(self):
        shot=dict(kind='video',video=self.original.name,video_request=dict(external_dialogue=dict(
            plan=dict(duration=4),clips=[])))
        alignment=dict(status='calibrated',intervals=[dict(start=0,end=4,target_start=0,target_end=4,scale=1)])
        diagnostic=dict(status='mixed_estimated_non_vocal',quality_accepted=False)
        def run(command,label):Path(command[-1]).write_bytes(b'muxed audio and video')
        with patch('backend.app.video_external_alignment.calibrate',return_value=(self.rendered,alignment)), \
             patch.object(exact,'make_track',return_value=self.track) as make_track, \
             patch.object(env,'mix_environment',return_value=(self.track,diagnostic)) as mix, \
             patch.object(export,'probe_media_duration',return_value=4), \
             patch.object(export,'_run',side_effect=run) as mux:
            output=exact.preview(self.path,shot,volume=.37)
            self.assertEqual(make_track.call_args.kwargs['narrator_volume'],.37)
            self.assertEqual(mix.call_args.args[1:3],(self.original,self.rendered))
            self.assertEqual(mix.call_args.args[-1],alignment)
            self.assertEqual(exact.preview(self.path,shot,volume=.37),output)
            self.assertEqual(mux.call_count,1)
            (self.path/output).write_bytes(b'corrupted mux')
            with self.assertRaisesRegex(ValueError,'缓存'):exact.preview(self.path,shot,volume=.37)
