"""One watchdog pass, as `python3 -m runner_liveness` runs it."""

import sys

from runner_liveness.watchdog import main

sys.exit(main())
