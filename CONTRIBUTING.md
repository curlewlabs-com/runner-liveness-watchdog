# Contributing

Thanks for your interest in improving `runner-liveness-watchdog`. The bar for a
change is whether it keeps the watchdog's two promises: it repairs a runner
that has stopped serving GitHub, and it never costs a healthy runner its job.
A false restart is the worse failure, so a change that makes detection more
eager needs evidence from real hosts, not a model of them.

## Evidence for a detection change

The replays under `tests/fixtures/` are captures from real hosts: `ss` output
and `_diag` worker-log names and mtimes, with addresses, runner names and
non-runner process names replaced. A change to what counts as wedged or
starved comes with the capture that shows it, trimmed the same way, and a test
that replays it at the default limits.

## Running the checks

These are the checks CI runs on every push and pull request:

```sh
python3 -m unittest discover -s tests -t .
python3 -m pip install -r requirements-dev.txt
ruff format --check . && ruff check . && mypy
```

The `/proc` tests skip off Linux; CI runs them on Linux x86_64 and arm64.

## Conventions

- **Tests ship with the change.** A bug fix or a feature includes a test in the
  same pull request. A test that guards a failure mode says which one.
- **Fail closed.** Whatever cannot be read is reported and read as "do not
  act", never as an empty answer.
- **Standard library only at runtime**, so an install is a copy of the package.
- **No new settings without a reason a host could not run without.** The
  defaults are what the watchdog was tuned on.
- **Pin dependencies exactly.** An action reference is its release's commit
  SHA with the version beside it, and the tools in `requirements-dev.txt` are
  exact versions. Renovate proposes their updates; a pin written inline in a
  workflow needs a `# renovate:` comment above it saying what the literal is.
- **Comments explain why, not what.**
- **Keep tracked text ASCII.**

## Submitting

Keep each pull request focused on one change, make sure the checks pass, and
describe the why in the pull request body. CI must be green before merge.

## Releasing

A patch or minor release needs no approval: whoever cuts it follows the steps
below in order, on a `main` commit whose required checks have passed. A major
version, a 1.0, or a minor before 1.0 that changes an interface consumers use
needs the maintainer's explicit approval before its tag is pushed.

Every release gets a fixed `vMAJOR.MINOR.PATCH` tag. The repository blocks
updates and deletions of those tags, and publishing the GitHub Release makes
the tag and its assets immutable.

1. Bump `version` in `pyproject.toml` and merge that to `main`.
2. Tag the merge commit `vMAJOR.MINOR.PATCH` and push the tag.
3. Publish it: `gh release create vMAJOR.MINOR.PATCH`.

## License

Unless you explicitly state otherwise, any contribution intentionally submitted
for inclusion in this project by you, as defined in the Apache-2.0 license,
shall be dual licensed under the MIT and Apache-2.0 licenses, as the README's
License section describes, without any additional terms or conditions.
