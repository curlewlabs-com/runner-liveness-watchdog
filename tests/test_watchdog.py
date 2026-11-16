"""What the watchdog reads, and what it runs as.

A false restart can cost the job GitHub is handing a listener at that moment, so
the cases that must NOT restart matter as much as the ones that must. The `ss`
capture is real, with its addresses replaced and its non-runner process names
neutralized, because a fixture written from a model of a format this does not
own could not establish that its shape is what arrives. The starvation measure,
the /proc reads, and the orphan signalling have suites of their own.

What stays live-host-only is everything the watchdog reads off systemd and
/proc about units it does not own - unit state, cgroups, listener supervision,
the restart itself. The README's dry-run recipe exercises that on a real host.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner_liveness import procfs, watchdog

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SRC = Path(__file__).resolve().parents[1] / "src"


class SocketSilenceTest(unittest.TestCase):
    silence = watchdog.parse_socket_silence(
        (FIXTURES / "ss-established.txt").read_text()
    )

    def test_a_listener_is_judged_by_its_most_recently_active_socket(self) -> None:
        # The listener's most stale socket is routinely a leaked one on a
        # healthy listener; taking it would call healthy listeners wedged.
        self.assertEqual(self.silence[1591], 22179)

    def test_only_the_first_owner_of_a_shared_socket_is_charged(self) -> None:
        self.assertIn(2797217, self.silence)
        self.assertNotIn(2797106, self.silence)

    def test_a_socket_ss_printed_no_lastsnd_for_reads_as_just_sent(self) -> None:
        # ss leaves lastsnd out when it is zero. Skipped, the fresh socket
        # would leave this listener judged on its stale one, and restarted.
        raw = (
            '0 0 192.0.2.10:1 198.51.100.1:443 users:(("Runner.Listener",pid=7,fd=1))\n'
            "\t cubic wscale:10,10 rto:204 bytes_sent:12\n"
            '0 0 192.0.2.10:2 198.51.100.1:443 users:(("Runner.Listener",pid=7,fd=2))\n'
            "\t cubic wscale:10,10 rto:204 lastsnd:99999999\n"
        )
        self.assertEqual(watchdog.parse_socket_silence(raw), {7: 0})

    def test_a_captured_socket_with_no_lastsnd_reads_as_just_sent(self) -> None:
        # A real host's ss output, sampled until a socket had sent within the
        # millisecond: its detail line carries no lastsnd at all. That socket
        # is the only one its process holds, which the old reading dropped.
        raw = (FIXTURES / "ss-just-sent.txt").read_text()
        detail = [line for line in raw.splitlines() if line[:1] in (" ", "\t")]
        self.assertTrue(any("lastsnd:" not in line for line in detail))
        silence = watchdog.parse_socket_silence(raw)
        self.assertEqual(silence[40527], 0)
        # Replayed at the default limit, no listener in the capture is wedged.
        with mock.patch.dict(os.environ, {}, clear=True):
            limit = watchdog.load_config().silence_limit_ms
        listeners = {
            int(pid) for pid in re.findall(r'"Runner\.Listener",pid=([0-9]+)', raw)
        }
        self.assertTrue(listeners)
        self.assertTrue(all(silence.get(pid, 0) < limit for pid in listeners))

    def test_a_detail_line_with_no_socket_line_before_it_is_ignored(self) -> None:
        self.assertEqual(watchdog.parse_socket_silence("\t cubic lastsnd:5\n"), {})


class PreviousPassUptimeTest(unittest.TestCase):
    def test_no_stamp_measures_from_boot_without_comment(self) -> None:
        self.assertEqual(watchdog.previous_pass_uptime(None, 100), (0, None))
        self.assertEqual(watchdog.previous_pass_uptime("\n", 100), (0, None))

    def test_a_stamp_is_read(self) -> None:
        self.assertEqual(watchdog.previous_pass_uptime("50\n", 100), (50, None))

    def test_an_unusable_stamp_measures_from_boot_and_says_so(self) -> None:
        for stamp in ("abc", "5 0", "-3", "\u0662"):
            uptime, reason = watchdog.previous_pass_uptime(stamp, 100)
            self.assertEqual(uptime, 0, stamp)
            self.assertIsNotNone(reason, stamp)

    def test_a_stamp_from_a_previous_boot_measures_from_boot(self) -> None:
        # Past the current uptime means the stamp outlived a reboot; trusting
        # it would make every silence predate the previous pass.
        uptime, reason = watchdog.previous_pass_uptime("500", 100)
        self.assertEqual(uptime, 0)
        self.assertIsNotNone(reason)


class LastRestartTest(unittest.TestCase):
    """The cooldown is what keeps a unit from being restarted every tick, so a
    stamp it cannot read must never read as no recent restart."""

    def test_only_an_absent_stamp_means_no_recent_restart(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            self.assertEqual(watchdog.last_restart(Path(scratch, "unit")), (None, None))

    def test_a_stamp_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            stamp = Path(scratch, "unit")
            stamp.write_text("1791335829\n")
            self.assertEqual(watchdog.last_restart(stamp), (1791335829, None))

    def test_a_stamp_that_does_not_parse_is_dated_by_its_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            stamp = Path(scratch, "unit")
            stamp.write_text("garbled")
            os.utime(stamp, (1791335829, 1791335829))
            when, problem = watchdog.last_restart(stamp)
        self.assertEqual(when, 1791335829)
        self.assertIsNotNone(problem)

    def test_a_stamp_that_cannot_be_read_holds_the_restart(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            stamp = Path(scratch, "unit")
            stamp.mkdir()
            when, problem = watchdog.last_restart(stamp)
        self.assertIsNone(when)
        self.assertIsNotNone(problem)


class RestartGuardTest(unittest.TestCase):
    """The Worker set is the last thing read before a restart, and a live one
    stops it: the restart that interrupts a job is the failure this exists to
    prevent. Driven with systemctl recorded rather than run."""

    def restart(
        self, workers: list[int] | None, *, failed: bool = False
    ) -> tuple[object, list[str], bool]:
        calls: list[str] = []

        def command(argv: list[str]) -> tuple[int, str]:
            calls.append(" ".join(argv))
            return 0, ""

        def live(unit: str, cgroup: str) -> list[int] | None:
            calls.append("read workers")
            return workers

        real_last_restart = watchdog.last_restart

        def last(stamp: Path) -> tuple[int | None, str | None]:
            calls.append("read stamp")
            return real_last_restart(stamp)

        with (
            tempfile.TemporaryDirectory() as scratch,
            mock.patch.object(watchdog, "STATE_DIR", Path(scratch)),
            mock.patch.object(watchdog, "_command_output", command),
            mock.patch.object(watchdog, "live_workers", live),
            mock.patch.object(watchdog, "last_restart", last),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            outcome = watchdog.restart_unit(
                "u.service", "test", "/u", watchdog.load_config(), failed=failed
            )
            stamped = Path(scratch, "u.service").exists()
        return outcome, calls, stamped

    def test_an_idle_unit_is_restarted_after_its_workers_are_read(self) -> None:
        outcome, calls, stamped = self.restart([])
        self.assertIs(outcome, watchdog.RestartOutcome.STARTED)
        self.assertEqual(
            calls,
            [
                "read stamp",
                "read workers",
                "systemctl reset-failed u.service",
                "systemctl restart u.service",
            ],
        )
        self.assertTrue(stamped)

    def test_a_failed_unit_with_a_live_worker_stays_down_and_unstamped(self) -> None:
        # A job that outlived the unit's main process must not get a new
        # listener beside it, and the runner is still down, so the pass says so.
        for workers in ([4242], None):
            outcome, calls, stamped = self.restart(workers, failed=True)
            self.assertIs(outcome, watchdog.RestartOutcome.HELD, workers)
            self.assertEqual(calls, ["read stamp", "read workers"], workers)
            self.assertFalse(stamped, workers)

    def test_a_live_worker_stops_the_restart_and_spends_no_cooldown(self) -> None:
        for workers in ([4242], None):
            outcome, calls, stamped = self.restart(workers)
            self.assertIs(outcome, watchdog.RestartOutcome.DECLINED, workers)
            self.assertEqual(calls, ["read stamp", "read workers"], workers)
            self.assertFalse(stamped, workers)

    def test_a_failed_controlgroup_query_holds_a_failed_unit(self) -> None:
        # An empty value from a failed query is not "no cgroup left"; read as
        # that, the Worker guard reads nothing and a job that outlived the
        # unit's main process gets a new listener beside it.
        def command(argv: list[str]) -> tuple[int, str]:
            if argv[:2] == ["systemctl", "is-active"]:
                return 3, "failed\n"
            if "ControlGroup" in argv:
                return 1, ""
            raise AssertionError(f"unexpected {argv}")

        tally = watchdog.Tally()
        with (
            mock.patch.object(watchdog, "_command_output", command),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            watchdog.judge_unit("u.service", {}, 0, watchdog.load_config(), tally)
        self.assertEqual((tally.held, tally.acted), (1, 0))


@unittest.skipUnless(Path("/proc/self/cgroup").exists(), "needs /proc")
class OrphanRevalidationTest(unittest.TestCase):
    """The re-check before every orphan signal, against real processes in this
    test's own cgroup: a listener binary, and a Worker child it may start."""

    def setUp(self) -> None:
        membership = Path("/proc/self/cgroup").read_text().rstrip("\n")
        self.assertTrue(membership.startswith("0::"), "needs the unified hierarchy")
        self.cgroup = membership.removeprefix("0::")
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        bin_dir = Path(self.scratch.name, "bin")
        bin_dir.mkdir()
        # The interpreter, which runs under any name.
        for name in ("Runner.Listener", "Runner.Worker"):
            (bin_dir / name).symlink_to(sys.executable)
        self.bin = bin_dir

    def listener(self, with_worker: bool) -> procfs.Process:
        code = (
            "import subprocess, sys, time\n"
            "if len(sys.argv) > 1:\n"
            "    subprocess.Popen([sys.argv[1], '-c', 'import time; time.sleep(30)'])\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        argv = [str(self.bin / "Runner.Listener"), "-c", code]
        if with_worker:
            argv.append(str(self.bin / "Runner.Worker"))
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, child)
        assert child.stdout is not None
        self.assertEqual(child.stdout.readline(), "ready\n")
        identity = procfs.process_identity(child.pid)
        assert identity is not None
        return procfs.Process(child.pid, identity)

    def stop(self, child: subprocess.Popen[str]) -> None:
        for pid in procfs.children_matching(child.pid, "Runner.Worker"):
            os.kill(pid, 9)
        child.kill()
        child.wait()

    def still_orphan(self, listener: procfs.Process) -> bool:
        # MainPID 1: nothing descends from it, so only the rest is judged.
        with mock.patch.object(watchdog, "unit_main_pid", lambda unit: (1, "1")):
            return watchdog.is_still_orphan("u.service", listener, self.cgroup, 1)

    def test_an_idle_orphan_is_still_one(self) -> None:
        self.assertTrue(self.still_orphan(self.listener(with_worker=False)))

    def test_an_orphan_that_took_a_job_is_not_signalled(self) -> None:
        # Killing a busy orphan kills its job.
        self.assertFalse(self.still_orphan(self.listener(with_worker=True)))

    def test_an_unreadable_child_scan_is_read_as_mid_job(self) -> None:
        listener = self.listener(with_worker=False)
        with mock.patch.object(
            procfs, "children_matching", side_effect=OSError("cannot list /proc")
        ):
            self.assertFalse(self.still_orphan(listener))


class LiveWorkersTest(unittest.TestCase):
    def test_a_cgroup_systemd_already_removed_holds_no_worker(self) -> None:
        # A failed unit whose processes are all gone has no cgroup left; read
        # as unreadable instead, it could never be restarted.
        self.assertEqual(
            watchdog.live_workers("unit", "/system.slice/no-such-unit.service"), []
        )


class ConfigTest(unittest.TestCase):
    def test_a_limit_that_does_not_parse_is_refused(self) -> None:
        # Read as anything, it would switch its check off without a word.
        for name in (
            "RUNNER_LIVENESS_SILENCE_LIMIT_MS",
            "RUNNER_LIVENESS_STARVED_LIMIT_SECONDS",
        ):
            with (
                mock.patch.dict(os.environ, {name: "20m"}, clear=True),
                self.assertRaises(watchdog.ConfigError, msg=name),
            ):
                watchdog.load_config()

    def test_dry_run_is_any_value_but_zero(self) -> None:
        for value, expected in (("1", True), ("yes", True), ("0", False), ("", False)):
            with mock.patch.dict(
                os.environ, {"RUNNER_LIVENESS_DRY_RUN": value}, clear=True
            ):
                self.assertEqual(watchdog.load_config().dry_run, expected, value)

    def test_no_labels_file_leaves_the_starvation_check_off(self) -> None:
        # The default: a host that keeps no labels file runs the other checks.
        with mock.patch.dict(os.environ, {}, clear=True):
            config = watchdog.load_config()
        self.assertIsNone(config.labels_file)
        self.assertIsNone(watchdog.starvation_reason("unit", [1], config))

    def test_a_labels_file_that_is_a_path_is_refused(self) -> None:
        # An absolute path would give every lane the same labels, making every
        # lane on the host a sibling of every other.
        for value in ("/etc/runner-labels", "../labels", "..", "."):
            with (
                mock.patch.dict(
                    os.environ, {"RUNNER_LIVENESS_LABELS_FILE": value}, clear=True
                ),
                self.assertRaises(watchdog.ConfigError, msg=value),
            ):
                watchdog.load_config()


class StandaloneCopyTest(unittest.TestCase):
    def test_the_package_imports_with_only_the_stdlib_beside_it(self) -> None:
        # The README installs a copy of the package directory and nothing else.
        # An import of anything outside it passes here and fails on the host at
        # its next timer pass.
        with tempfile.TemporaryDirectory() as scratch:
            shutil.copytree(SRC / "runner_liveness", Path(scratch, "runner_liveness"))
            imported = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    (
                        f"import sys; sys.path.insert(0, {scratch!r});"
                        " import runner_liveness.watchdog"
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(imported.returncode, 0, imported.stderr)


if __name__ == "__main__":
    unittest.main()
