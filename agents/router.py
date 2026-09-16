"""The router: which channel is actually answering right now.

The routing table in app.py (BAY_ROUTES) says what to prefer in
principle - DeepSeek for code, gpt-oss for chat, the local model as the
floor. It cannot say what is true at 4pm on a Tuesday: that Groq has
been returning 429s for the last five minutes, or that the local model
is taking forty seconds to start because something else has the CPU.
Until now the site found that out one person at a time, each of them
waiting on the channel before failing over.

This reads the evidence every reply leaves (db.channel_stats) and
scores each (provider, model) on the last WINDOW minutes:

  failing   more than half of recent attempts failed, with enough of
            them to mean it -> the channel is demoted below every
            healthy one
  slow      the median time to first token is over SLOW_TTFT seconds
            -> demoted below channels that are answering promptly

A channel with no recent evidence is assumed healthy: the cost of
guessing wrong is one failover, and the alternative - refusing to try
anything unproven - would never let a recovered channel back in.
Rankings are relative, never absolute: rank() only reorders what it is
given and never removes a candidate, so a site where every channel is
struggling still asks somebody rather than nobody.
"""
from __future__ import annotations

import datetime
import statistics
import time

import db

WINDOW_MINUTES = 10
MIN_SAMPLES = 3
SLOW_TTFT = 8.0          # seconds to first token that a person notices
KEEP_DAYS = 7

# The window is read on every reply; the query is cheap (an index on
# created) but not free, so the last answer is reused for a few seconds.
_cache: dict = {"at": 0.0, "since": "", "rows": []}
CACHE_SECONDS = 5


def _iso(dt: datetime.datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def record(provider: str, model: str, ok: bool, ttft: float | None = None,
           total: float | None = None, tokens: int | None = None,
           reason: str = "") -> None:
    """Note how one reply went. Called from the reply path; never
    raises, never blocks on anything but the local database."""
    db.channel_record(provider, model, ok, ttft, total, tokens, reason)
    _cache["at"] = 0.0


def _recent() -> list[dict]:
    now = time.monotonic()
    if now - _cache["at"] < CACHE_SECONDS:
        return _cache["rows"]
    since = _iso(datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(minutes=WINDOW_MINUTES))
    rows = db.channel_window(since)
    _cache.update({"at": now, "since": since, "rows": rows})
    return rows


def health(provider: str, model: str | None = None) -> dict:
    """What the last few minutes say about a channel.

    -> {"samples", "failures", "failing", "ttft", "slow", "score"}.
    `model` None means the provider as a whole - a 429 from Groq is a
    fact about the key, not the model that hit it.
    """
    rows = [r for r in _recent() if r["provider"] == provider
            and (model is None or r["model"] == model)]
    samples = len(rows)
    failures = sum(1 for r in rows if not r["ok"])
    ttfts = [r["ttft"] for r in rows if r["ok"] and r["ttft"] is not None]
    ttft = statistics.median(ttfts) if ttfts else None
    failing = samples >= MIN_SAMPLES and failures * 2 > samples
    slow = ttft is not None and len(ttfts) >= MIN_SAMPLES and ttft > SLOW_TTFT
    # Lower is better. Failing outranks slow: a slow answer is still an
    # answer.
    score = (2 if failing else 0) + (1 if slow else 0)
    return {"samples": samples, "failures": failures, "failing": failing,
            "ttft": ttft, "slow": slow, "score": score}


def rank(candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The same candidates, healthy ones first. A stable sort, so the
    routing table's own order decides among equals."""
    return sorted(candidates, key=lambda c: health(c[0], c[1])["score"])


def summary(hours: int = 24) -> list[dict]:
    """Per (provider, model) over the last `hours`, for the dashboard:
    replies, failure rate, median first-token and total time, tokens a
    second. Sorted busiest first."""
    since = _iso(datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(hours=hours))
    rows = db.channel_window(since)
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["provider"], r["model"]), []).append(r)
    out = []
    for (provider, model), rs in groups.items():
        oks = [r for r in rs if r["ok"]]
        ttfts = [r["ttft"] for r in oks if r["ttft"] is not None]
        totals = [r["total"] for r in oks if r["total"] is not None]
        rates = [r["tokens"] / r["total"] for r in oks
                 if r["tokens"] and r["total"] and r["total"] > 0.2]
        reasons: dict[str, int] = {}
        for r in rs:
            if not r["ok"]:
                reasons[r["reason"] or "failed"] = reasons.get(r["reason"] or "failed", 0) + 1
        out.append({
            "provider": provider, "model": model,
            "replies": len(rs), "failures": len(rs) - len(oks),
            "failure_rate": round((len(rs) - len(oks)) / len(rs), 3) if rs else 0.0,
            "ttft_p50": round(statistics.median(ttfts), 2) if ttfts else None,
            "total_p50": round(statistics.median(totals), 2) if totals else None,
            "tokens_per_s": round(statistics.median(rates), 1) if rates else None,
            "reasons": reasons,
            "now": health(provider, model),
        })
    out.sort(key=lambda x: -x["replies"])
    return out


def prune() -> int:
    """Forget evidence older than KEEP_DAYS. -> rows removed."""
    before = _iso(datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=KEEP_DAYS))
    return db.channel_prune(before)
