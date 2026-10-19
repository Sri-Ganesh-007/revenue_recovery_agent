"""Usage budgets for the public demo (there is no login or access code).

While DEMO_MODE=true these in-memory, per-process, one-hour budgets cap how many model-calling requests and synthetic
gateway events the server accepts, so the demo cannot burn the model quota or fill the database. They reset on restart.
With DEMO_MODE off nothing is limited and the original endpoints behave exactly as before.
"""
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException

from app.config import settings

WINDOW_SECONDS = 3600
_lock = threading.Lock()
_events: dict[str, deque] = defaultdict(deque)


def spend(bucket: str, limit: int, units: int = 1) -> None:
    """Consume `units` from a sliding one-hour budget; raise 429 when it would be exceeded. Only enforced in DEMO_MODE."""
    if not settings.demo_mode:
        return
    now = time.monotonic()
    with _lock:
        q = _events[bucket]
        while q and now - q[0] > WINDOW_SECONDS:
            q.popleft()
        if len(q) + units > limit:
            raise HTTPException(status_code=429, detail=f"Demo budget reached ({limit} {bucket} per hour). Try again later.")
        q.extend([now] * units)


def reset_budgets() -> None:
    with _lock:
        _events.clear()


def limit_llm_runs():
    spend("model runs", settings.demo_max_llm_runs_per_hour)


def limit_events():
    spend("gateway events", settings.demo_max_events_per_hour)
