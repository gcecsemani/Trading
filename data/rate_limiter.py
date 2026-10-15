"""
data/rate_limiter.py
====================
Token-bucket throttle + retry wrapper for every Kite REST call.

Zerodha's limit is 3 requests/second per API key, shared across *all* processes
using that key. ENGINE["kite_rps"] must be set to 3 / (number of processes).

Order placement gets its own, separate bucket because Kite meters orders
differently (10/s, 200/min) and because a throttled entry is far less harmful
than a throttled exit — exits are given priority via `critical=True`, which
bypasses the wait when the bucket is empty.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable

from kiteconnect import exceptions as kite_exc

import config
from core.logging_setup import get

log = get("ratelimit")

MAX_RETRIES = 4
BASE_BACKOFF = 0.8
_RATE_LIMIT_MARKERS = ("429", "too many", "rate limit", "throttle")


class TokenBucket:
    def __init__(self, rate: float, burst: int) -> None:
        self._rate = max(0.1, rate)
        self._burst = max(1, burst)
        self._tokens = float(self._burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Take one token; returns how long the caller must sleep (0 = free)."""
        with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self._burst, self._tokens + (now - self._last) * self._rate
            )
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            wait = (1.0 - self._tokens) / self._rate
            self._tokens = 0.0
            return wait


_data_bucket = TokenBucket(rate=config.ENGINE["kite_rps"], burst=3)
_order_bucket = TokenBucket(rate=6.0, burst=6)


def _is_rate_limited(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


def kite_call(fn: Callable[..., Any], *args, **kwargs) -> Any:
    """Throttled + retried Kite REST data call."""
    return _call(_data_bucket, fn, False, *args, **kwargs)


def kite_order_call(fn: Callable[..., Any], *args, critical: bool = False, **kwargs) -> Any:
    """
    Throttled + retried Kite order call.

    critical=True is for exits: never sleep on the bucket, because delaying a
    stop-loss is worse than risking a 429 (which we retry anyway).
    """
    return _call(_order_bucket, fn, critical, *args, **kwargs)


def _call(bucket: TokenBucket, fn: Callable[..., Any], critical: bool, *args, **kwargs) -> Any:
    name = getattr(fn, "__name__", str(fn))
    last_exc: Exception = RuntimeError("unreachable")

    for attempt in range(MAX_RETRIES):
        wait = bucket.acquire()
        if wait > 0:
            if critical:
                log.debug("critical call %s — skipping %.2fs throttle", name, wait)
            else:
                time.sleep(wait)

        try:
            return fn(*args, **kwargs)

        except kite_exc.TokenException:
            # Access token expired — only a fresh login fixes this.
            raise

        except kite_exc.InputException:
            # Bad payload: retrying sends the same bad payload again.
            raise

        except (kite_exc.NetworkException, kite_exc.DataException,
                kite_exc.GeneralException) as exc:
            last_exc = exc
            retryable = _is_rate_limited(exc) or isinstance(exc, kite_exc.NetworkException)
            if not retryable or attempt == MAX_RETRIES - 1:
                log.error("%s failed after %d attempt(s): %s", name, attempt + 1, exc)
                raise
            backoff = BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 0.4)
            log.warning("%s attempt %d/%d failed (%s) — retry in %.1fs",
                        name, attempt + 1, MAX_RETRIES, exc, backoff)
            time.sleep(backoff)

    raise last_exc
