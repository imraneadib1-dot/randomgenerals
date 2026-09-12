"""Per-caller rate limits, in process.

    from ratelimit import Limiter, limited

    CHAT = Limiter(rate=20, per=60, burst=5)

    @app.route("/api/chat", methods=["POST"])
    @limited("chat", CHAT, key=lambda: "user:" + current_owner_id())
    def chat(): ...

WHY THIS EXISTS

Nothing limited how often one caller could hit the generation routes,
sign-in, or sign-up. The only "rate limit" was Groq's per-minute token
budget - shared by everyone on the site - so a single script looping
/api/chat drained it for every other visitor, and a six-digit
verification code could be guessed at full speed.

A token bucket per key: each key holds up to `burst` tokens, refilled
at `rate` per `per` seconds, and a request takes one. Bursts are
allowed (five quick messages is how people actually type); a sustained
rate above the limit is not.

WHY IN MEMORY

This deployment runs gunicorn with ONE worker and eight threads
(deploy/oracle-setup.sh, Dockerfile, render.yaml), so one dict behind
one lock sees every request and is correct. If --workers is ever
raised above 1, each worker gets its own buckets and the limits
silently become N times looser - move the state to SQLite then. The
Dockerfile says why workers is 1; this is one more reason.

Keys are chosen by the caller. Signed-in users are keyed by user id;
guests by IP, not by guest id, because a guest id is minted from a
cookie and a cookie can be discarded per request. Sign-in and sign-up
are keyed by IP and additionally by the email being tried.
"""
import threading
import time
from functools import wraps
from typing import Callable

from flask import jsonify, request


class Limiter:
    """A token bucket per key. Thread-safe; a Lock around a dict."""

    def __init__(self, rate: float, per: float = 60.0, burst: int | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = float(rate)          # tokens added per `per` seconds
        self.per = float(per)
        self.burst = int(burst if burst is not None else max(1, round(rate)))
        self.clock = clock
        self._buckets: dict[str, list[float]] = {}   # key -> [tokens, at]
        self._lock = threading.Lock()
        self._sweep_at = clock()

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """Take `cost` tokens for `key`. -> (allowed, retry_after_seconds).

        retry_after is 0 when allowed, else how long until enough
        tokens will have accrued - what goes in the Retry-After header.
        """
        now = self.clock()
        per_second = self.rate / self.per
        with self._lock:
            self._maybe_sweep(now)
            tokens, at = self._buckets.get(key, [float(self.burst), now])
            tokens = min(float(self.burst), tokens + (now - at) * per_second)
            if tokens >= cost:
                self._buckets[key] = [tokens - cost, now]
                return True, 0.0
            self._buckets[key] = [tokens, now]
            wait = (cost - tokens) / per_second if per_second > 0 else self.per
            return False, max(1.0, wait)

    def _maybe_sweep(self, now: float) -> None:
        """Forget keys that have been full for a while. Called under the
        lock. A bucket that has refilled completely carries no
        information, and without this the dict grows by one entry per
        address that ever visited."""
        if now - self._sweep_at < 300:
            return
        self._sweep_at = now
        per_second = self.rate / self.per
        full_after = self.burst / per_second if per_second > 0 else 0
        for key in [k for k, (_t, at) in self._buckets.items()
                    if now - at > full_after]:
            del self._buckets[key]

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)


def limited(name: str, limiter: Limiter, key: Callable[[], str],
            cost: float = 1.0):
    """Flask decorator. Answers 429 with Retry-After when the caller's
    bucket is empty. `key` is called per request, inside the request
    context, so it can read the session and the address."""
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            try:
                k = key()
            except Exception:                            # noqa: BLE001
                k = "ip:" + (request.remote_addr or "?")
            ok, wait = limiter.allow(name + ":" + k, cost)
            if not ok:
                body = jsonify({
                    "error": "Too many requests. Try again in about %d "
                             "seconds." % int(wait + 0.999),
                    "retry_after": int(wait + 0.999),
                })
                body.status_code = 429
                body.headers["Retry-After"] = str(int(wait + 0.999))
                return body
            return view(*args, **kwargs)
        return wrapped
    return decorate
