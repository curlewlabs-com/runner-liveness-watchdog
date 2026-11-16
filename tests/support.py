"""What the live-process tests share: a bound on waiting for the kernel.

A wait here is for something the kernel finishes on its own schedule, such as
a KILLed process leaving. The bound is a backstop against a hang, never the
expected wait, so it is generous: hitting it is a failure, not a slow pass.
"""

from __future__ import annotations

import time
from collections.abc import Callable

BACKSTOP_SECONDS = 30.0


def wait_until(condition: Callable[[], bool]) -> bool:
    """Whether `condition` became true before the backstop."""
    deadline = time.monotonic() + BACKSTOP_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True
