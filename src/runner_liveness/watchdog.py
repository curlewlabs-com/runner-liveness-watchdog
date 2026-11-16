"""Restart a runner service that has stopped serving GitHub while looking healthy.

Also kills a listener that a timed-out stop left running beside the unit's own.
Run by runner-liveness.timer every few minutes, as root, on a Linux host whose
GitHub Actions runners are systemd services installed by the runner's own
`svc.sh`. It imports only the standard library, so it runs from a copy of the
package with nothing installed beside it.

The failure modes it covers, none of which a unit's Restart= can see:

 1. Wedged listener. The listener process stays alive and the unit stays
    `active (running)`, but its broker connection is dead in a way TCP cannot
    detect: the request was fully sent AND fully acknowledged, so Send-Q is
    empty, nothing is outstanding, and the kernel never arms a retransmit
    timer. To the kernel it is a healthy idle socket; the listener is blocked
    forever in a read whose response will never arrive. The runner's own
    RetryRequest only retries requests that *fail* - never one that simply
    never returns. GitHub shows the runner offline, jobs route to its
    siblings, and nothing on the host reports a problem.

    A job already dispatched when the wedge lands is failed by GitHub once its
    own lost-communication window expires, with no steps recorded and no log
    to retrieve. That job is also why the wedge is recoverable at all: the
    listener goes silent before it spawns a Worker, so the never-interrupt-a-
    running-job guard below does not see one and the unit stays eligible for
    restart.

    One way to get there is a socket whose source address dies under it, such
    as one opened over an interface that later loses its lease: TCP source
    addresses cannot migrate. Detection keys on socket silence, not on any
    cause, so it covers a wedge that arrives some other way too.

 2. Unit systemd has given up on. A runner launcher that cannot exec, or a
    crash loop past the unit's start limit, leaves no listener process at all -
    so the socket check above has nothing to measure, and only the unit state
    gives it away.

    Only `failed` counts as given-up-on. An `inactive` unit is a runner an
    operator stopped on purpose, so that a manual `systemctl stop` sticks;
    `activating`/`deactivating` are transients belonging to a restart or a
    runner self-update already in progress. Those are reported in the run
    summary, never acted on.

 3. Orphaned listener. A stop that times out is SIGKILLed only at the
    wrapper, because the runner's unit stops with KillMode=process; the
    listener underneath lives on with the runner's session, and the next start
    brings a second one up beside it. Every replacement loops on "A session
    for this runner already exists" until the first session lapses, and once
    the orphan and its replacement hold one the same job lands in each. The
    orphan is the listener in the unit's cgroup that does not descend from the
    unit's main process; it is killed once idle, its supervisor first so it
    cannot respawn it.

 4. Starved lane. Everything the checks above read looks healthy - the unit is
    `active (running)`, the listener keeps long-polling the broker with
    `status=Online` so its socket never goes quiet, and GitHub shows the runner
    `online` and not busy - yet GitHub assigns it nothing while its same-label
    siblings on the host take every job. A restart, which opens a new broker
    session, brings it back at once. Why the old session stops receiving work
    is on GitHub's side and not established, so detection keys on the outcome.
    The starvation module has the measure: time the lane has idled while every
    same-label sibling on the host was running a job at once, read off the
    lanes' own job history. It needs each lane's labels, which the runner does
    not keep locally, so it runs only when RUNNER_LIVENESS_LABELS_FILE names a
    file the operator keeps in each runner directory.

Detection of mode 1 keys on `ss` lastsnd, NOT on _diag mtime: a healthy idle
listener can leave _diag untouched for ~40 min, which is inside any threshold
worth setting. It takes the MINIMUM lastsnd across the listener's established
sockets - its most recently active connection. Taking the maximum instead
measures the listener's most *stale* socket, which is routinely a leaked
half-open connection on a perfectly healthy listener; that inversion is a known
way to conclude the whole class of wedge is a false alarm.

On a KVM guest, lastsnd was observed advancing through the hypervisor host's
whole suspend, so at resume every socket the nap interrupted reads as silent for
all of it until its listener sends again, seconds later. The timer, overdue at
resume, fires first, and a host is often woken because work has arrived for it:
a restart then can land while GitHub is handing the listener a job, which
GitHub fails once its lost-communication window expires. So only the silence
that predates the previous pass counts - the reading minus the /proc/uptime
elapsed since that pass. That relies on a nap never advancing lastsnd further
than /proc/uptime, which counts suspended time (proc_uptime(5)). A wedge's
silence keeps growing, so it is called one pass later than the limit alone
would call it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import Path

from runner_liveness import orphans, procfs
from runner_liveness import starvation as lane_starvation

# Stops a systematically-unhappy unit from being restarted every timer tick,
# which would also burn the unit's own start limit and park it `failed`.
# Wall-clock on purpose: the point is to not thrash in real time. It suppresses
# the retry, never the report - a unit waiting out its cooldown is still a
# runner that is down, and still fails the run.
RESTART_COOLDOWN_SECONDS = 3600

# tmpfs: the cooldown and previous-pass stamps are per-boot state, not history
# worth keeping.
STATE_DIR = Path("/run/runner-liveness")


class ConfigError(ValueError):
    """A configuration variable that does not hold a usable value."""


@dataclass(frozen=True)
class Config:
    silence_limit_ms: int
    starved_limit_seconds: int
    labels_file: str | None
    dry_run: bool


def _env_count(name: str, default: int) -> int:
    """A non-negative integer tuning variable, unset or empty meaning `default`.

    Refused outright rather than read as something: a limit that does not parse
    would otherwise switch its check off without a word.
    """
    raw = os.environ.get(name) or str(default)
    if not (raw.isascii() and raw.isdigit()):
        raise ConfigError(f"{name}={raw!r} is not a whole number")
    return int(raw)


def _labels_file() -> str | None:
    """The labels file's name inside each runner directory, or None when unset.

    A bare file name only: a path would resolve outside the runner directory,
    and an absolute one would give every lane the same labels, making every
    lane on the host a sibling of every other without a word.
    """
    name = os.environ.get("RUNNER_LIVENESS_LABELS_FILE") or None
    if name is not None and ("/" in name or name in (".", "..")):
        raise ConfigError(
            f"RUNNER_LIVENESS_LABELS_FILE={name!r} is not a bare file name"
        )
    return name


def load_config() -> Config:
    return Config(
        # 30 min. Healthy listeners sampled across several runner hosts topped
        # out around 67s of silence, so this sits well over an order of
        # magnitude above the worst healthy observation while cutting wedge
        # recovery from days to under an hour. Healthy silence and wedged
        # silence are far apart rather than adjacent, which is what makes the
        # exact value uncritical: a wedge's silence grows without bound,
        # because the socket it is blocked on will never carry anything again,
        # so it crosses any threshold set above the healthy ceiling and keeps
        # going. The width here buys margin, not precision. Raise it before
        # lowering: a false restart can cost the job GitHub is handing the
        # listener at that moment, and that margin is what keeps it from
        # happening at all.
        silence_limit_ms=_env_count("RUNNER_LIVENESS_SILENCE_LIMIT_MS", 1_800_000),
        # 20 min of every sibling busy while this lane idled. On a healthy
        # host that state is ended by the next job routed to the host, so it
        # accumulates only across lulls in which no job arrived while every
        # sibling was busy; a starved lane accumulates it for as long as the
        # host is loaded. The limit sits several times above the accumulation
        # measured on healthy lanes across several hosts, and no higher than
        # that margin needs, since every minute of it is a minute a loaded host
        # runs a lane short. Raise it before lowering, for the same reason as
        # the silence limit.
        starved_limit_seconds=_env_count("RUNNER_LIVENESS_STARVED_LIMIT_SECONDS", 1200),
        # Unset, the starvation check is off and the other three run alone.
        labels_file=_labels_file(),
        # Report decisions without acting. This is how the detection half gets
        # verified on a live host - drop a limit far enough that healthy lanes
        # trip it and confirm the script names them - without restarting a
        # working pool.
        dry_run=(os.environ.get("RUNNER_LIVENESS_DRY_RUN") or "0") != "0",
    )


def log(message: str) -> None:
    """One decision line, on stderr.

    stderr, not syslog: systemd captures a oneshot's stderr into the journal
    under its own unit (StandardError= inherits StandardOutput=journal), so this
    reads the same via `journalctl -u runner-liveness.service` and when an
    operator runs the script by hand over ssh.
    """
    print(f"runner-liveness: {message}", file=sys.stderr, flush=True)


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _command_output(argv: Sequence[str]) -> tuple[int, str]:
    """A command's exit status and its stdout, stderr discarded.

    A command that cannot be started reads as a failure with no output, the way
    a shell reports one it cannot find.
    """
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return 127, ""
    return completed.returncode, completed.stdout


def _command_transcript(argv: Sequence[str]) -> tuple[int, str]:
    """A command's exit status and its stdout and stderr together.

    For the probes whose failure has to be reported in the command's own words.
    """
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except OSError as error:
        return 127, str(error)
    return completed.returncode, completed.stdout


def _scan[T](what: str, scan: Callable[[], T]) -> T | None:
    """A /proc scan's answer, or None, reported, when /proc cannot be listed.

    None is never an empty answer: an empty Worker set is what clears a unit for
    restart, so a scan that did not happen must not read as one that found
    nothing.
    """
    try:
        return scan()
    except OSError as error:
        log(f"cannot list /proc {what}: {error}")
        return None


def parse_socket_silence(raw: str) -> dict[int, int]:
    """Minimum lastsnd (ms) per PID, from `ss -tnpiH state established` output.

    `ss -i` emits its detail on the line *after* each socket line, so the socket
    line's pid is carried forward onto the detail line that follows it. Detail
    lines are the indented ones; that is what separates the two. Only the first
    pid on a socket line and the first detail line after it count.
    """
    quietest: dict[int, int] = {}
    current: int | None = None
    for line in raw.split("\n"):
        if line[:1] in (" ", "\t"):
            silence = re.search(r"lastsnd:([0-9]+)", line)
            if current is not None and silence is not None:
                ms = int(silence.group(1))
                if current not in quietest or ms < quietest[current]:
                    quietest[current] = ms
            current = None
            continue
        owner = re.search(r"pid=([0-9]+)", line)
        current = int(owner.group(1)) if owner is not None else None
    return quietest


def socket_silence_by_pid() -> dict[int, int] | None:
    """Every established socket's silence on this host, from one ss pass.

    A failed probe is reported and returned as None rather than parsed: an empty
    parse would read as "every listener holds no socket" - a watchdog checking
    nothing while logging a clean pass. Fail loudly instead.
    """
    rc, raw = _command_transcript(["ss", "-tnpiH", "state", "established"])
    if rc != 0:
        log(
            f"ss failed (rc={rc}) - cannot evaluate listener liveness: {raw.rstrip(chr(10))}"
        )
        return None
    return parse_socket_silence(raw)


def previous_pass_uptime(stamp: str | None, uptime_now: int) -> tuple[int, str | None]:
    """The uptime the previous pass recorded, and why it was refused if it was.

    With no usable stamp - the first pass since boot, the state directory being
    tmpfs - the answer is 0, so the gap runs back to boot, which no socket's
    silence can predate, and that pass restarts nothing for silence.
    """
    value = (stamp or "").rstrip("\n")
    if value == "":
        return 0, None
    if not (value.isascii() and value.isdigit()):
        return 0, f"previous-pass stamp '{value}' is unusable - measuring from boot"
    if int(value) > uptime_now:
        return 0, (
            f"previous-pass stamp {value}s is past the current uptime {uptime_now}s"
            " - measuring from boot"
        )
    return int(value), None


class RestartOutcome(Enum):
    """What restart_unit did; the run summary and the exit status key on it.

    This is only ever reached for a unit that is bad right now, so REFUSED and
    HELD each mean "this runner is down and this pass did not fix it" and each
    has to reach the non-zero exit; only DECLINED is silence.

    HELD does more work than its name suggests - it is what reports a launcher
    that cannot exec, not REFUSED. Runner units are Type=simple, so a restart
    reports success the moment the fork lands and never waits for the execve: a
    truncated runsvc.sh logs "restart ok" right here, then fails 203/EXEC and
    parks `failed` again well inside one timer tick. Only the pass after that sees it, and
    sees it holding a fresh stamp. Folding HELD into DECLINED leaves a
    permanently dead runner reported healthy for the whole cooldown hour, then
    again for the next one.
    """

    # systemd accepted the start.
    STARTED = 0
    # Declined without acting, and no runner is down because of it (dry run).
    DECLINED = 1
    # systemd refused the restart outright - needs a human.
    REFUSED = 2
    # Down, but the cooldown is holding this pass's retry off.
    HELD = 3


def restart_unit(unit: str, reason: str, config: Config) -> RestartOutcome:
    if config.dry_run:
        log(f"{unit}: {reason} - WOULD restart (dry run)")
        return RestartOutcome.DECLINED

    stamp = STATE_DIR / unit
    now = int(time.time())
    recorded = _read_file(stamp)
    if recorded is not None:
        last = recorded.strip()
        if not (last.isascii() and last.isdigit()):
            log(
                f"{unit}: restart stamp '{last}' is unusable - treating it as no recent restart"
            )
        elif now - int(last) < RESTART_COOLDOWN_SECONDS:
            log(
                f"{unit}: {reason}, but restarted {now - int(last)}s ago - holding off"
                f" (cooldown {RESTART_COOLDOWN_SECONDS}s)"
            )
            return RestartOutcome.HELD

    stamp.write_text(f"{now}\n")
    log(f"{unit}: {reason} - restarting")
    # A unit parked by its start limit refuses a plain restart until its
    # failure state is cleared, so clear it first. Harmless otherwise.
    _command_output(["systemctl", "reset-failed", unit])
    if _command_output(["systemctl", "restart", unit])[0] == 0:
        log(f"{unit}: restart ok")
        return RestartOutcome.STARTED
    log(f"{unit}: restart FAILED - needs a human")
    return RestartOutcome.REFUSED


@dataclass
class Tally:
    checked: int = 0
    acted: int = 0
    parked: int = 0
    unrecovered: int = 0
    held: int = 0
    orphans: int = 0

    def record(self, outcome: RestartOutcome) -> None:
        """Fold a restart outcome into the run counters, in one place for every
        call site. DECLINED counts nowhere, which is the intent."""
        if outcome is RestartOutcome.STARTED:
            self.acted += 1
        elif outcome is RestartOutcome.REFUSED:
            self.unrecovered += 1
        elif outcome is RestartOutcome.HELD:
            self.held += 1


def lane_history(unit: str, lane: Path) -> list[lane_starvation.Job] | None:
    """A lane's jobs, or None, reported, when its history cannot be trusted."""
    history = lane_starvation.lane_jobs(lane)
    if isinstance(history, lane_starvation.Unreadable):
        log(f"{unit}: {history.reason} - skipping the starvation check")
        return None
    return history


def starvation_reason(
    unit: str, listeners: Sequence[int], config: Config
) -> str | None:
    """Why this idle unit's lane counts as starved, or None when it does not.

    With no labels file configured the check is off and says nothing. Every
    other way of not being able to judge it is reported and answers None: a
    lane that cannot be judged is left alone.
    """
    labels_file = config.labels_file
    if labels_file is None:
        return None
    lanes = {procfs.listener_lane(pid) for pid in listeners}
    lane = lanes.pop() if len(lanes) == 1 else None
    if lane is None:
        log(f"{unit}: listener lane unreadable - skipping the starvation check")
        return None
    labels = lane_starvation.recorded_labels(lane, labels_file)
    if labels is None:
        log(
            f"{unit}: no {labels_file} in {lane} - cannot judge it against its siblings"
        )
        return None
    serving = _scan("for the lanes this host serves", procfs.serving_lanes)
    if serving is None:
        return None
    if serving.unplaced:
        # An unplaced listener may serve an idle sibling, which would end every
        # all-busy stretch, so its lane being unknown makes the judgement unknown.
        pids = ", ".join(str(pid) for pid in serving.unplaced)
        log(
            f"{unit}: listener pid(s) {pids} serve a lane that cannot be read"
            " - skipping the starvation check"
        )
        return None
    siblings = lane_starvation.same_label_siblings(
        lane, labels, serving.lanes, labels_file
    )
    if isinstance(siblings, lane_starvation.Unreadable):
        log(f"{unit}: {siblings.reason} - skipping the starvation check")
        return None
    if not siblings:
        return None

    started = [procfs.process_started_at(pid) for pid in listeners]
    known_starts = [start for start in started if start is not None]
    if len(known_starts) != len(started):
        log(f"{unit}: listener start unreadable - skipping the starvation check")
        return None
    own_jobs = lane_history(unit, lane)
    if own_jobs is None:
        return None
    sibling_jobs: list[list[lane_starvation.Job]] = []
    for sibling in siblings:
        jobs = lane_history(unit, sibling)
        if jobs is None:
            return None
        sibling_jobs.append(jobs)

    found = lane_starvation.judge(
        max(known_starts),
        own_jobs,
        sibling_jobs,
        time.time(),
        config.starved_limit_seconds,
    )
    if found is None:
        return None
    names = ", ".join(sibling.name for sibling in siblings)
    return (
        f"lane idle for {int(found.idle_seconds)}s while every same-label sibling"
        f" ({names}) was running a job for {int(found.all_busy_seconds)}s of it"
        f" (limit {config.starved_limit_seconds}s)"
    )


def judge_unit(
    unit: str,
    silence: dict[int, int],
    since_previous_pass_ms: int,
    config: Config,
    tally: Tally,
) -> None:
    state = _command_output(["systemctl", "is-active", unit])[1].rstrip("\n")
    if state == "failed":
        tally.record(
            restart_unit(
                unit, "unit is 'failed' - systemd has stopped retrying it", config
            )
        )
        return
    if state != "active":
        # Deliberately stopped, or mid-transition. Neither is ours to undo.
        tally.parked += 1
        log(f"{unit}: '{state}' - leaving it alone (not a failure this should undo)")
        return

    cgroup = _command_output(
        ["systemctl", "show", "-p", "ControlGroup", "--value", unit]
    )[1]
    cgroup = cgroup.rstrip("\n")
    if not cgroup or not Path(f"/sys/fs/cgroup{cgroup}").is_dir():
        log(f"{unit}: active but no readable cgroup - skipping")
        return

    # Never interrupt a running job. A wedged listener has no Worker, so this
    # costs no detection - it only rules out restarting a busy runner.
    # Before the busy check: the orphan coexists with a worker that belongs to
    # either listener, and the state it produces is worst while jobs run.
    rc, main_pid_text = _command_output(
        ["systemctl", "show", "-p", "MainPID", "--value", unit]
    )
    main_pid_text = (main_pid_text if rc == 0 else f"{main_pid_text}0").rstrip("\n")
    main_pid = (
        int(main_pid_text) if main_pid_text.isascii() and main_pid_text.isdigit() else 0
    )
    if main_pid <= 1:
        # Without a main process to descend from, every listener would read as
        # an orphan, the unit's own included. Fail closed: no orphan judgement
        # for this unit on this tick.
        log(
            f"{unit}: MainPID unreadable ('{main_pid_text}') - skipping the orphan check"
        )
    else:
        candidates = _scan(
            f"looking for Runner.Listener in {cgroup}",
            lambda: procfs.unit_pids(cgroup, "Runner.Listener"),
        )
        for pid in candidates or []:
            if procfs.descends_from(pid, main_pid):
                continue
            # An unreadable child list is read as mid-job: killing a busy orphan
            # kills its job.
            children = _scan(
                f"looking for the children of {pid}",
                partial(procfs.children_matching, pid, "Runner.Worker"),
            )
            if children != []:
                log(
                    f"{unit}: orphaned listener pid {pid} is mid-job - leaving it for the next tick"
                )
                continue
            if orphans.kill_orphan_listener(unit, pid, config.dry_run, log):
                tally.orphans += 1

    # A failed lookup has logged itself and skips the unit for this tick: a
    # unit whose Worker set is unknown is not one to judge idle.
    workers = _scan(
        f"looking for Runner.Worker in {cgroup}",
        lambda: procfs.unit_pids(cgroup, "Runner.Worker"),
    )
    if workers is None or workers:
        return

    listeners = _scan(
        f"looking for Runner.Listener in {cgroup}",
        lambda: procfs.unit_pids(cgroup, "Runner.Listener"),
    )
    if listeners is None:
        return
    if not listeners:
        log(
            f"{unit}: active with no Runner.Listener process - leaving it"
            " (systemd owns process supervision here)"
        )
        return

    # Quietest listener socket for this unit; a listener holding several
    # connections is judged by its most recently active one.
    readings = [silence[pid] for pid in listeners if pid in silence]
    if not readings:
        # Not the observed wedge signature (which holds exactly one socket), and
        # a listener mid-reconnect looks identical, so this only gets recorded.
        log(
            f"{unit}: listener holds no established socket - not restarting, recording for follow-up"
        )
        return
    quietest = min(readings)

    limit_ms = config.silence_limit_ms
    if quietest > limit_ms:
        # Past the limit, mode 1 owns the unit for this pass, whichever way it
        # decides: a silence it declines to call (the resume case in the
        # header) is not one for mode 4 to second-guess.
        before_previous_pass_ms = quietest - since_previous_pass_ms
        if before_previous_pass_ms > limit_ms:
            tally.record(
                restart_unit(
                    unit,
                    f"listener silent for {quietest // 1000}s,"
                    f" {before_previous_pass_ms // 1000}s of it before the previous pass"
                    f" (limit {limit_ms // 1000}s)",
                    config,
                )
            )
        else:
            log(
                f"{unit}: listener silent for {quietest // 1000}s, but only"
                f" {max(before_previous_pass_ms, 0) // 1000}s of it before the previous pass"
                f" {since_previous_pass_ms // 1000}s ago (limit {limit_ms // 1000}s)"
                " - not restarting"
            )
        return

    reason = starvation_reason(unit, listeners, config)
    if reason is not None:
        tally.record(restart_unit(unit, reason, config))


def main() -> int:
    if os.geteuid() != 0:
        print(
            "must run as root (systemctl restart, and ss -p needs root to map another"
            " user's sockets)",
            file=sys.stderr,
        )
        return 1

    if shutil.which("ss") is None:
        log("ss not found (iproute2) - cannot evaluate listener liveness, exiting")
        return 1

    try:
        config = load_config()
    except ConfigError as error:
        log(f"{error} - exiting")
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Same capture-then-parse reason as the ss probe: a failed enumeration must
    # not be indistinguishable from a host that legitimately has no runners,
    # which would exit 0 on the reassuring "nothing to check" line.
    #
    # The exit status alone cannot tell those apart - `list-unit-files` returns
    # 1 when the pattern matches nothing and when systemd is unreachable alike.
    # The output does: a no-match prints nothing, while a real failure prints
    # its reason ("Failed to connect to system scope bus ..."). That was
    # measured, so this discriminates on observed behavior rather than on the
    # documented status alone.
    units_rc, units_raw = _command_transcript(
        [
            "systemctl",
            "list-unit-files",
            "actions.runner.*.service",
            "--no-legend",
            "--no-pager",
        ]
    )
    units_raw = units_raw.rstrip("\n")
    if units_rc != 0 and units_raw:
        log(
            f"systemctl list-unit-files failed (rc={units_rc}) - cannot enumerate runners: {units_raw}"
        )
        return 1
    units = [line.split()[0] for line in units_raw.splitlines() if line.split()]
    if not units:
        log("no actions.runner.*.service units on this host - nothing to check")
        return 0

    silence = socket_silence_by_pid()
    if silence is None:
        return 1

    # Uptime at this pass's socket read, and how long before it the previous
    # pass read them; the header says why a silence is judged against that gap.
    # A dry run leaves the stamp alone, so it cannot change what the next real
    # pass decides.
    uptime_now = procfs.uptime_seconds()
    previous_pass_stamp = STATE_DIR / "previous-pass"
    uptime_previous, refusal = previous_pass_uptime(
        _read_file(previous_pass_stamp), uptime_now
    )
    if refusal is not None:
        log(refusal)
    since_previous_pass_ms = (uptime_now - uptime_previous) * 1000
    if not config.dry_run:
        previous_pass_stamp.write_text(f"{uptime_now}\n")

    tally = Tally()
    for unit in units:
        tally.checked += 1
        judge_unit(unit, silence, since_previous_pass_ms, config, tally)

    # Unconditional summary: a silent watchdog and an absent one look the same
    # in journalctl, so every pass leaves a heartbeat.
    log(
        f"checked {tally.checked} unit(s), restarted {tally.acted}, killed {tally.orphans}"
        f" orphaned listener(s), {tally.unrecovered} still down after a restart attempt,"
        f" {tally.held} still down inside the restart cooldown, left {tally.parked}"
        " stopped/transitional"
    )

    # Exit non-zero while any runner is still down and this pass did not bring
    # it back - whether the restart was rejected or the cooldown held the retry
    # off - so a host with a runner this script cannot fix surfaces in
    # `systemctl --failed` instead of only as prose in the journal. After the
    # summary, never before it - the heartbeat is the reason the summary is
    # unconditional. Safe against the timer: a failed oneshot does not stop its
    # own timer, and a 5-minute cadence never approaches systemd's default
    # start-limit of 5 in 10 seconds.
    return 1 if tally.unrecovered or tally.held else 0
