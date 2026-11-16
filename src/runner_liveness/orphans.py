"""Killing a listener that a timed-out stop left running beside its unit's own.

The watchdog module decides which listener is an orphan and whether it is idle;
this module owns sending it signals. Every signal re-reads the identity of the
process it is meant for at the moment of sending, through the procfs module,
because the gaps between capture and signal - the grace period above all - are
where a pid can exit and come back as another process, and a root watchdog's
SIGKILL must not reach that one. POSIX offers no signal conditional on
identity, so a recycled pid is signalled only inside the gap between the
re-read and the syscall, which this places as narrowly as it can be.

What happened is reported through the `report` callable the watchdog passes in,
as it happens, so the journal shows a kill before the grace period it waits out
rather than after.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable

from runner_liveness import procfs


def signal_if_same(pid: int, identity: str, signum: signal.Signals) -> bool:
    """Send `signum` to `pid` only if it is still the process `identity` names.

    Every signal, the KILL after the grace period above all, re-reads the
    identity: a pid that exited and was reused during the sleep must not receive
    a root watchdog's SIGKILL.
    """
    if procfs.process_identity(pid) != identity:
        return False
    try:
        os.kill(pid, signum)
    except OSError:
        return False
    return True


def still_supervised(
    pid: int, pid_identity: str, parent: int, parent_identity: str
) -> bool:
    """True while listener `pid` is still `pid_identity` AND its parent is still
    `parent` with `parent_identity`.

    A parent is only ever signalled on the strength of supervising this
    listener, so the whole relationship is re-read at the moment of signalling:
    a supervisor that exited and whose pid came back as something else is not
    the parent any more, whatever was captured.
    """
    return (
        procfs.process_identity(pid) == pid_identity
        and procfs.parent_pid(pid) == parent
        and procfs.process_identity(parent) == parent_identity
    )


def kill_orphan_listener(
    unit: str,
    listener: procfs.Process,
    still_orphan: Callable[[], bool],
    dry_run: bool,
    report: Callable[[str], None],
) -> bool:
    """Kill an idle orphaned listener and its supervisor; True once it was signalled.

    An orphaned listener: a stop that timed out under the unit's
    KillMode=process SIGKILLed only the wrapper and left the listener alive with
    the runner's session, and the next start brought a second listener up
    beside it. The two then share every job's work directory and corrupt each
    other's runs, so an idle orphan is killed here rather than waited out. Its
    parent (the RunnerService.js supervisor) goes first, or it respawns the
    listener it was supervising.

    `listener` carries the identity it was found with, and `still_orphan`
    re-reads everything the watchdog judged it on. Both are checked before every
    signal: the watchdog's scans between finding the listener and this call are
    long enough for a pid to be reused, and an identity captured here instead of
    at enumeration would vouch for whatever holds the pid by now.
    """
    pid, pid_identity = listener.pid, listener.identity
    if procfs.process_identity(pid) != pid_identity or not still_orphan():
        report(
            f"{unit}: listener pid {pid} is no longer the orphan found - not signalled"
        )
        return False
    parent = procfs.parent_pid(pid) or 1
    parent_identity = procfs.process_identity(parent) if parent > 1 else None
    if dry_run:
        report(
            f"{unit}: orphaned listener pid {pid} (parent {parent}) - WOULD kill (dry run)"
        )
        return False
    report(f"{unit}: killing orphaned listener pid {pid} and its parent {parent}")
    # The supervisor first, and outright: a TERM it survives is answered by
    # respawning the listener the moment that one exits on its own TERM, and
    # the respawn would outlive this pass. It holds no broker session of its
    # own, so nothing is lost to a KILL - and it is signalled only while it
    # demonstrably still is this listener's supervisor, re-read here at the
    # moment of signalling. With it gone, the listener's grace period below
    # has nothing left to respawn it.
    if parent_identity is not None:
        if still_orphan() and still_supervised(
            pid, pid_identity, parent, parent_identity
        ):
            try:
                os.kill(parent, signal.SIGKILL)
            except OSError:
                pass
        else:
            report(
                f"{unit}: supervisor {parent} not re-verified as the orphan's parent - not signalled"
            )
    # TERM before KILL so the listener can release its session with the broker;
    # a listener KILLed outright leaves a session the unit's own listener must
    # wait out.
    if still_orphan():
        signal_if_same(pid, pid_identity, signal.SIGTERM)
    time.sleep(5)
    if still_orphan():
        signal_if_same(pid, pid_identity, signal.SIGKILL)
    return True
