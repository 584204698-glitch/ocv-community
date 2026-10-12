import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.app import comfyui_bridge as bridge


class DefaultsTests(unittest.TestCase):
    def test_account_defaults_persist_and_do_not_leak_to_other_users(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(bridge,'ROOT',Path(tmp)):
            app=FastAPI();app.include_router(bridge.router);client=TestClient(app)
            payload={'video_generation_backend':'comfyui','comfyui_profile_id':'fast','comfyui_h3_prompt_agent':True}
            with patch.object(bridge,'require_user',return_value={'id':1}),patch.object(bridge,'video_profile',return_value={'kind':'video'}):
                self.assertEqual(client.put('/api/comfyui/creation-defaults',json=payload).status_code,200)
                self.assertEqual(client.get('/api/comfyui/creation-defaults').json()['defaults']['comfyui_profile_id'],'fast')
            with patch.object(bridge,'require_user',return_value={'id':2}):
                self.assertIsNone(client.get('/api/comfyui/creation-defaults').json()['defaults'])

    def test_missing_workflow_cannot_replace_saved_default(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(bridge,'ROOT',Path(tmp)),patch.object(bridge,'require_user',return_value={'id':1}):
            app=FastAPI();app.include_router(bridge.router);client=TestClient(app)
            self.assertEqual(client.put('/api/comfyui/creation-defaults',json={'video_generation_backend':'api'}).status_code,200)
            with patch.object(bridge,'video_profile',side_effect=ValueError('missing profile')):
                self.assertEqual(client.put('/api/comfyui/creation-defaults',json={'video_generation_backend':'comfyui','comfyui_profile_id':'missing'}).status_code,400)
            self.assertEqual(client.get('/api/comfyui/creation-defaults').json()['defaults']['video_generation_backend'],'api')


if __name__=='__main__':unittest.main()
