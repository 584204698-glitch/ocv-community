import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image

from backend.app import video_quality as quality, video_export as export, one_click


class QualityTests(unittest.TestCase):
    def test_provider_syntax_recovery_keeps_observations_and_issues_without_rescreening(self):
        review={'observations':[{'time':t,'description':'Visible subject'} for t in quality.sample_times(5)],
                'issues':[{'code':'anatomy','time':2,'evidence':'An extra hand is visible.'}],
                'uncertainties':['The rear hand is occluded.']}
        raw=json.dumps(review).replace('"issues":','"issues"',1)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/'clip.mp4').write_bytes(b'original video')
            sheets=[{'mime_type':'image/jpeg','data':'fixture'}]
            with patch.object(quality,'_sheets',return_value=sheets), \
                 patch('backend.app.pipeline.probe_media_duration',return_value=5), \
                 patch('backend.app.video_agents.generate_gemini_text',
                       side_effect=[raw,json.dumps(review)]) as generate:
                result=quality.screen_shot(path,{'id':'one','video':'clip.mp4','duration':5})
                self.assertEqual(generate.call_count,2)
                initial,repaired=[call.kwargs for call in generate.call_args_list]
                self.assertEqual(initial['image_data'],sheets)
                self.assertEqual(initial['temperature'],.1)
                self.assertEqual(initial['max_output_tokens'],4500)
                self.assertNotIn('image_data',repaired)
                repair_payload=json.loads(repaired['user_prompt'])
                self.assertEqual(repair_payload['raw_output'],raw)
                self.assertEqual(repair_payload['original_request'],{'response_contract':quality.SYSTEM})
                self.assertIn('不补造缺失字段或空列表',repaired['system_prompt'])
                self.assertEqual(result['review']['issues'],review['issues'])
                self.assertEqual(result['review']['observations'],review['observations'])
                self.assertFalse(result['quality_guaranteed'])
                quality.screen_shot(path,{'id':'one','video':'clip.mp4','duration':5})
                self.assertEqual(generate.call_count,2)
            response=next((path/'assets/quality_reviews').glob('*.response.json'))
            self.assertEqual(json.loads(response.read_text(encoding='utf8'))['model_outputs'],
                             {'original':raw,'repaired':json.dumps(review)})
            self.assertEqual((path/'clip.mp4').read_bytes(),b'original video')

    def test_failed_syntax_repair_and_missing_fields_never_create_pass_cache(self):
        raw='{"observations" []}'
        for repaired in (raw,'{"observations":[],"uncertainties":[]}',
                         '{"observations":[],"issues":[],"uncertainties":[]}'):
            with self.subTest(repaired=repaired),tempfile.TemporaryDirectory() as folder:
                path=Path(folder);(path/'clip.mp4').write_bytes(b'original video')
                with patch.object(quality,'_sheets',return_value=[]), \
                     patch('backend.app.pipeline.probe_media_duration',return_value=5), \
                     patch('backend.app.video_agents.generate_gemini_text',side_effect=[raw,repaired]) as generate:
                    with self.assertRaises(ValueError):
                        quality.screen_shot(path,{'id':'one','video':'clip.mp4','duration':5})
                    self.assertEqual(generate.call_count,2)
                files=list((path/'assets/quality_reviews').glob('*.json'))
                self.assertEqual(len(files),2)
                self.assertTrue(all(p.name.endswith(('.response.json','.rejected.json')) for p in files))
                diagnostic=json.loads(next(p for p in files if p.name.endswith('.rejected.json')).read_text(encoding='utf8'))
                self.assertEqual(diagnostic['model_outputs'],{'original':raw,'repaired':repaired})
                if repaired==raw:
                    self.assertIn('镜头 one',diagnostic['error'])
                    self.assertIn('repaired_content',diagnostic['error'])
                self.assertEqual((path/'clip.mp4').read_bytes(),b'original video')

    def test_provider_envelope_failure_is_identified_without_saving_body_or_retry(self):
        private='PRIVATE_PROVIDER_BODY'
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/'clip.mp4').write_bytes(b'original video')
            with patch.object(quality,'_sheets',return_value=[]), \
                 patch('backend.app.pipeline.probe_media_duration',return_value=5), \
                 patch('backend.app.video_agents.generate_gemini_text',
                       side_effect=json.JSONDecodeError(private,private,1)) as generate:
                with self.assertRaisesRegex(ValueError,'provider_response') as raised:
                    quality.screen_shot(path,{'id':'one','video':'clip.mp4','duration':5})
                generate.assert_called_once()
                self.assertNotIn(private,str(raised.exception))
            files=list((path/'assets/quality_reviews').glob('*.json'))
            self.assertEqual(len(files),1)
            self.assertTrue(files[0].name.endswith('.rejected.json'))
            self.assertNotIn(private,files[0].read_text(encoding='utf8'))

    def test_frozen_composition_is_compared_and_latest_storyboard_is_not_used(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/'clip.mp4').write_bytes(b'fixture')
            Image.new('RGB',(16,16),'red').save(path/'frozen.png')
            Image.new('RGB',(16,16),'blue').save(path/'latest.png')
            digest=hashlib.sha256((path/'frozen.png').read_bytes()).hexdigest()
            shot={'id':'one','video':'clip.mp4','duration':5,'image':'latest.png',
                'video_request':{'prompt':'A continuous view of the same room.',
                    'input_manifest':[{'role':'composition','file':'frozen.png','sha256':digest,'label':'<Picture 1>'}]}}
            def ask(system,evidence,media):
                from backend.app.gemini_client import _reference_images
                self.assertEqual(len(_reference_images(media)),9)
                self.assertEqual(evidence['image_order'][0]['sha256'],digest)
                self.assertEqual(evidence['image_order'][1],{'kind':'video_sample','time':0})
                self.assertEqual(evidence['submitted_prompt'],shot['video_request']['prompt'])
                return {'issues':[{'code':'scene_continuity','time':2,'evidence':'The submitted room becomes a forest.'}],
                    'uncertainties':[], 'observations':[{'time':t,'description':'Room then forest'} for t in quality.sample_times(5,8)]}
            with patch.object(quality,'_sheets',return_value=[{'mime_type':'image/jpeg','data':'sample'}]*8), \
                 patch('backend.app.pipeline.probe_media_duration',return_value=5):
                first=quality.screen_shot(path,shot,ask=ask)
                Image.new('RGB',(16,16),'green').save(path/'latest.png')
                self.assertEqual(first,quality.screen_shot(path,shot,ask=Mock(side_effect=AssertionError('cache missed'))))
                Image.new('RGB',(16,16),'blue').save(path/'frozen.png')
                with self.assertRaisesRegex(ValueError,'已变化'):
                    quality.screen_shot(path,shot,ask=Mock(side_effect=AssertionError('bad input reached model')))
            self.assertEqual(first['composition_reference']['sha256'],digest)
            self.assertFalse(first['quality_guaranteed'])

    def test_reference_path_and_hash_are_verified_before_cache_or_model(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/'clip.mp4').write_bytes(b'fixture')
            for name in ('../outside.png','missing.png',''):
                shot={'id':'one','video':'clip.mp4','duration':5,'video_request':{'input_manifest':[
                    {'role':'composition','file':name,'sha256':'0'*64}]}}
                ask=Mock()
                with self.subTest(name=name),patch('backend.app.pipeline.probe_media_duration',return_value=5):
                    with self.assertRaises(ValueError):quality.screen_shot(path,shot,ask=ask)
                ask.assert_not_called()

    def test_invalid_observations_are_kept_as_diagnostics_never_as_a_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/'clip.mp4').write_bytes(b'fixture')
            invalid={'observations':[],'issues':[],'uncertainties':[]}
            with patch.object(quality,'_sheets',return_value=[]), \
                 patch('backend.app.pipeline.probe_media_duration',return_value=5):
                with self.assertRaisesRegex(ValueError,'逐张'):
                    quality.screen_shot(path,{'id':'one','video':'clip.mp4','duration':5},ask=lambda *args:invalid)
            files=list((path/'assets/quality_reviews').glob('*.json'))
            self.assertEqual(len(files),1)
            self.assertTrue(files[0].name.endswith('.rejected.json'))
            self.assertEqual(json.loads(files[0].read_text(encoding='utf8'))['response'],invalid)

    def test_malformed_or_unlocated_claims_never_pass(self):
        for value in ({}, {'issues':[]}, {'issues':[{'code':'anatomy','time':float('nan'),'evidence':'bad'}],'uncertainties':[]},
                      {'issues':[{'code':'speech','time':1,'evidence':'not heard'}],'uncertainties':[]}):
            with self.assertRaises(ValueError):quality.validate_review(value,5)

    def test_cache_is_bound_to_actual_clip_and_story_not_just_shot_id(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);clip=path/'clip.mp4';clip.write_bytes(b'first clip')
            shot={'id':'one','kind':'video','video':'clip.mp4','duration':5,'action':'Pick up cup'}
            ask=Mock(return_value={'issues':[],'uncertainties':['brief occlusion'],
                'observations':[{'time':time,'description':'visible subject'} for time in quality.sample_times(5)]})
            with patch.object(quality,'_sheets',return_value=[{'mime_type':'image/jpeg','data':'fixture'}]), \
                 patch('backend.app.pipeline.probe_media_duration',return_value=5):
                first=quality.screen_shot(path,shot,ask=ask)
                self.assertEqual(quality.screen_shot(path,shot,ask=ask),first)
                self.assertEqual(ask.call_count,1)
                clip.write_bytes(b'new clip');quality.screen_shot(path,shot,ask=ask)
                shot['action']='Put down cup';quality.screen_shot(path,shot,ask=ask)
                self.assertEqual(ask.call_count,3)
                self.assertFalse(first['quality_guaranteed']);self.assertFalse(first['audio_checked'])
                with patch.object(quality,'SYSTEM',quality.SYSTEM+'\nUpdated scene review rule.'):
                    quality.screen_shot(path,shot,ask=ask)
                self.assertEqual(ask.call_count,4)
                shot['video']='../outside.mp4'
                with self.assertRaises(ValueError):quality.screen_shot(path,shot,ask=ask)

    def test_native_frozen_actions_and_actual_tail_are_reviewed_and_cache_invalidates(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/'clip.mp4').write_bytes(b'fixture')
            plan={'scene':'One bell is fixed to the wall.'}
            shot={'id':'one','video':'clip.mp4','duration':7,
                  'video_request':{'dialogue':{'plan':plan}},
                  'continuity_states':[{'prop_id':'bell','end':'wall'}]}
            times=quality.sample_times(7.291667)
            ask=Mock(return_value={'issues':[],'uncertainties':[],
                'observations':[{'time':t,'description':'One bell on wall'} for t in times]})
            with patch.object(quality,'_sheets',return_value=[]), \
                 patch('backend.app.pipeline.probe_media_duration',return_value=7.291667):
                first=quality.screen_shot(path,shot,ask=ask)
                self.assertEqual(first['sample_times'][-1],7.242)
                self.assertEqual(ask.call_args.args[1]['native_dialogue'],plan)
                shot['continuity_states'][0]['end']='hand'
                self.assertNotEqual(quality.screen_shot(path,shot,ask=ask)['fingerprint'],first['fingerprint'])
                plan['scene']='The character moves the bell to the lower wall.'
                quality.screen_shot(path,shot,ask=ask)
                self.assertEqual(ask.call_count,3)
                cache=path/'assets/quality_reviews'/f"{first['fingerprint']}.json"
                saved=json.loads(cache.read_text(encoding='utf8'));saved['review']['observations']=[]
                cache.write_text(json.dumps(saved),encoding='utf8')
                shot['continuity_states'][0]['end']='wall'
                plan['scene']='One bell is fixed to the wall.'
                with self.assertRaisesRegex(ValueError,'逐张'):quality.screen_shot(path,shot,ask=ask)

    def test_native_modes_do_not_skip_visual_review_without_legacy_staging_context(self):
        from contextlib import nullcontext
        for mode in ('dialogue','mixed'):
            record={'creation_parameters':{'speech_mode':mode},'shots':[{'id':'one','kind':'video'}]}
            with self.subTest(mode=mode), \
                 patch('backend.app.language_routing.project_language_scope',return_value=nullcontext()), \
                 patch('backend.app.video_studio.planning_parameters',return_value={}), \
                 patch('backend.app.video_studio.production_prompt_context',side_effect=AssertionError('legacy context not needed')), \
                 patch.object(quality,'screen_shot',return_value={'review':{'issues':[]}}) as screen:
                self.assertEqual(len(quality.check_automatic_export(Path('.'),record,1)),1)
                screen.assert_called_once()

    def test_invalid_actual_duration_never_reaches_model(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/'clip.mp4').write_bytes(b'fixture')
            for duration in (float('nan'),float('inf'),0,18):
                ask=Mock()
                with self.subTest(duration=duration),patch('backend.app.pipeline.probe_media_duration',return_value=duration):
                    with self.assertRaises(ValueError):quality.screen_shot(path,{'id':'one','video':'clip.mp4','duration':7,
                        'video_request':{'dialogue':{'plan':{'duration':7}}}},ask=ask)
                ask.assert_not_called()

    def test_narration_long_source_only_reviews_exported_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/'clip.mp4').write_bytes(b'fixture')
            shot={'id':'one','video':'clip.mp4','duration':5}
            ask=Mock(return_value={'issues':[],'uncertainties':[],
                'observations':[{'time':t,'description':'Visible scene'} for t in quality.sample_times(5)]})
            with patch.object(quality,'_sheets',return_value=[]) as samples, \
                 patch('backend.app.pipeline.probe_media_duration',return_value=30):
                result=quality.screen_shot(path,shot,ask=ask)
            self.assertEqual(samples.call_args.args[1],5)
            self.assertEqual(result['actual_duration'],30)
            self.assertEqual(result['reviewed_duration'],5)

    def test_failed_visual_review_never_renders_or_marks_complete(self):
        record={'id':'one','revision':1,'logs':[],'status':'exporting'}
        active={'path'}
        with patch.object(export.studio,'read',side_effect=lambda p:copy.deepcopy(record)), \
             patch.object(export.studio,'save') as save, patch.object(export.studio,'ACTIVE',active), \
             patch.object(export,'_render') as render, \
             patch.object(quality,'check_automatic_export',side_effect=ValueError('extra hands at 2s')):
            export._worker('path',1,True)
        render.assert_not_called()
        self.assertEqual(save.call_args.args[1]['status'],'export_failed')
        self.assertIn('extra hands',save.call_args.args[1]['error'])
        self.assertNotIn('path',active)

    def test_coordinator_requests_review_on_automatic_export(self):
        with patch.object(export,'start_export_for_user') as start, \
             patch.object(export.studio,'storyboard_review_error',return_value=''):
            one_click.dispatch('export',{'id':1},{'id':'a'*32,'revision':2,
                'creation_parameters':{'quality_review_policy':'strict'}})
        self.assertIs(start.call_args.kwargs['review_generated'],True)

    def test_manual_observation_export_remains_available_without_quality_claim(self):
        record={'id':'one','revision':1,'logs':[],'status':'exporting'}
        with patch.object(export.studio,'read',side_effect=lambda p:copy.deepcopy(record)), \
             patch.object(export.studio,'save'),patch.object(export.studio,'ACTIVE',set()), \
             patch.object(export,'_render',return_value=('out','raw','sub')), \
             patch.object(export,'reconcile_source_job'),patch.object(quality,'check_automatic_export') as check:
            export._worker('path',1)
        check.assert_not_called()

if __name__=='__main__':unittest.main()
