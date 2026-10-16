import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

limits=load('limits','plugins/ocv_community/maintenance_src/backend/app/image_reference_limits.py')
privacy=load('privacy','tools/check_release_privacy.py')

class MaintenanceTests(unittest.TestCase):
    def test_existing_model_capacity_and_no_reference_provider(self):
        self.assertEqual(limits.reference_limit({}),4)
        self.assertEqual(limits.reference_limit({'reference_images':False}),0)
        for bad in (True,0,17,'8'):
            with self.assertRaises(ValueError):limits.reference_limit({'max_reference_images':bad})
        with self.assertRaises(ValueError):limits.validate_reference_count(5,{})

    def test_capacity_updates_stay_with_the_same_model(self):
        self.assertEqual(limits.profile_reference_limit({'model_id':'a'},{'model_id':'a','max_reference_images':8}),8)
        self.assertEqual(limits.profile_reference_limit({'model_id':'a'},{'model_id':'b','max_reference_images':8}),4)

    def test_privacy_reports_rules_without_echoing_credentials(self):
        key=b'sk-'+b'A'*32
        result=privacy.findings('module.py',b'example='+key)
        self.assertEqual(result,[{'file':'module.py','rule':'provider_key'}])
        self.assertNotIn(key.decode(),str(result))
        self.assertTrue(privacy.findings('workspace/project.json',b'{}'))
        self.assertTrue(privacy.findings('safe.md',b'/home/'+b'l/' + b'project'))
        self.assertFalse(privacy.findings('README.md',b'Configure your own API key in the host.'))

if __name__=='__main__':unittest.main()
