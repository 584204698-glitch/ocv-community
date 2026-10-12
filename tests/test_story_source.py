"""Execute the actual create_job route body before its media/config boundary.

The full application imports media runtimes and initializes global stores. This
test compiles only the real route function, retaining all its statements, with
explicit boundary doubles. It does not claim full-app HTTP integration coverage.
"""

import ast
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

from backend.app import story_workspace as stories


STORY = "阿宁把门口的木凳搬到屋檐下，因为她记得送信人常在那里歇脚。傍晚，木凳上多了一封没有被雨淋湿的信。"


class ConfigBoundaryReached(Exception):
    """Stop a successful source check before any runtime/service work."""


class StorySourceJobBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "backend" / "app" / "main.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "create_job")
        function.decorator_list = []
        isolated = ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            function,
        ], type_ignores=[])
        cls.route_code = compile(ast.fix_missing_locations(isolated), str(path), "exec")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.request = Request({"type": "http", "headers": []})
        for name, value in (("ROOT", Path(temporary.name)), ("require_user", lambda request: {"id": 1})):
            handle = patch.object(stories, name, value)
            handle.start()
            self.addCleanup(handle.stop)
        self.generator = Mock(side_effect=AssertionError("No model calls are allowed in source tests"))
        handle = patch.object(stories, "_generate_story", self.generator)
        handle.start()
        self.addCleanup(handle.stop)

        record = stories.create_story(stories.CreateStory(
            id="c" * 32, intent="manuscript", title="雨中的木凳", story=STORY,
        ), self.request)
        self.source = stories.adopt_story(record["id"], stories.StoryRevision(revision=record["revision"]), self.request)["source"]

        self.configuration = Mock(side_effect=ConfigBoundaryReached)
        self.cloud = Mock(side_effect=AssertionError("Cloud work started before source validation"))
        self.store = Mock()
        self.resolver = Mock(wraps=stories.resolve_story_source)
        namespace = {
            "HTTPException": HTTPException,
            "require_user": lambda request: {"id": 1},
            "MAX_SCRIPT_CHARACTERS": 12000,
            "normalize_project_name": lambda value: value or "新作品",
            "resolve_story_source": self.resolver,
            "_required_job_config_error": self.configuration,
            "cloud_client_for": self.cloud,
            "store": self.store,
        }
        exec(self.route_code, namespace)
        self.route = namespace["create_job"]

    def payload(self, *, script=STORY, include_source=True):
        data = {"project_name": "来源边界测试", "script": script,
                "tts_engine": "qwen", "dynamic_video": True, "use_cloud_image_pool": True}
        if include_source:
            data["story_source"] = copy.deepcopy(self.source)
        # Only the two request-model methods read by the route are supplied;
        # request schema validation is covered separately at the story API.
        return SimpleNamespace(model_fields_set=set(data), model_dump=lambda: copy.deepcopy(data))

    def test_mismatched_source_stops_before_configuration_cloud_or_job_creation(self):
        with self.assertRaises(HTTPException) as caught:
            self.route(self.payload(script=STORY + "这是没有采用的新结尾。"), self.request)
        self.assertEqual(caught.exception.status_code, 409)
        self.configuration.assert_not_called()
        self.cloud.assert_not_called()
        self.assertEqual(self.store.mock_calls, [])
        self.generator.assert_not_called()

    def test_valid_source_is_frozen_before_configuration_is_checked(self):
        with self.assertRaises(ConfigBoundaryReached):
            self.route(self.payload(), self.request)
        data = self.configuration.call_args.args[0]
        snapshot = data["story_snapshot"]
        for key, value in {**self.source, "title": "雨中的木凳", "story": STORY}.items():
            self.assertEqual(snapshot[key], value)
        self.assertEqual(snapshot['snapshot_version'], 1)
        self.assertEqual(snapshot['brief']['intent'], 'manuscript')
        self.assertRegex(snapshot['snapshot_sha256'], r'^[a-f0-9]{64}$')
        self.assertEqual(data["script"], STORY)
        self.assertTrue(data["step_mode"])
        self.cloud.assert_not_called()
        self.assertEqual(self.store.mock_calls, [])
        self.generator.assert_not_called()

    def test_legacy_script_without_story_source_keeps_existing_route(self):
        with self.assertRaises(ConfigBoundaryReached):
            self.route(self.payload(include_source=False), self.request)
        self.resolver.assert_not_called()
        data = self.configuration.call_args.args[0]
        self.assertNotIn("story_snapshot", data)
        self.assertEqual(data["script"], STORY)
        self.cloud.assert_not_called()
        self.assertEqual(self.store.mock_calls, [])
        self.generator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
