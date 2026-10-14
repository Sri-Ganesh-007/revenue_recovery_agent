"""Backend-enforced access control for the public demo. CORS is NOT authentication; this is.

* DEMO_ACCESS_TOKEN (server-side env var, never shipped in the frontend bundle) is a shared access code. When it is
  set, every protected endpoint needs the header `X-Demo-Token: <code>`; visitors type the code into the UI.
* DEMO_MODE=true without a token fails closed (503) unless DEMO_ALLOW_OPEN_ACCESS=true (local development only).
* With DEMO_MODE off and no token, nothing changes: the original endpoints behave exactly as before.
* The in-memory budgets cap how many model calls / synthetic events a single server process allows per hour while
  DEMO_MODE is on. They reset on restart and are per process (Render free = one process).
"""
import hmac
import threading
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException

from app.config import settings

WINDOW_SECONDS = 3600
_lock = threading.Lock()
_events: dict[str, deque] = defaultdict(deque)


def access_required() -> bool:
    return bool(settings.demo_access_token) or (settings.demo_mode and not settings.demo_allow_open_access)


def require_access(x_demo_token: str | None = Header(default=None, alias="X-Demo-Token")):
    token = settings.demo_access_token
    if token:
        if not x_demo_token or not hmac.compare_digest(x_demo_token.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="A valid demo access code is required (header X-Demo-Token).")
    elif settings.demo_mode and not settings.demo_allow_open_access:
        raise HTTPException(status_code=503, detail="Demo access is not configured: set DEMO_ACCESS_TOKEN on the backend.")


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
