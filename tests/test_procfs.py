"""Reading a process's facts off /proc.

The identity it reads is what every signal the watchdog sends is checked
against, so a misread field is a SIGKILL to the wrong process. The stat line
below was captured from a runner host; the one with a hostile comm is
hand-written, as no capture held one.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runner_liveness import procfs


class StartTicksTest(unittest.TestCase):
    def test_start_ticks_from_a_captured_listener_stat(self) -> None:
        stat = (
            "1564 (Runner.Listener) S 1454 1358 1358 0 -1 4194304 72708 547432907 19827"
            " 24561 3193 679 2213621 418565 20 0 15 0 1049 280886956032 21371"
        )
        self.assertEqual(procfs.start_ticks_from_stat(stat), "1049")

    def test_a_comm_with_spaces_and_parentheses_cannot_shift_the_field(self) -> None:
        stat = "77 (a) b (c) S 1 77 77 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 4242 0 0"
        self.assertEqual(procfs.start_ticks_from_stat(stat), "4242")


class ParentPidTest(unittest.TestCase):
    def test_a_status_without_a_parent_line_has_no_parent(self) -> None:
        # Read as anything else, a pid would walk to an ancestor it does not have.
        self.assertIsNone(procfs.parent_pid_from_status("Name:\tx\nState:\tZ\n"))
        self.assertEqual(procfs.parent_pid_from_status("Name:\tx\nPPid:\t42\n"), 42)


class ListenerLaneTest(unittest.TestCase):
    def test_a_listener_serves_the_lane_its_binary_lives_in(self) -> None:
        argv0 = "/home/runner/actions-runner-3/bin/Runner.Listener"
        self.assertTrue(procfs.runs_listener(argv0))
        self.assertEqual(
            procfs.lane_of_listener(argv0),
            Path("/home/runner/actions-runner-3"),
        )

    def test_a_listener_off_the_lane_layout_has_no_lane(self) -> None:
        # Such a listener is reported as unplaced, which stops the starvation
        # check: the lane it serves might be an idle sibling.
        for argv0 in ("./bin/Runner.Listener", "/opt/Runner.Listener"):
            self.assertTrue(procfs.runs_listener(argv0), argv0)
            self.assertIsNone(procfs.lane_of_listener(argv0), argv0)

    def test_a_command_that_only_names_the_listener_is_not_one(self) -> None:
        # An operator's `pgrep -f Runner.Listener` would otherwise read as an
        # unplaced listener and switch the check off while it ran.
        self.assertFalse(procfs.runs_listener("/usr/bin/pgrep"))


@unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc")
class LiveProcessTest(unittest.TestCase):
    def test_a_child_descends_from_its_parent_and_not_the_reverse(self) -> None:
        # The orphan check rests on this: a listener that does not descend from
        # its unit's main process is the one it kills.
        child = subprocess.Popen(["sleep", "30"])
        try:
            parent = procfs.parent_pid(child.pid)
            assert parent is not None
            self.assertTrue(procfs.descends_from(child.pid, parent))
            self.assertFalse(procfs.descends_from(parent, child.pid))
            self.assertIn(child.pid, procfs.children_matching(parent, "sleep 30"))
        finally:
            child.kill()
            child.wait()


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(Path("/proc/self/cgroup").exists(), "needs /proc")
class UnitListenersTest(unittest.TestCase):
    """The listeners the orphan check may signal, found in this test's own
    cgroup, where both children below also sit."""

    def test_only_the_listener_binary_is_found_with_the_identity_it_had(
        self,
    ) -> None:
        # A command that only names the listener is not one: found as a
        # candidate, it would be killed with its parent in the orphan's place.
        cgroup = Path("/proc/self/cgroup").read_text().rstrip("\n")
        self.assertTrue(cgroup.startswith("0::"), "needs the unified hierarchy")
        cgroup = cgroup.removeprefix("0::")
        with tempfile.TemporaryDirectory() as scratch:
            listener_binary = Path(scratch, "bin", "Runner.Listener")
            listener_binary.parent.mkdir()
            # The interpreter, which runs under any name: a multi-call sleep
            # refuses one that is not its own.
            listener_binary.symlink_to(sys.executable)
            listener = subprocess.Popen(
                [str(listener_binary), "-c", "import time; time.sleep(30)"]
            )
            mention = subprocess.Popen(
                ["sh", "-c", "sleep 30; : Runner.Listener"],
            )
            try:
                found = procfs.unit_listeners(cgroup)
                pids = [process.pid for process in found]
                self.assertIn(listener.pid, pids)
                self.assertNotIn(mention.pid, pids)
                process = next(p for p in found if p.pid == listener.pid)
                self.assertTrue(procfs.is_unit_listener(process, cgroup))
                stale = procfs.Process(listener.pid, "an identity from before reuse")
                self.assertFalse(procfs.is_unit_listener(stale, cgroup))
            finally:
                for child in (listener, mention):
                    child.kill()
                    child.wait()
