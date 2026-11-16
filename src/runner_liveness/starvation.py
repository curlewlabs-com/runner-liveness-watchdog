"""Whether an idle runner lane is starved, read off the lanes' own job history.

A starved lane looks healthy to every other check the watchdog runs - its
listener keeps long-polling the broker, GitHub shows it `online` and not busy -
yet GitHub assigns it nothing while its same-label siblings on the
host take every job. This module measures that from the lanes' own records and
answers in typed outcomes; it logs and acts on nothing, which is the watchdog's
part.

The measure is time the lane has spent idle while EVERY sibling - every other
lane on the host whose labels file records the same label set - was running a
job at once. In that state the lane is the host's only free runner for those
labels, so the next job for them either lands on it or leaves the host, and a
job landing on it ends the idle. Counting sibling jobs instead does not discriminate:
GitHub's assignment is skewed enough that a healthy lane can idle through a long
stream of sibling jobs for as long as one sibling is free at any moment.

The job history is each lane's own `_diag/Worker_*.log`: one file per job, named
for the worker's start in UTC and last written as it runs and exits. A busy
worker that has not written for a while reads as finished, which can only shrink
the busy time, so a gap in the record errs toward leaving the lane alone. A
worker log whose name does not parse, or that cannot be stat'ed, makes the
history unreadable rather than one job shorter, for the opposite reason: missing
the judged lane's own latest job would start its idle count early. Siblings fail
closed the same way: a serving lane whose labels file cannot be read may be an
idle sibling, and leaving it out would count all-busy time it was not part of,
so it makes the judgement unknown rather than shrinking the sibling set. A lane
no live listener serves is no sibling at all, since it can take no job.

Judging a lane against its siblings leaves blind spots, each accepted. Starved
lanes in one group each see another idle, so a group with more than one starved
at once is not called. And siblings all running long jobs with nothing queued
for the label read as starvation for their idle lane; that costs one restart of
an idle lane per cooldown.

The measure assumes the host does not sleep while a job runs. A nap inside a
sibling's job would count as busy time, since a job spans its worker log's name
to its last write; a nap while every lane is idle adds idle time that cannot
count, so a host that sleeps only when idle needs no correction.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# A job's worker log, named for the worker's start in UTC.
WORKER_LOG = re.compile(r"Worker_([0-9]{8}-[0-9]{6})-utc\.log")


@dataclass(frozen=True)
class Job:
    """One job a lane ran, as its worker log records it."""

    started: float
    last_written: float


@dataclass(frozen=True)
class Unreadable:
    """A lane history that cannot be trusted, and why."""

    reason: str


@dataclass(frozen=True)
class Starvation:
    idle_seconds: float
    all_busy_seconds: float


def worker_log_start(name: str) -> float | None:
    """The epoch second a worker log's name records, or None for any other file."""
    match = WORKER_LOG.fullmatch(name)
    if match is None:
        return None
    try:
        named = datetime.strptime(f"{match.group(1)}+0000", "%Y%m%d-%H%M%S%z")
    except ValueError:
        return None
    return named.timestamp()


def lane_jobs(lane: Path) -> list[Job] | Unreadable:
    """Every job in the lane's `_diag`."""
    jobs: list[Job] = []
    diag = lane / "_diag"
    try:
        with os.scandir(diag) as entries:
            for entry in entries:
                started = worker_log_start(entry.name)
                if started is None:
                    if entry.name.startswith("Worker_"):
                        return Unreadable(
                            f"unrecognized worker log {diag / entry.name}"
                        )
                    continue
                try:
                    last_written = entry.stat().st_mtime
                except OSError as error:
                    return Unreadable(f"cannot stat {diag / entry.name}: {error}")
                jobs.append(Job(started=started, last_written=last_written))
    except OSError as error:
        return Unreadable(f"cannot read {diag}: {error}")
    return jobs


def recorded_labels(lane: Path, labels_file: str) -> frozenset[str] | None:
    """The label set the lane's labels file records, or None without one.

    The runner keeps no local copy of the labels it registered with, so the file
    is the operator's: comma-separated, as `config.sh --labels` takes them. A
    set, because GitHub attaches no meaning to label order.
    """
    try:
        record = (lane / labels_file).read_text(errors="replace")
    except OSError:
        return None
    labels = frozenset(label for label in record.strip().split(",") if label)
    return labels or None


def same_label_siblings(
    lane: Path, labels: frozenset[str], candidates: Iterable[Path], labels_file: str
) -> list[Path] | Unreadable:
    """The candidates other than `lane` whose recorded label set is `labels`.

    Unreadable when any other candidate carries no readable labels file: it may
    be a sibling, and an idle one would end every all-busy stretch.
    """
    siblings: list[Path] = []
    for other in sorted(candidates):
        if other == lane:
            continue
        recorded = recorded_labels(other, labels_file)
        if recorded is None:
            return Unreadable(
                f"no {labels_file} in {other}, so whether it is a sibling is unknown"
            )
        if recorded == labels:
            siblings.append(other)
    return siblings


def busy_spans(
    jobs: Sequence[Job], start: float, end: float
) -> list[tuple[float, float]]:
    """The disjoint stretches of [start, end] during which any of `jobs` ran."""
    spans: list[tuple[float, float]] = []
    for job in sorted(jobs, key=lambda job: job.started):
        low, high = max(job.started, start), min(job.last_written, end)
        if low >= high:
            continue
        if spans and low <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], high))
        else:
            spans.append((low, high))
    return spans


def _overlap(
    first: Sequence[tuple[float, float]], second: Sequence[tuple[float, float]]
) -> list[tuple[float, float]]:
    """The stretches two disjoint, sorted span lists cover in common."""
    overlap: list[tuple[float, float]] = []
    i = j = 0
    while i < len(first) and j < len(second):
        low = max(first[i][0], second[j][0])
        high = min(first[i][1], second[j][1])
        if low < high:
            overlap.append((low, high))
        if first[i][1] < second[j][1]:
            i += 1
        else:
            j += 1
    return overlap


def all_busy_seconds(
    sibling_jobs: Sequence[Sequence[Job]], start: float, end: float
) -> float:
    """How long within [start, end] every sibling was running a job at once."""
    if not sibling_jobs:
        return 0.0
    covered = busy_spans(sibling_jobs[0], start, end)
    for jobs in sibling_jobs[1:]:
        covered = _overlap(covered, busy_spans(jobs, start, end))
    return sum(high - low for low, high in covered)


def idle_since(listener_started: float, own_jobs: Sequence[Job]) -> float:
    """When the lane last became free: its listener's start or its last job's end.

    A restart starts the count over, so a lane that was restarted is judged only
    on what it has done since.
    """
    return max([listener_started, *(job.last_written for job in own_jobs)])


def judge(
    listener_started: float,
    own_jobs: Sequence[Job],
    sibling_jobs: Sequence[Sequence[Job]],
    now: float,
    limit_seconds: int,
) -> Starvation | None:
    """How long an idle lane has idled and its siblings were all busy, when that
    makes it starved; None when it does not.

    A lane whose siblings were never all busy at once is not starved whatever
    the limit, so a limit of 0 cannot restart every idle lane on the host.
    """
    since = idle_since(listener_started, own_jobs)
    busy = all_busy_seconds(sibling_jobs, since, now)
    if busy <= 0 or busy < limit_seconds:
        return None
    return Starvation(idle_seconds=now - since, all_busy_seconds=busy)
