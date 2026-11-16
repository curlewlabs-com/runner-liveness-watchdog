# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it
privately via
[GitHub's security advisory feature](https://github.com/curlewlabs-com/runner-liveness-watchdog/security/advisories/new).

Do not open a public issue for security vulnerabilities.

## Scope

The watchdog runs as root, restarts systemd units and sends signals, so its
security surface is which unit and which process those reach:

- **Units.** Only units matching `actions.runner.*.service` are judged or
  restarted.
- **Signals.** Only a process whose executable is `Runner.Listener`, in a
  runner unit's cgroup, that does not descend from the unit's main process -
  and that listener's direct parent - is signalled. Its start time is recorded
  when it is found. Before every signal the watchdog re-reads that start time,
  the executable, the cgroup and the unit's main process, so a pid that exited
  and came back as another process during the pass is not signalled. POSIX has
  no identity-checked signal, so a reuse inside the gap between the last
  re-read and the syscall is not excluded.
- **Untrusted jobs.** It has not been run on hosts whose runners take
  untrusted code. A job runs as the runner user inside the unit's cgroup, so
  it can start processes there, including ones named `Runner.Listener`.
- **Files read.** `/proc`, the runner directories' `_diag/` and, when
  configured, their labels file. That setting must be a bare file name.
- **Files written.** Timestamps under `/run/runner-liveness`.
- **Commands run.** `systemctl` and `ss`, from `PATH`, which it trusts as it
  trusts `python3`.
