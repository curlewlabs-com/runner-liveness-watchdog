"""What /proc says about this host's runner processes, for the watchdog.

The watchdog module decides and acts on these answers; this module only
reads. Every answer is a fresh read at the moment of asking rather than a
snapshot, because the watchdog re-asks between a decision and the signal it
rests on: a process that exits, or a pid reused, in between is seen as what it
is now.

A scan reads every process's command line and, for the matches alone, that
process's own /proc/<pid>/cgroup. It never reads a cgroup's cgroup.procs: that
runs a kernel task iteration, and on a kernel without the upstream fix for
CVE-2026-98163 an iteration that races a job process's exit takes a reference on
a task already being freed and panics the kernel, failing every job on the host.
On a kernel carrying that fix, cgroup.procs would be safe to read. A
process's own /proc/<pid>/cgroup resolves that one task's path and iterates
nothing.

A process that exits mid-read is skipped, as if it had not been there. A /proc
that cannot be listed at all raises OSError out of every scan, so no caller
reads "no such process" off a read that never happened.

A "lane" here is one runner installation: the directory `config.sh` was run in,
whose `bin/Runner.Listener` its service starts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePath


def _read_text(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return None


def all_pids() -> list[int]:
    """Every pid on the host, ascending. Raises OSError when /proc cannot be listed."""
    return sorted(int(name) for name in os.listdir("/proc") if name.isdigit())


def command_line(pid: int) -> str | None:
    """A process's argv joined by spaces, or None once it is gone.

    A zombie's is empty, so no pattern matches it, which is what drops one that
    still names its old unit in /proc/<pid>/cgroup.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode(errors="replace")


def parent_pid_from_status(status: str) -> int | None:
    """The `PPid:` a `/proc/<pid>/status` reports, or None when it has none."""
    for line in status.splitlines():
        if line.startswith("PPid:"):
            value = line.split(":", 1)[1].strip()
            return int(value) if value.isdigit() else None
    return None


def parent_pid(pid: int) -> int | None:
    status = _read_text(f"/proc/{pid}/status")
    return None if status is None else parent_pid_from_status(status)


def start_ticks_from_stat(stat: str) -> str | None:
    """Field 22 of a `/proc/<pid>/stat` line: the start time in ticks since boot.

    Read after the ")" that ends the comm field, so a comm carrying spaces or a
    ")" of its own cannot shift it.
    """
    fields = stat.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 and fields[19].isdigit() else None


def process_identity(pid: int) -> str | None:
    """A process's identity beyond its pid: its start time in clock ticks since
    boot, which a recycled pid cannot reproduce. `ps -o lstart=` would resolve
    only to a second.
    """
    stat = _read_text(f"/proc/{pid}/stat")
    return None if stat is None else start_ticks_from_stat(stat)


def has_exited(pid: int, identity: str) -> bool:
    """Whether the process `identity` named at `pid` is gone: its pid is free or
    holds another process, or it is a zombie waiting to be reaped."""
    stat = _read_text(f"/proc/{pid}/stat")
    if stat is None or start_ticks_from_stat(stat) != identity:
        return True
    fields = stat.rpartition(")")[2].split()
    return bool(fields) and fields[0] in ("Z", "X")


def process_started_at(pid: int) -> float | None:
    """When a process started, in epoch seconds: boot time plus its start ticks."""
    ticks = process_identity(pid)
    proc_stat = _read_text("/proc/stat")
    if ticks is None or proc_stat is None:
        return None
    for line in proc_stat.splitlines():
        name, _, value = line.partition(" ")
        if name == "btime" and value.strip().isdigit():
            return int(value) + int(ticks) / os.sysconf("SC_CLK_TCK")
    return None


def uptime_seconds() -> int:
    """Whole seconds since boot, suspended time included (proc_uptime(5))."""
    return int(Path("/proc/uptime").read_text().split()[0].partition(".")[0])


def descends_from(pid: int, root: int) -> bool:
    """True when `root` is `pid` or one of its ancestors.

    Walks /proc rather than trusting the cgroup: an orphaned listener sits in
    the unit's cgroup beside the live one (systemd logs "Found left-over process
    ... Ignoring" and carries on), so the cgroup cannot tell the two apart -
    descent from the unit's main process can.
    """
    if root <= 1:
        return False
    current: int | None = pid
    while current is not None and current > 1:
        if current == root:
            return True
        current = parent_pid(current)
    return False


def children_matching(parent: int, pattern: str) -> list[int]:
    """Direct children of `parent` whose command line mentions `pattern`."""
    matches: list[int] = []
    for pid in all_pids():
        if parent_pid(pid) != parent:
            continue
        cmdline = command_line(pid)
        if cmdline is not None and pattern in cmdline:
            matches.append(pid)
    return matches


def unit_pids(cgroup: str, pattern: str) -> list[int]:
    """PIDs in cgroup `cgroup` (a unit's ControlGroup) whose command line
    mentions `pattern`: the members cgroup.procs lists, found without reading
    it. For the Worker guard, where a loose match only errs toward leaving a
    unit alone; the listeners the orphan check signals come from
    unit_listeners.

    The whole cgroup file must be the unified-hierarchy line for `cgroup`, so a
    process in a child cgroup is left out, as cgroup.procs leaves it out.
    """
    members: list[int] = []
    for pid in all_pids():
        cmdline = command_line(pid)
        if cmdline is None or pattern not in cmdline:
            continue
        if _in_cgroup(pid, cgroup):
            members.append(pid)
    return members


def _in_cgroup(pid: int, cgroup: str) -> bool:
    membership = _read_text(f"/proc/{pid}/cgroup")
    return membership is not None and membership.rstrip("\n") == f"0::{cgroup}"


@dataclass(frozen=True)
class ServingLanes:
    """The lanes live listeners serve, and the listeners whose lane is unknown."""

    lanes: frozenset[Path]
    unplaced: tuple[int, ...]


def _argv0(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return os.fsdecode(raw.split(b"\0", 1)[0])


def runs_listener(argv0: str) -> bool:
    """Whether a process whose argv[0] is `argv0` is the listener binary.

    By argv[0] rather than anywhere in the command line, so a command that only
    mentions the name - a `pgrep -f Runner.Listener` - is not taken for one.
    """
    return PurePath(argv0).name == "Runner.Listener"


@dataclass(frozen=True)
class Process:
    """A process as it was when found: its pid, and the identity that tells it
    apart from whatever holds that pid later."""

    pid: int
    identity: str


def _listener_in(pid: int, cgroup: str) -> Process | None:
    """`pid` as a listener in `cgroup`, with the identity its facts were read
    under, or None when it is not one.

    The identity is read on both sides of the facts it vouches for, so a pid
    that exited and came back as another process between the reads is refused
    rather than taken for the listener that was there first.
    """
    identity = process_identity(pid)
    if identity is None:
        return None
    argv0 = _argv0(pid)
    if argv0 is None or not runs_listener(argv0) or not _in_cgroup(pid, cgroup):
        return None
    return Process(pid, identity) if process_identity(pid) == identity else None


def unit_listeners(cgroup: str) -> list[Process]:
    """The listener processes in cgroup `cgroup` (a unit's ControlGroup).

    By argv[0], not by command-line substring, and each with its identity:
    these are the processes the orphan check may signal, so a command that only
    names the listener must not be one, and a signal sent later must be able to
    tell the process found here from one that took its pid since.
    """
    found = (_listener_in(pid, cgroup) for pid in all_pids())
    return [process for process in found if process is not None]


def is_unit_listener(process: Process, cgroup: str) -> bool:
    """Whether `process` is still the listener in `cgroup` it was found as."""
    return _listener_in(process.pid, cgroup) == process


def lane_of_listener(argv0: str) -> Path | None:
    """The lane a listener at `argv0` serves, when that is `<lane>/bin/Runner.Listener`."""
    binary = Path(argv0)
    if (
        not binary.is_absolute()
        or binary.name != "Runner.Listener"
        or binary.parent.name != "bin"
    ):
        return None
    return binary.parent.parent


def listener_lane(pid: int) -> Path | None:
    """The lane listener `pid` serves, or None when it cannot be read."""
    argv0 = _argv0(pid)
    return None if argv0 is None else lane_of_listener(argv0)


def serving_lanes() -> ServingLanes:
    """Every lane on this host that a live listener is serving."""
    lanes: set[Path] = set()
    unplaced: list[int] = []
    for pid in all_pids():
        argv0 = _argv0(pid)
        if argv0 is None or not runs_listener(argv0):
            continue
        lane = lane_of_listener(argv0)
        if lane is None:
            unplaced.append(pid)
        else:
            lanes.add(lane)
    return ServingLanes(lanes=frozenset(lanes), unplaced=tuple(unplaced))
