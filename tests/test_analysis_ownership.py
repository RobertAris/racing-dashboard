"""A lap history has one writer/worker owner, including across processes."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from racing_dashboard.analysis import AnalysisService
from tests.test_analysis import lap
from tests.test_discussion_http import FakeDiscussionConnection


class AnalysisOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)/"history"
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.connection.release.set()
            service.close()
        self.temp.cleanup()

    def service(self, *, active=False):
        service = AnalysisService(self.folder, connection=FakeDiscussionConnection(), active=active)
        self.services.append(service)
        return service

    def test_empty_service_is_lazy_and_closed_service_cannot_reacquire(self):
        service = self.service()
        service.state()
        self.assertFalse(self.folder.exists())
        self.assertIsNone(service._store_fd)
        service.close()
        service.close()
        with self.assertRaisesRegex(ValueError, "closed"):
            with service._database():
                pass
        self.assertFalse(self.folder.exists())

    def test_second_instance_cannot_requeue_an_inflight_paid_turn(self):
        first = self.service(active=True)
        first.ingest(lap())
        first.connection.block = True
        first.discuss("test-session:1", {"message": "Explain this captured lap"})
        self.assertTrue(first.connection.started.wait(2))
        with self.assertRaisesRegex(ValueError, "already open"):
            self.service(active=True)
        self.assertEqual(len(first.connection.requests), 1)
        self.assertEqual(first.get("test-session:1")["discussion"]["status"], "analyzing")
        first.connection.release.set()

    def test_another_process_cannot_open_an_owned_history(self):
        owner = self.service()
        owner.ingest(lap())
        script = """
import sys
from racing_dashboard.analysis import AnalysisService
try:
    service = AnalysisService(sys.argv[1])
except ValueError as error:
    print(str(error))
else:
    service.close()
    raise SystemExit('Second process unexpectedly acquired history')
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.folder)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, check=True, timeout=5)
        self.assertIn("already open", result.stdout)
        self.assertEqual(owner.get("test-session:1")["lap"]["lap_number"], 1)

    def test_configure_owns_settings_without_creating_a_database(self):
        first = self.service()
        first.configure({"enabled": False})
        self.assertFalse(first.path.exists())
        before = first.settings_path.read_text()
        second = self.service()
        with self.assertRaisesRegex(ValueError, "already open"):
            second.configure({"enabled": True})
        self.assertEqual(first.settings_path.read_text(), before)
        self.assertFalse(first.path.exists())

    def test_close_releases_ownership_and_preserves_lock_inode_and_private_mode(self):
        first = self.service()
        first.ingest(lap())
        lock = self.folder/".lap_engine.lock"
        inode = lock.stat().st_ino
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        first.close()
        first.close()
        self.assertTrue(lock.exists())
        self.assertIsNone(first._store_fd)
        second = self.service()
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(second.get("test-session:1")["lap"]["id"], "test-session:1")

    def test_close_retains_ownership_until_an_inflight_request_has_exited(self):
        first = self.service(active=True)
        first.ingest(lap())
        first.connection.block = True
        first.discuss("test-session:1", {"message": "Explain this captured lap"})
        self.assertTrue(first.connection.started.wait(2))
        first.close()
        self.assertIsNotNone(first._store_fd)
        self.assertTrue(first.discussion_thread.is_alive())
        with self.assertRaisesRegex(ValueError, "already open"):
            self.service(active=True)
        self.assertEqual(len(first.connection.requests), 1)
        first.connection.release.set()
        deadline = time.monotonic()+3
        while first._store_fd is not None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIsNone(first._store_fd)
        replacement = self.service()
        self.assertEqual(replacement.get("test-session:1")["discussion"]["status"], "queued")

    def test_symlink_lock_is_rejected_without_modifying_its_target(self):
        self.folder.mkdir()
        target = Path(self.temp.name)/"other-file"
        target.write_text("private fixture")
        os.chmod(target, 0o644)
        (self.folder/".lap_engine.lock").symlink_to(target)
        service = self.service()
        with self.assertRaisesRegex(ValueError, "safely"):
            service.ingest(lap())
        self.assertEqual(target.read_text(), "private fixture")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertFalse(service.path.exists())


if __name__ == "__main__":
    unittest.main()
