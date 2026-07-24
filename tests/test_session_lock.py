"""Focused stdlib tests for SessionFileLock."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest

# Allow importing project modules without installing a package.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils.session_lock import SessionFileLock, SessionLockError  # noqa: E402


class SessionFileLockTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.session_path = os.path.join(self._tmpdir.name, "acc1")

    def test_lock_path_normalizes_session_suffix(self):
        lock = SessionFileLock(self.session_path)
        self.assertTrue(lock.session_file.endswith(".session"))
        self.assertTrue(lock.lock_file.endswith(".session.lock"))

        lock2 = SessionFileLock(self.session_path + ".session")
        self.assertEqual(lock.session_file, lock2.session_file)
        self.assertEqual(lock.lock_file, lock2.lock_file)

    def test_exclusive_lock_blocks_second_acquirer(self):
        first = SessionFileLock(self.session_path)
        first.acquire(timeout=0)
        self.assertTrue(first.locked)

        second = SessionFileLock(self.session_path)
        with self.assertRaises(SessionLockError):
            second.acquire(timeout=0)

        first.release()
        self.assertFalse(first.locked)

        second.acquire(timeout=0)
        self.assertTrue(second.locked)
        second.release()

    def test_context_manager_releases(self):
        with SessionFileLock(self.session_path) as lock:
            self.assertTrue(lock.locked)
            blocked = SessionFileLock(self.session_path)
            with self.assertRaises(SessionLockError):
                blocked.acquire(timeout=0)
        # After exit, another process/thread can take it
        again = SessionFileLock(self.session_path)
        again.acquire(timeout=0)
        again.release()

    def test_double_release_is_safe(self):
        lock = SessionFileLock(self.session_path)
        lock.acquire(timeout=0)
        lock.release()
        lock.release()  # no exception

    def test_cross_thread_exclusion(self):
        holder = SessionFileLock(self.session_path)
        holder.acquire(timeout=0)
        errors = []

        def try_lock():
            other = SessionFileLock(self.session_path)
            try:
                other.acquire(timeout=0)
                errors.append("should_not_acquire")
                other.release()
            except SessionLockError:
                errors.append("blocked")

        t = threading.Thread(target=try_lock)
        t.start()
        t.join(timeout=5)
        self.assertEqual(errors, ["blocked"])
        holder.release()


if __name__ == "__main__":
    unittest.main()
