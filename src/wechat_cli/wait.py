import time
from dataclasses import dataclass

from .errors import AutomationError


@dataclass
class WaitResult:
    value: object
    elapsed_ms: float
    checks: int
    wakes: int


def wait_until(predicate, wake, timeout, description, clock=time.monotonic):
    started = clock()
    deadline = started + timeout
    checks = 0
    wakes = 0
    while True:
        checks += 1
        value = predicate()
        if value:
            return WaitResult(value, round((clock() - started) * 1000, 3), checks, wakes)
        remaining = deadline - clock()
        if remaining <= 0:
            raise AutomationError("TIMEOUT", f"Timed out waiting for {description}",
                                  {"timeout_s": timeout, "checks": checks, "wakes": wakes}, True)
        wake(min(remaining, 0.25))
        wakes += 1
