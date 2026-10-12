import threading
import unittest
from unittest.mock import patch

from backend.app import video_generation as generation


class LocalComputeSlotTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(generation, 'LOCAL_VIDEO_COMPUTE_LOCK', threading.Lock())
        guard.start()
        self.addCleanup(guard.stop)

    def test_other_project_cannot_prepare_tts_until_h3_run_finishes(self):
        waiting = threading.Event()
        entered = threading.Event()
        stopped = threading.Event()
        calls = []

        def second_project():
            with generation._local_compute_slot(stopped, waiting.set) as admitted:
                calls.append(admitted)
                entered.set()

        with generation._local_compute_slot(threading.Event()) as first:
            self.assertTrue(first)
            thread = threading.Thread(target=second_project)
            thread.start()
            self.assertTrue(waiting.wait(2))
            self.assertFalse(entered.is_set())
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [True])

    def test_waiting_project_can_stop_without_preparing_or_cancelling_owner(self):
        waiting = threading.Event()
        cancelled = threading.Event()
        calls = []

        def second_project():
            with generation._local_compute_slot(cancelled, waiting.set) as admitted:
                calls.append(admitted)

        with generation._local_compute_slot(threading.Event()):
            thread = threading.Thread(target=second_project)
            thread.start()
            self.assertTrue(waiting.wait(2))
            cancelled.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(calls, [False])
            self.assertTrue(generation.LOCAL_VIDEO_COMPUTE_LOCK.locked())
        self.assertFalse(generation.LOCAL_VIDEO_COMPUTE_LOCK.locked())

    def test_failure_releases_slot_and_pre_cancelled_run_does_not_claim_it(self):
        with self.assertRaisesRegex(RuntimeError, 'preparation failed'):
            with generation._local_compute_slot(threading.Event()):
                raise RuntimeError('preparation failed')
        cancelled = threading.Event()
        cancelled.set()
        with generation._local_compute_slot(cancelled) as admitted:
            self.assertFalse(admitted)
        with generation._local_compute_slot(threading.Event()) as admitted:
            self.assertTrue(admitted)
