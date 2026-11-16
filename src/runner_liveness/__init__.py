"""Restart a self-hosted GitHub Actions runner service that has stopped taking
jobs while still looking healthy. See the README.

`python3 -m runner_liveness` runs one pass; the watchdog module holds it, and
the procfs, starvation and orphans modules hold what it reads, measures and
signals.
"""

import sys

# Checked here, before any other module of the package is imported, so an older
# interpreter gets this message rather than a syntax error from a newer form.
if sys.version_info < (3, 12):  # noqa: UP036 - the point is to run on older ones
    sys.exit("runner-liveness needs Python 3.12 or newer")
