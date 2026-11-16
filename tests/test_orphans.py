"""No signal the orphan kill sends reaches the wrong process.

Driven against real child processes, because what is under test is the
kernel's answer to a re-read identity, which a fake process table could not
give.
"""

from __future__ import annotations

import signal
import subprocess
import unittest
from pathlib import Path

from runner_liveness import orphans, procfs


@unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc")
class SignalIfSameTest(unittest.TestCase):
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


@unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc")
class KillOrphanListenerTest(unittest.TestCase):
    def test_a_pid_reused_since_it_was_found_is_not_signalled_nor_its_parent(
        self,
    ) -> None:
        # The watchdog scans /proc between finding a listener and killing it,
        # long enough for the pid to come back as another process. An identity
        # captured at kill time would vouch for that process and send its parent
        # a root SIGKILL; the identity found at enumeration must refuse it.
        # The live process here stands in for the reuser, under a shell parent
        # of its own, so a regression kills the shell rather than this test.
        shell = subprocess.Popen(
            ["sh", "-c", "sleep 300 & echo $!; wait"],
            stdout=subprocess.PIPE,
            text=True,
        )
        reuser = 0
        try:
            assert shell.stdout is not None
            reuser = int(shell.stdout.readline())
            found_earlier = procfs.Process(reuser, "an identity from before reuse")
            messages: list[str] = []
            killed = orphans.kill_orphan_listener(
                "unit", found_earlier, lambda: True, False, messages.append
            )
            self.assertFalse(killed)
            self.assertIsNone(shell.poll())
            self.assertIsNotNone(procfs.process_identity(reuser))
            self.assertTrue(messages)
        finally:
            reuser_identity = procfs.process_identity(reuser) if reuser else None
            shell.kill()
            shell.wait()
            if reuser_identity is not None:
                orphans.signal_if_same(reuser, reuser_identity, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
