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
- **Signals.** Only a `Runner.Listener` in a runner unit's cgroup that does
  not descend from the unit's main process, and that listener's direct parent,
  are signalled. Every signal re-reads the target's start time from `/proc`
  first, so a pid that exited and was reused is not signalled; POSIX has no
  identity-checked signal, so a reuse inside the gap between that read and the
  syscall is not excluded.
- **Files read.** `/proc`, the runner directories' `_diag/` and, when
  configured, their labels file. That setting must be a bare file name.
- **Files written.** Timestamps under `/run/runner-liveness`.
- **Commands run.** `systemctl` and `ss`, from `PATH`, which it trusts as it
  trusts `python3`.
