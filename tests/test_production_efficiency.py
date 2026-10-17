"""Standalone enhancement checks without a host, credentials or model requests."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]/'plugins/ocv_community/extension_src/backend/app'

def modules():
    package=types.ModuleType('efficiency_test_host');package.__path__=[str(ROOT)]
    sys.modules[package.__name__]=package
    result=[]
    for name in ('production_metrics','video_review_execution','video_review_recovery'):
        qualified=package.__name__+'.'+name
        spec=importlib.util.spec_from_file_location(qualified,ROOT/(name+'.py'))
        module=importlib.util.module_from_spec(spec);sys.modules[qualified]=module
        spec.loader.exec_module(module);result.append(module)
    return result

metrics,execution,recovery=modules()

class EfficiencyTests(unittest.TestCase):
    def test_read_without_data_does_not_create_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'absent'
            self.assertEqual(metrics.summary(path)['requests'],0)
            self.assertFalse(path.exists())

    def test_failures_preserve_independent_result_and_context(self):
        with tempfile.TemporaryDirectory() as folder,metrics.production_scope(folder,'outer'):
            def success():
                metrics.cache_hit('private-path');return 'preserved'
            def fail():raise ValueError('contract rejected')
            result={name:(value,error) for name,value,error in execution.run_reviews({'one':success,'two':fail})}
            self.assertEqual(result['one'],('preserved',None))
            self.assertIsInstance(result['two'][1],ValueError)
            self.assertEqual(metrics._SCOPE.get()['stage'],'outer')
            self.assertEqual(metrics.summary(folder)['cache_hits'],1)
            self.assertNotIn('private-path',(Path(folder)/metrics.FILE).read_text())

    def test_retry_binds_receipt_and_does_not_treat_unknown_as_failed(self):
        original={'status':'failed','failure':{'kind':'invalid_contract'},'attempt_id':'first'}
        first=recovery.recovery(original)
        self.assertTrue(first['retry_available'])
        self.assertNotEqual(first['retry_token'],recovery.retry_token({**original,'attempt_id':'second'}))
        unknown=recovery.recovery({'status':'dispatching'})
        self.assertFalse(unknown['retry_available'])
        self.assertEqual(unknown['outcome'],'unknown')

if __name__=='__main__':unittest.main()
