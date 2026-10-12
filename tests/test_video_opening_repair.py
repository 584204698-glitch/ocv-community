import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.video_opening_repair import capture, prepare, MARKER


class OpeningRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        (self.path / 'core.jpg').write_bytes(b'original-image')
        self.shot = dict(image='core.jpg', image_prompt='水粉绘本，女孩站在石阶上。',
                         visual_description='女孩独自站在石阶上，持竹枝。',
                         action='完成一道剑式。', continuity_states=[], cast_plan={},
                         video_error='H3 请求准备失败：' + MARKER + '双脚在石阶前的平地。')

    def test_existing_conflict_becomes_local_edit_without_changing_plan(self):
        original = copy.deepcopy(self.shot)
        report = capture(self.path, self.shot)
        image, prompt = prepare(self.path, self.shot)
        self.assertEqual(report['image_sha256'], hashlib.sha256(b'original-image').hexdigest())
        self.assertEqual(image, (self.path / 'core.jpg').resolve())
        self.assertIn(original['visual_description'], prompt)
        self.assertIn('错误现状', prompt)
        self.assertIn('不提前画出后续动作结果', prompt)
        self.shot.pop('opening_image_repair')
        self.assertEqual(self.shot, original)

    def test_unrelated_errors_do_not_offer_image_repair(self):
        self.shot['video_error'] = '开场对接改变了角色名单或顺序'
        self.assertIsNone(capture(self.path, self.shot))

    def test_replacement_image_rejects_old_diagnosis(self):
        capture(self.path, self.shot)
        (self.path / 'core.jpg').write_bytes(b'new-image')
        with self.assertRaisesRegex(ValueError, '旧开场诊断'):
            prepare(self.path, self.shot)

    def test_changed_approved_design_rejects_old_diagnosis(self):
        capture(self.path, self.shot)
        self.shot['action'] = '改为走下台阶'
        with self.assertRaises(ValueError):
            prepare(self.path, self.shot)

    def test_other_project_or_unsafe_image_is_not_reused(self):
        capture(self.path, self.shot)
        self.shot['image'] = '../outside.jpg'
        with self.assertRaises(ValueError):
            prepare(self.path, self.shot)

    def test_web_repair_uses_frozen_image_and_keeps_script_and_other_shots(self):
        from backend.app import video_studio as studio
        capture(self.path, self.shot)
        self.shot.update(id='shot_003', image_status='completed')
        other = dict(id='shot_004', image='other.jpg', video='other.mp4')
        record = dict(id='project', revision=4, status='image_review',
                      logs=[], shots=[self.shot, other])
        before = copy.deepcopy(record['shots'])
        with patch.object(studio, 'require_user', return_value={'id': 1}), \
                patch.object(studio, 'directory', return_value=self.path), \
                patch.object(studio, 'read', return_value=record), \
                patch.object(studio, 'editable'), \
                patch.object(studio, 'project_has_image_edits', return_value=False), \
                patch.object(studio, '_image_configs', return_value=[{}]), \
                patch.object(studio, '_start_storyboard_redraw') as start:
            studio.repair_opening_image('project', 'shot_003', studio.Review(revision=4), None)
        self.assertEqual(record['shots'], before)
        args, kwargs = start.call_args
        self.assertEqual(Path(args[5][0]).read_bytes(), b'original-image')
        self.assertNotEqual(Path(args[5][0]), self.path / 'core.jpg')
        self.assertEqual(kwargs['display_prompt'], self.shot['image_prompt'])
        self.assertEqual(kwargs['task_action'], 'opening_repair')


if __name__ == '__main__':
    unittest.main()
