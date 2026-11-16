"""Which idle lanes the starvation measure calls starved.

A false call costs a restart of a lane that was about to take work, so the cases
that must NOT be called matter as much as the ones that must. The replays run
`_diag` job history captured from real hosts, with the lanes renamed, rather
than composed here: the signal is a property of how GitHub actually spreads
jobs over a host's lanes, which a fixture written from a model of that could
not establish.
They judge at the watchdog's default limit, so they also hold that default
between what a healthy lane reaches and what the observed starved lane did.
Hand-written inputs are the edge cases no capture happened to contain.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from collections import defaultdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from unittest import mock

from runner_liveness import starvation, watchdog

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LABELS = ".runner-labels"


def utc(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


def captured_jobs(name: str) -> dict[str, list[starvation.Job]]:
    """Each lane's jobs from a capture of `<lane>\\t<worker log>\\t<mtime>` rows."""
    jobs: dict[str, list[starvation.Job]] = defaultdict(list)
    for line in (FIXTURES / name).read_text().splitlines():
        lane, log_name, mtime = line.split("\t")
        started = starvation.worker_log_start(log_name)
        assert started is not None, log_name
        jobs[lane].append(starvation.Job(started=started, last_written=float(mtime)))
    return jobs


def default_starved_limit() -> int:
    with mock.patch.dict(os.environ, {}, clear=True):
        return watchdog.load_config().starved_limit_seconds


def judged_at(
    jobs: dict[str, list[starvation.Job]],
    lane: str,
    listener_started: float,
    now: float,
    limit: int,
) -> starvation.Starvation | None:
    """The verdict on `lane` from the history its `_diag` held at `now`.

    Jobs the capture records starting later are not yet on disk then; a sibling
    job still running is clipped to `now` by the measure itself.
    """
    own = [job for job in jobs[lane] if job.started <= now]
    siblings = [other for name, other in sorted(jobs.items()) if name != lane]
    return starvation.judge(listener_started, own, siblings, now, limit)


class StarvedLaneReplayTest(unittest.TestCase):
    """Three same-label lanes, the morning `actions-runner-3` ran one job and then
    nothing for half an hour while its siblings took the rest, until a human
    restarted it.

    Its listener started at 01:40:38, the instant its own Runner log is named
    for, and the manual restart landed at 02:20:37."""

    jobs = captured_jobs("diag-starved.tsv")
    lane = "actions-runner-3"
    listener_started = utc("2026-10-06T01:40:38")
    restarted_by_hand = utc("2026-10-06T02:20:37")

    def test_the_idle_window_starts_when_its_one_job_ended(self) -> None:
        found = judged_at(
            self.jobs,
            self.lane,
            self.listener_started,
            self.restarted_by_hand,
            default_starved_limit(),
        )
        assert found is not None
        became_free = self.restarted_by_hand - found.idle_seconds
        self.assertEqual(
            datetime.fromtimestamp(became_free, UTC).replace(microsecond=0),
            datetime(2026, 10, 6, 1, 49, 0, tzinfo=UTC),
        )

    def test_the_default_limit_calls_it_before_the_human_did(self) -> None:
        # The check exists for this lane: a limit raised past what this window
        # accumulated would leave the observed failure to a human again.
        limit = default_starved_limit()
        tick = self.listener_started
        while tick < self.restarted_by_hand:
            if judged_at(self.jobs, self.lane, self.listener_started, tick, limit):
                break
            tick += 60
        self.assertLess(tick, self.restarted_by_hand)

    def test_its_healthy_siblings_are_not_restarted_with_it(self) -> None:
        # Each sibling counts the starved lane among its own siblings, and that
        # lane is idle throughout, so the restart lands on the one lane that
        # needs it rather than on every idle moment of the busy ones.
        limit = default_starved_limit()
        for sibling in ("actions-runner-1", "actions-runner-2"):
            for job in self.jobs[sibling]:
                now = job.started - 1
                self.assertIsNone(
                    judged_at(self.jobs, sibling, self.listener_started, now, limit),
                    (sibling, job),
                )


class HealthyLaneReplayTest(unittest.TestCase):
    """Eight same-label lanes, where `actions-runner-4` sat idle for close to an
    hour while its siblings took a steady stream of jobs, then took one itself
    on the same listener session: healthy, only out of GitHub's favor.

    It is why the rule is not "siblings finished several jobs while it idled",
    nor "it idled past a limit": each fires here, on a lane that was fine. Its
    listener started at 23:49:33, the instant its Runner log is named for,
    and served the whole capture."""

    jobs = captured_jobs("diag-healthy-long-idle.tsv")
    lane = "actions-runner-4"
    listener_started = utc("2026-10-06T23:49:33")

    def idle_window(self) -> tuple[float, float]:
        own = sorted(self.jobs[self.lane], key=lambda job: job.started)
        gaps = [(a.last_written, b.started) for a, b in pairwise(own)]
        return max(gaps, key=lambda gap: gap[1] - gap[0])

    def test_it_is_not_starved(self) -> None:
        start, end = self.idle_window()
        self.assertGreater(end - start, default_starved_limit())
        self.assertIsNone(
            judged_at(
                self.jobs,
                self.lane,
                self.listener_started,
                end - 1,
                default_starved_limit(),
            )
        )


class BusiestHealthyLaneReplayTest(unittest.TestCase):
    """Five same-label lanes, where `actions-runner-5` idled for a few minutes
    while every sibling was running a job for nearly all of it, then took the
    next job itself: the most all-busy time any healthy lane accumulated across
    the captures the default limit was chosen from. Its
    listener started at 21:41:14, the instant its Runner log is named for."""

    jobs = captured_jobs("diag-healthy-busiest.tsv")
    lane = "actions-runner-5"
    listener_started = utc("2026-10-06T21:41:14")
    next_job = utc("2026-10-07T00:20:17")

    def test_the_default_limit_leaves_it_alone(self) -> None:
        # The floor under the limit: lowered past what a healthy lane reaches,
        # the check restarts lanes that were about to take work. The low-limit
        # verdict is what shows this lane's siblings really were all busy.
        before = self.next_job - 1
        self.assertIsNotNone(
            judged_at(self.jobs, self.lane, self.listener_started, before, 1)
        )
        self.assertIsNone(
            judged_at(
                self.jobs,
                self.lane,
                self.listener_started,
                before,
                default_starved_limit(),
            )
        )


class StarvationTest(unittest.TestCase):
    def test_siblings_never_all_busy_is_not_starvation_at_any_limit(self) -> None:
        # A limit of 0 would otherwise restart every idle lane on the host,
        # every pass, whether or not anything was wrong.
        own = [starvation.Job(started=0, last_written=10)]
        busy_apart = [
            [starvation.Job(started=10, last_written=50)],
            [starvation.Job(started=50, last_written=90)],
        ]
        self.assertIsNone(starvation.judge(0, own, busy_apart, 100, 0))

    def test_all_busy_time_at_the_limit_is_starvation(self) -> None:
        own = [starvation.Job(started=0, last_written=10)]
        busy_together = [
            [starvation.Job(started=10, last_written=90)],
            [starvation.Job(started=20, last_written=100)],
        ]
        found = starvation.judge(0, own, busy_together, 100, 70)
        self.assertEqual(
            found, starvation.Starvation(idle_seconds=90, all_busy_seconds=70)
        )
        self.assertIsNone(starvation.judge(0, own, busy_together, 100, 71))


class AllBusySecondsTest(unittest.TestCase):
    def test_no_siblings_is_never_all_busy(self) -> None:
        self.assertEqual(starvation.all_busy_seconds([], 0, 100), 0)

    def test_only_the_stretch_every_sibling_shares_counts(self) -> None:
        first = [starvation.Job(started=0, last_written=60)]
        second = [starvation.Job(started=40, last_written=100)]
        self.assertEqual(starvation.all_busy_seconds([first, second], 0, 100), 20)

    def test_a_job_straddling_the_window_counts_only_inside_it(self) -> None:
        jobs = [starvation.Job(started=-500, last_written=30)]
        self.assertEqual(starvation.all_busy_seconds([jobs], 0, 100), 30)

    def test_overlapping_jobs_on_one_lane_are_not_counted_twice(self) -> None:
        # A lane's worker logs overlap when its stale worker is still writing as
        # the next job starts; double-counting would inflate the busy time.
        jobs = [
            starvation.Job(started=0, last_written=50),
            starvation.Job(started=30, last_written=80),
        ]
        self.assertEqual(starvation.all_busy_seconds([jobs], 0, 100), 80)

    def test_a_log_last_written_before_its_name_counts_nothing(self) -> None:
        # A clock step between the worker's start and its last write.
        jobs = [starvation.Job(started=50, last_written=40)]
        self.assertEqual(starvation.all_busy_seconds([jobs], 0, 100), 0)


class IdleSinceTest(unittest.TestCase):
    def test_a_restart_after_the_last_job_starts_the_count_over(self) -> None:
        # Otherwise the pass after a starvation restart would read the old idle
        # window again and restart the fresh listener inside its cooldown.
        jobs = [starvation.Job(started=0, last_written=100)]
        self.assertEqual(starvation.idle_since(500, jobs), 500)

    def test_a_job_after_the_listener_started_ends_the_idle(self) -> None:
        jobs = [starvation.Job(started=600, last_written=700)]
        self.assertEqual(starvation.idle_since(500, jobs), 700)


class WorkerLogStartTest(unittest.TestCase):
    def test_the_name_is_read_as_utc_whatever_the_local_zone(self) -> None:
        # 02:30Z on the 7th is still the 6th in Los Angeles; reading the name in
        # local time would shift every job by the zone offset.
        expected = datetime(2026, 10, 7, 2, 30, 56, tzinfo=UTC).timestamp()
        original = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        try:
            started = starvation.worker_log_start("Worker_20261007-023056-utc.log")
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()
        self.assertEqual(started, expected)

    def test_other_diag_files_are_not_jobs(self) -> None:
        for name in (
            "Runner_20261006-014038-utc.log",
            "pages",
            "blocks",
            "Worker_x.log",
        ):
            self.assertIsNone(starvation.worker_log_start(name), name)


class LaneJobsTest(unittest.TestCase):
    def test_jobs_are_read_from_worker_logs_alone(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            diag = Path(scratch, "_diag")
            (diag / "pages").mkdir(parents=True)
            for name in (
                "Worker_20261006-014615-utc.log",
                "Runner_20261006-014038-utc.log",
            ):
                (diag / name).write_text("")
            jobs = starvation.lane_jobs(Path(scratch))
        assert isinstance(jobs, list)
        self.assertEqual([job.started for job in jobs], [utc("2026-10-06T01:46:15")])

    def test_a_worker_log_that_cannot_be_stat_ed_makes_the_history_unreadable(
        self,
    ) -> None:
        # Same reason as a name that does not parse: the log missed could be
        # the judged lane's own latest job. A log pruned mid-scan is the case.
        with tempfile.TemporaryDirectory() as scratch:
            diag = Path(scratch, "_diag")
            diag.mkdir()
            (diag / "Worker_20261006-014615-utc.log").symlink_to(diag / "gone")
            self.assertIsInstance(
                starvation.lane_jobs(Path(scratch)), starvation.Unreadable
            )

    def test_a_worker_log_that_does_not_parse_makes_the_history_unreadable(
        self,
    ) -> None:
        # Dropping it instead could drop the judged lane's own latest job, which
        # starts its idle count early and reads a lane that just worked as starved.
        with tempfile.TemporaryDirectory() as scratch:
            diag = Path(scratch, "_diag")
            diag.mkdir()
            (diag / "Worker_20261006-014615-utc.log").write_text("")
            (diag / "Worker_20261306-014615-utc.log").write_text("")
            self.assertIsInstance(
                starvation.lane_jobs(Path(scratch)), starvation.Unreadable
            )


class RecordedLabelsTest(unittest.TestCase):
    def test_lanes_differing_only_in_label_order_are_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            first, second = Path(scratch, "a"), Path(scratch, "b")
            for lane, record in (
                (first, "self-hosted,linux,gpu\n"),
                (second, "gpu,self-hosted,linux"),
            ):
                lane.mkdir()
                (lane / LABELS).write_text(record)
            self.assertEqual(
                starvation.recorded_labels(first, LABELS),
                starvation.recorded_labels(second, LABELS),
            )

    def test_lanes_differing_only_in_label_case_or_spacing_are_siblings(
        self,
    ) -> None:
        # GitHub matches labels case-insensitively; an idle sibling whose file
        # spells one differently would otherwise drop out of the group, and the
        # busy rest would read as all-busy around the judged lane.
        with tempfile.TemporaryDirectory() as scratch:
            first, second = Path(scratch, "a"), Path(scratch, "b")
            for lane, record in (
                (first, "self-hosted,Linux,GPU"),
                (second, " self-hosted , linux,gpu \n"),
            ):
                lane.mkdir()
                (lane / LABELS).write_text(record)
            self.assertEqual(
                starvation.recorded_labels(first, LABELS),
                starvation.recorded_labels(second, LABELS),
            )

    def test_a_lane_without_a_record_cannot_be_judged(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            self.assertIsNone(starvation.recorded_labels(Path(scratch), LABELS))

    def lanes_with_records(
        self, scratch: str, records: dict[str, str | None]
    ) -> dict[str, Path]:
        lanes: dict[str, Path] = {}
        for name, record in records.items():
            lanes[name] = Path(scratch, name)
            lanes[name].mkdir()
            if record is not None:
                (lanes[name] / LABELS).write_text(record)
        return lanes

    def test_siblings_are_the_other_lanes_with_the_same_set(self) -> None:
        # A lane that counted itself, or one routing on other labels, as a
        # sibling would read its own idle, or that lane's, as all-busy time.
        with tempfile.TemporaryDirectory() as scratch:
            lanes = self.lanes_with_records(
                scratch,
                {
                    "judged": "self-hosted,gpu",
                    "same": "gpu,self-hosted",
                    "other": "self-hosted,arm64",
                },
            )
            self.assertEqual(
                starvation.same_label_siblings(
                    lanes["judged"],
                    frozenset({"self-hosted", "gpu"}),
                    lanes.values(),
                    LABELS,
                ),
                [lanes["same"]],
            )

    def test_a_serving_lane_without_a_record_makes_the_siblings_unknown(
        self,
    ) -> None:
        # Left out instead, an idle lane that is in truth a sibling would let
        # the rest read as all busy, and a healthy lane be restarted.
        with tempfile.TemporaryDirectory() as scratch:
            lanes = self.lanes_with_records(
                scratch,
                {"judged": "gpu", "same": "gpu", "unrecorded": None},
            )
            self.assertIsInstance(
                starvation.same_label_siblings(
                    lanes["judged"], frozenset({"gpu"}), lanes.values(), LABELS
                ),
                starvation.Unreadable,
            )


if __name__ == "__main__":
    unittest.main()
