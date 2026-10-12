import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import video_dialogue as dialogue
from backend.app.h3_voice_reference import POLICY


def frozen(basis):
    return {'basis':basis,'fingerprint':hashlib.sha256(
        json.dumps(basis,ensure_ascii=False,sort_keys=True).encode()).hexdigest()}


class LegacyObservationTests(unittest.TestCase):
    def setUp(self):
        self.basis={'plan':{'words':'您好','start':.5},'voices':[{'voice_id':'voice','sha256':'original'}],
                    'identities':{'one':'identity-sha'},'images':[{'sha256':'core-sha'}]}
        self.old=frozen(copy.deepcopy(self.basis))
        self.current=frozen({**copy.deepcopy(self.basis),'reference_audio_policy':POLICY})

    def test_only_explicit_observation_allows_unchanged_legacy_voice_excerpt_policy(self):
        record={'creation_parameters':{'speech_mode':'dialogue'},'shots':[{
            'id':'shot_003','kind':'video','video_status':'completed',
            'video_request':{'audio_policy':{'mode':'native_dialogue'},'dialogue':self.old}}]}
        with patch('backend.app.video_inputs.resolve_video_inputs',return_value=({},[])), \
             patch.object(dialogue,'prepare_dialogue',return_value=self.current):
            with self.assertRaises(ValueError):dialogue.assert_native_export_current(1,Path('.'),record)
            self.assertEqual(dialogue.assert_native_export_current(1,Path('.'),record,observation_only=True),['shot_003'])
            self.assertNotIn('reference_audio_policy',record['shots'][0]['video_request']['dialogue']['basis'])

    def test_observation_never_allows_changed_words_voice_identity_image_or_timing(self):
        variants=[('plan',{'words':'别说话','start':.5}),('plan',{'words':'您好','start':2}),
                  ('voices',[{'voice_id':'other','sha256':'new'}]),('identities',{'one':'changed'}),
                  ('images',[{'sha256':'replacement'}])]
        for key,value in variants:
            with self.subTest(key=key,value=value):
                current=copy.deepcopy(self.current);current['basis'][key]=value
                self.assertFalse(dialogue._legacy_voice_policy_only(self.old,current))

    def test_corrupt_unknown_or_already_migrated_fingerprints_do_not_qualify(self):
        bad=copy.deepcopy(self.old);bad['fingerprint']='fabricated'
        self.assertFalse(dialogue._legacy_voice_policy_only(bad,self.current))
        for policy in (POLICY,'unknown'):
            old=frozen({**self.basis,'reference_audio_policy':policy})
            self.assertFalse(dialogue._legacy_voice_policy_only(old,self.current))

    def test_nonvisual_source_correction_requires_exact_recompiled_original_assets_and_explicit_observation(self):
        old_plan={'scene':'original description','words':'您好','start':.5}
        old=frozen({**copy.deepcopy(self.basis),'plan':old_plan})
        record={'creation_parameters':{'speech_mode':'dialogue'},'shots':[{
            'id':'shot_003','kind':'video','video_status':'completed','native_dialogue':{'scene':'corrected description'},
            'video_request':{'audio_policy':{'mode':'native_dialogue'},'dialogue':old}}]}
        for changed in (False,True):
            restored=copy.deepcopy(old)
            if changed:restored=frozen({**restored['basis'],'images':[{'sha256':'changed-pixels'}]})
            def prepare(*args,**kwargs):return restored if args[3]['native_dialogue']==old_plan else self.current
            with patch('backend.app.video_inputs.resolve_video_inputs',return_value=({},[])), \
                 patch('backend.app.video_dynamic_review.reviewed_nonvisual_original_plan',return_value=old_plan), \
                 patch.object(dialogue,'prepare_dialogue',side_effect=prepare):
                if changed:
                    with self.assertRaises(ValueError):dialogue.assert_native_export_current(1,Path('.'),record,observation_only=True)
                else:
                    self.assertEqual(dialogue.assert_native_export_current(1,Path('.'),record,observation_only=True),[])
                with self.assertRaises(ValueError):dialogue.assert_native_export_current(1,Path('.'),record)
                self.assertEqual(record['shots'][0]['video_request']['dialogue'],old)
