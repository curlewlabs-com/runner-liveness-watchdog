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
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner_liveness import watchdog

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
