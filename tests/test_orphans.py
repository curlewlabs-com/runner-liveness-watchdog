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
from unittest import mock

from runner_liveness import orphans, procfs
from tests.support import wait_until


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

    def test_an_orphan_that_stopped_being_one_is_not_signalled_or_counted(
        self,
    ) -> None:
        # Between finding the orphan and its TERM it can pick up a job or go;
        # nothing is signalled then, and the pass must not count it as killed.
        shell = subprocess.Popen(
            ["sh", "-c", "sleep 300 & echo $!; wait"],
            stdout=subprocess.PIPE,
            text=True,
        )
        listener = 0
        try:
            assert shell.stdout is not None
            listener = int(shell.stdout.readline())
            identity = procfs.process_identity(listener)
            assert identity is not None
            answers = iter([True])
            messages: list[str] = []
            killed = orphans.kill_orphan_listener(
                "unit",
                procfs.Process(listener, identity),
                lambda: next(answers, False),
                False,
                messages.append,
            )
            self.assertFalse(killed)
            self.assertIsNone(shell.poll())
            self.assertEqual(procfs.process_identity(listener), identity)
            self.assertTrue(any("not signalled" in m for m in messages), messages)
        finally:
            left = procfs.process_identity(listener) if listener else None
            shell.kill()
            shell.wait()
            if left is not None:
                orphans.signal_if_same(listener, left, signal.SIGKILL)

    def test_an_orphan_that_ignores_its_term_is_killed_and_counted(self) -> None:
        # A delivered TERM alone does not make it gone; the KILL after the grace
        # period does, and only then does the pass count it. The listener says
        # its pid only once it ignores TERM, so the TERM cannot beat the trap.
        shell = subprocess.Popen(
            [
                "sh",
                "-c",
                "sh -c 'trap \"\" TERM; echo $$; exec sleep 300' & wait",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        listener = 0
        sent: list[signal.Signals] = []
        real_signal_if_same = orphans.signal_if_same

        def recorded(pid: int, identity: str, signum: signal.Signals) -> bool:
            sent.append(signum)
            return real_signal_if_same(pid, identity, signum)

        try:
            assert shell.stdout is not None
            listener = int(shell.stdout.readline())
            identity = procfs.process_identity(listener)
            assert identity is not None

            def no_sleep(seconds: float) -> None:
                return None

            with (
                mock.patch("time.sleep", no_sleep),
                mock.patch.object(orphans, "signal_if_same", recorded),
            ):
                killed = orphans.kill_orphan_listener(
                    "unit",
                    procfs.Process(listener, identity),
                    lambda: not procfs.has_exited(listener, identity),
                    False,
                    lambda message: None,
                )
            self.assertTrue(killed)
            self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])
            self.assertTrue(wait_until(lambda: procfs.has_exited(listener, identity)))
        finally:
            # The listener ignores TERM, so a failure above must not leave it
            # running: KILL it by its identity if it is still there.
            left = procfs.process_identity(listener) if listener else None
            shell.kill()
            shell.wait()
            if left is not None:
                real_signal_if_same(listener, left, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
