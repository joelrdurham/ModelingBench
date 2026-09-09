"""Overall invocation budget helpers.

Budget usage is accumulated only while ``run_loop`` is executing. An orderly
pause returns from that loop and does not consume time while waiting. A crash
cannot identify active work reliably, so an unflushed interval is not charged.
"""
from __future__ import annotations
import time
from .errors import StateError

class BudgetExhausted(StateError):
    """The user supplied overall invocation budget has been consumed."""

def remaining_seconds(budget: float | None, used_seconds: float, started: float) -> float | None:
    if budget is None:
        return None
    return max(0.0, float(budget) - float(used_seconds) - (time.monotonic() - started))

def require_remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BudgetExhausted('User budget exhausted')
    return remaining

def bounded_timeout(timeout: float, deadline: float | None) -> float:
    remaining = require_remaining(deadline)
    return min(float(timeout), remaining) if remaining is not None else float(timeout)


def deadline_expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline
