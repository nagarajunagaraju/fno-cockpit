"""Thread-safe rate limiting for every Kite REST call (Zerodha limits: quote 1/s, historical 3/s, others 10/s)."""
from __future__ import annotations

import threading
import time

LIMITS = {"quote": 1.0, "ltp": 1.0, "ohlc": 1.0, "historical_data": 2.0}
DEFAULT = 8.0


def _is_throttle(e: Exception) -> bool:
    m = str(e).lower()
    return "too many requests" in m or "429" in m or "rate limit" in m


class _Bucket:
    def __init__(self, per_sec):
        self.gap = 1.0 / per_sec
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next:
                time.sleep(self.next - now)
                now = self.next
            self.next = now + self.gap


class RateLimitedKite:
    """Wraps a KiteConnect object; method calls are throttled per endpoint group."""

    def __init__(self, kite):
        self._kite = kite
        self._buckets = {k: _Bucket(v * 0.9) for k, v in LIMITS.items()}
        self._default = _Bucket(DEFAULT)
        self.calls = 0
        self.throttled = 0

    def __getattr__(self, name):
        attr = getattr(self._kite, name)
        if not callable(attr) or name in ("login_url", "set_access_token"):
            return attr
        bucket = self._buckets.get(name, self._default)

        def wrapped(*a, **kw):
            delay = 1.0
            for attempt in range(5):
                bucket.wait()
                self.calls += 1
                try:
                    return attr(*a, **kw)
                except Exception as e:  # Kite throttling -> back off and retry, never give up on first 429
                    if attempt == 4 or not _is_throttle(e):
                        raise
                    self.throttled += 1
                    time.sleep(delay)
                    delay *= 2
        return wrapped
