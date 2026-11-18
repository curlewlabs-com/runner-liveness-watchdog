# runner-liveness-watchdog

## What it is

A systemd timer and a standard-library Python package for Linux hosts that
run self-hosted GitHub Actions runners as services. Every five minutes it looks
at each `actions.runner.*.service` unit on the host and repairs the ones that
have stopped taking jobs while still looking healthy:

- a listener whose connection to GitHub has gone silent: **restart** the unit;
- a unit systemd has given up on (`failed`): **restart** it;
- a second listener left running beside the unit's own after a stop timed
  out: **kill** the leftover one;
- optionally, a lane GitHub has stopped assigning work to while its
  same-label siblings on the host take every job: **restart** it.

It never restarts a unit that is running a job, and never touches a unit that
was stopped on purpose.

**Scope.** It runs as root: it restarts systemd units and sends signals to
runner processes. It supports runners installed as systemd services and
nothing else. It has been run on Linux hosts and virtual machines carrying
several runners each, all registered to one repository in its default runner
group and running that repository's own workflows. A host whose runners take
untrusted code - pull requests from forks, or any workflow you did not write -
is outside what it has been used for; read [SECURITY.md](SECURITY.md) before
running it there.

## Why it exists

A self-hosted runner can show `online` and idle while jobs queue, or drop to
`offline` while its service still reports `active (running)`. Nothing on the
host notices: systemd's `Restart=` only acts on a process that exits, and the
runner's own retry only acts on a request that fails, not on one that never
returns. The usual fix is someone noticing and running
`sudo ./svc.sh stop && sudo ./svc.sh start` by hand, often days later.

This tool keys on what each of those states actually looks like from the host:

- **Wedged listener.** The listener is blocked on a broker connection that
  will never answer: the request was sent and acknowledged, so the kernel has
  nothing to retransmit and the socket looks like a healthy idle one. A socket
  whose source address died under it, such as one opened over an interface
  that later lost its lease, is one way to get there. The watchdog reads each
  listener's quietest established socket from `ss` (`lastsnd`), and restarts
  the unit when even that one has been silent past a limit. Healthy idle
  listeners send every minute or so, far below the 30-minute default.
- **Given-up unit.** A launcher that cannot exec, or a crash loop past the
  unit's start limit, leaves the unit `failed` with no listener at all. The
  watchdog clears the failure and restarts it. `inactive` means an operator
  stopped it, and is left alone.
- **Orphaned listener.** The runner's generated unit stops with
  `KillMode=process`, so a stop that times out kills only the wrapper. The old
  listener lives on with its GitHub session, the next start brings a second one
  up beside it, and the two can take the same job and share its work
  directory. The watchdog finds the listener in the unit's cgroup that does not
  descend from the unit's main process and, once it has no job, kills it -
  its supervisor first, so nothing respawns it.
- **Starved lane.** The listener keeps long-polling, GitHub shows it online
  and not busy, and yet it is assigned nothing while its siblings take every
  job. A restart opens a new session and it takes work at once. The watchdog
  measures how long the lane has been idle while *every* same-label sibling on
  the host was running a job at once, from the runners' own
  `_diag/Worker_*.log` files. In that state the lane was the host's only free
  runner for those labels, so a healthy one would have been given the next
  job.

## How to use it

### Install

As root, on each runner host, from a checkout of this repository:

```sh
install -d -m 0755 /usr/local/lib/runner-liveness
cp -R src/runner_liveness /usr/local/lib/runner-liveness/
install -m 0644 systemd/runner-liveness.service systemd/runner-liveness.timer \
    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now runner-liveness.timer
```

The service runs `/usr/bin/python3 -m runner_liveness` from
`/usr/local/lib/runner-liveness`, so the copied package is all it needs. To
upgrade, copy the package again; the next timer pass runs it.

### Watch it

```sh
systemctl list-timers runner-liveness.timer   # next pass
journalctl -u runner-liveness.service -f      # one line per decision
```

Every pass ends with a summary line, even when nothing is wrong, so a watchdog
that stopped running is distinguishable from one with nothing to do. The pass
exits non-zero while a runner is still down and the pass did not bring it back,
so `systemctl --failed` lists `runner-liveness.service` until it does. Under a
still-active timer that is the intended shape: the next pass that finds nothing
wrong clears it.

### Settings

All are optional and are set in a drop-in, so a reinstall of the unit does not
overwrite them (`systemctl edit runner-liveness.service`):

```ini
[Service]
Environment=RUNNER_LIVENESS_LABELS_FILE=.runner-labels
```

| Variable | Default | What it does |
|---|---|---|
| `RUNNER_LIVENESS_LABELS_FILE` | unset: starvation check off | Name of a file in each runner directory that lists the runner's labels, turning the starvation check on. |
| `RUNNER_LIVENESS_SILENCE_LIMIT_MS` | `1800000` (30 min) | How long a listener's quietest socket must have been silent, before the previous pass, to be called wedged. |
| `RUNNER_LIVENESS_STARVED_LIMIT_SECONDS` | `1200` (20 min) | How long a lane's same-label siblings must all have been running jobs at once while it idled, to be called starved. |
| `RUNNER_LIVENESS_DRY_RUN` | `0` | Any other value logs each decision as `WOULD restart` / `WOULD kill` and acts on nothing. |

**Silence limit.** Job length does not move it: a unit running a job is never
judged, and an idle listener's polling cadence is the runner's, not your
workload's. Raise it before lowering it - a false restart can cost the job
GitHub is handing the listener at that moment.

**Starvation limit.** On a fleet whose jobs routinely run longer than this on
every sibling at once while nothing else is queued for their labels, the idle
lane reads as starved and is restarted once an hour; raise the limit above
that job length if those restarts matter to you.

### The labels file

Without it the watchdog does its main job - wedged, failed and orphaned
runners - and skips only the starvation check. The runner keeps no local
record of the labels it registered with, so the starvation check needs one to
know which runners on a host are interchangeable. To turn it on, write the
labels into a file in each runner directory (the directory `config.sh` ran
in), comma-separated as `config.sh --labels` takes them, and name that file in
`RUNNER_LIVENESS_LABELS_FILE`:

```sh
echo "self-hosted,linux,x64,gpu" > /home/runner/actions-runner-1/.runner-labels
```

Runners whose files hold the same set are treated as interchangeable, so the
file has to say everything GitHub routes on. Order, case and spaces around a
label do not matter. Runners registered to different repositories,
organizations or runner groups are not interchangeable even with identical
labels: give each such set its own entry, such as the group's name, so they do
not match. Once the check is on, give every runner on the host a labels file:
while any serving runner lacks one, the starvation check skips the whole host,
since that runner might be an idle sibling.

### Proving detection works without restarting anything

A dry run with a limit low enough that healthy runners trip it shows the
watchdog reading every unit:

```sh
sudo env RUNNER_LIVENESS_DRY_RUN=1 RUNNER_LIVENESS_SILENCE_LIMIT_MS=1000 \
    sh -c 'cd /usr/local/lib/runner-liveness && /usr/bin/python3 -m runner_liveness'
```

Every idle listener should be named with its silence, as
`listener silent for Ns, but only 0s of it before the previous pass ... - not restarting`:
only silence that predates the previous timer pass counts, and a healthy
listener has sent since then. A runner executing a job is skipped by design,
so a busy host names fewer runners than it has.

The starvation check is reached only with the silence limit at its default,
so lower the starvation limit instead:

```sh
sudo env RUNNER_LIVENESS_DRY_RUN=1 RUNNER_LIVENESS_STARVED_LIMIT_SECONDS=1 \
    RUNNER_LIVENESS_LABELS_FILE=.runner-labels \
    sh -c 'cd /usr/local/lib/runner-liveness && /usr/bin/python3 -m runner_liveness'
```

It names each idle lane whose siblings were all running a job at once for any
stretch since it last became free. On a lightly loaded host expect no names.

## What it never does

- **Interrupt a job.** A unit with a live `Runner.Worker` is not restarted, and
  an orphaned listener with a Worker child is left for a later pass. The
  Worker set is re-read just before the restart and before every signal, so a
  job dispatched while the pass was deciding is not interrupted either; only
  a job landing in the moment between that read and the `systemctl` calls
  that follow it can be. A `failed` unit whose Worker outlived its main
  process is held, and reported as down.
- **Undo an operator.** `inactive`, `activating` and `deactivating` units are
  counted in the summary and left alone.
- **Thrash.** A unit it restarted is not restarted again for an hour. A runner
  still down inside that hour keeps the pass failing, so it stays visible. A
  restart stamp that cannot be read holds the restart rather than skipping the
  cooldown.
- **Guess.** Whatever it cannot read - `ss` failing, an unlistable `/proc`, a
  main PID it cannot resolve, a worker log it cannot parse - is reported and
  read as "do not act", never as "nothing there".
- **Signal a process it did not judge.** An orphaned listener is recorded
  with its start time when it is found, by its executable rather than by any
  command line that mentions it. Before every signal the watchdog re-reads
  that start time, the executable, the unit's cgroup and the unit's main
  process, and sends nothing if any of them changed.
- **Read `cgroup.procs`.** On kernels without the fix for CVE-2026-98163, a
  `cgroup.procs` read that races an exiting process can panic the kernel. The
  watchdog finds a unit's processes from each process's own
  `/proc/<pid>/cgroup` instead.

## Limits

What it deliberately does not cover, and why:

- **A host that sleeps while a job runs** inflates the starvation measure: a
  job spans its worker log's name to its last write, so a nap inside a
  sibling's job counts as busy time. The cost is at most one restart of an idle
  lane per hour. A host that sleeps only when idle is unaffected. Socket
  silence is corrected for suspend.
- **Two starved lanes in one sibling group** each see the other idle, so
  neither is called.
- **Siblings all on long jobs with nothing queued** read as starvation for the
  idle lane; see the starvation limit above.
- **Siblings are runners with equal label sets.** The check assumes each job
  requests a runner's whole set, as it does where runners differ by a label
  that names them apart. Any two runners sharing a label can compete for a job
  that requests only what they share, so no rule over label sets alone is
  complete; where workflows request such a subset, a differing runner left
  idle can make the judged lane read as starved. Give such runners equal
  labels files, or leave the check off.
- **A per-process `/proc` read that fails is read as that process having
  exited.** The watchdog runs as root and reads `cmdline`, `status`, `stat`
  and `cgroup`, which are world-readable and fail only once the process is
  gone. A listing of `/proc` that fails is reported, and that unit is not
  judged on that pass.

## Requirements

- Linux with systemd on the unified cgroup hierarchy (cgroup v2).
- Runners installed as services with the runner's own `svc.sh`, so units are
  named `actions.runner.*.service` and each runner directory holds
  `bin/Runner.Listener` and `_diag/`.
- `/usr/bin/python3` 3.12 or newer, and iproute2's `ss`.
- Root, for `systemctl restart`, the signals, and `ss -p` on the runner user's
  sockets.

launchd and Windows service runners are not covered.

## Versioning

Releases are tagged `vMAJOR.MINOR.PATCH`. Before 1.0, a minor release may
change a setting's name or default; the release notes say so.

## Development

```sh
python3 -m unittest discover -s tests -t .
python3 -m pip install -r requirements-dev.txt
ruff format --check . && ruff check . && mypy
```

The tests that read a live process's `/proc` entries skip off Linux. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT license ([LICENSE-MIT](LICENSE-MIT))

at your option. Releases published under the MIT license alone may also be used
under either license.

### Contribution

By submitting a contribution for inclusion in this project, you agree that it
is dual licensed as above, without any additional terms or conditions.
