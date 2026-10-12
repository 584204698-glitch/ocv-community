import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from PIL import Image
from backend.app.video_scene_observation import observe_scene, validate_observation


class SceneObservationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.path=Path(temp.name)
        self.image=self.path/'scene.png';Image.new('RGB',(32,24),'white').save(self.image)
        self.asset={'image':'scene.png','image_prompt':'STORY_EXPECTS_FIVE_OBJECTS','style':'SECRET_STYLE'}
        self.value={'objects':[{'kind':'chair','description':'One visible back and seat on the left.',
                               'box':[0,100,400,900],'text':''}],
                    'appearance':'Clear color blocks.','uncertainties':[]}

    def test_observation_excludes_story_expectations_and_reuses_actual_image_hash(self):
        ask=Mock(return_value=self.value)
        first=observe_scene(self.path,self.asset,ask=ask)
        self.asset['image_prompt']='DIFFERENT_DESIRED_COUNT'
        self.asset['image_version']='untrusted metadata'
        self.assertEqual(observe_scene(self.path,self.asset,ask=ask),first)
        ask.assert_called_once()
        system,task,media=ask.call_args.args
        self.assertNotIn('STORY_EXPECTS',system+str(task))
        self.assertNotIn('SECRET_STYLE',system+str(task))
        self.assertEqual(task,{'task':'describe_visible_scene'})
        self.assertEqual(len(media),1)
        Image.new('RGB',(32,24),'blue').save(self.image)
        self.assertIsNone(observe_scene(self.path,self.asset,ask=ask,cached_only=True))
        self.assertEqual(ask.call_count,1)
        self.assertNotEqual(observe_scene(self.path,self.asset,ask=ask)['image_sha256'],first['image_sha256'])
        self.assertEqual(ask.call_count,2)

    def test_invalid_or_missing_localization_cannot_be_cached_as_valid_observation(self):
        for box in ([0,0,0,10],[0,0,1001,100],[0,float('nan'),30,50],None):
            value={**self.value,'objects':[{**self.value['objects'][0],'box':box}]}
            with self.subTest(box=box),self.assertRaises(ValueError):validate_observation(value)
        ask=Mock(return_value={'objects':[]})
        with self.assertRaises(ValueError):observe_scene(self.path,self.asset,ask=ask)
        self.assertIsNone(observe_scene(self.path,self.asset,cached_only=True))
        self.assertEqual(len(list((self.path/'assets/scene_observations').glob('*.rejected.json'))),1)
