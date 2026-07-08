"""In-memory sliding-window rate limiting.

Per-process only — correct for the current single-uvicorn deployment; a
multi-instance deploy would need a shared store (e.g. Redis). FastAPI
dependency factories that build on this primitive (`limit_by_ip`,
`limit_by_user`) live in `api_deps.py` to avoid a top-level module reaching
into `users.service` — see `.importlinter` boundary contracts. Keys used by
those factories are "name:identity" so different endpoints never share
buckets.
"""

from __future__ import annotations

import threading
import time
from collections import deque

__all__ = ["SlidingWindowLimiter"]


class SlidingWindowLimiter:
    def __init__(self, max_events: int, window_s: float) -> None:
        self._max = max_events
        self._window = window_s
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._events.setdefault(key, deque())
            while q and q[0] <= now - self._window:
                q.popleft()
            if len(q) >= self._max:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
