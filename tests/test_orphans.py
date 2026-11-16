"""No signal the orphan kill sends reaches the wrong process.

Driven against a real child process, because what is under test is the kernel's
answer to a re-read identity, which a fake process table could not give.
"""

from __future__ import annotations

import signal
import subprocess
import unittest
from pathlib import Path

from runner_liveness import orphans, procfs


class SignalIfSameTest(unittest.TestCase):
    @unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc")
    def test_a_signal_never_reaches_a_process_with_another_identity(self) -> None:
        # The KILL after the orphan's grace period lands on whatever holds the
        # pid by then; an identity that no longer matches must stop it.
        child = subprocess.Popen(["sleep", "30"])
        try:
            identity = procfs.process_identity(child.pid)
            self.assertIsNotNone(identity)
            self.assertFalse(
                orphans.signal_if_same(child.pid, "not-it", signal.SIGKILL)
            )
            self.assertIsNone(child.poll())
            assert identity is not None
            self.assertTrue(orphans.signal_if_same(child.pid, identity, signal.SIGKILL))
            self.assertEqual(child.wait(timeout=10), -signal.SIGKILL)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


if __name__ == "__main__":
    unittest.main()
