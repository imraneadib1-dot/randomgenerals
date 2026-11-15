"""What every model channel promises the caller about failing.

    from providers import ProviderUnavailable, RateLimited, Unreachable

Three channels answer chat here - pip 1.0, OpenRouter and a local
Ollama - and app.py fails over between them. That only works if they
all fail the SAME WAY, and for a while they did not: one raised when it
was busy so the request could be answered elsewhere, OpenRouter yielded
"[OpenRouter error 502.]" as if it were the reply, and Ollama let a
requests exception escape to a generic handler that wrote
"[Error talking to ollama: HTTPConnectionPool(...)]" into the
conversation. Same situation, three outcomes, and only one of them
recoverable.

These classes are the shared contract, so they live where every
channel can import them without importing each other. pip_api
re-exports them by name: the checks and app.py say
pip_api.ProviderUnavailable, and that is the same object.

THE CONTRACT

    RAISED ONLY BEFORE THE FIRST CHUNK.

That is what makes it recoverable - nothing has reached the browser,
so the caller can answer the same conversation from another channel
and the reader never sees a seam. Once any text has been yielded it is
too late to switch, and a failure after that point is yielded as a
sentence at the end of the reply instead, never raised.

Catch the base class rather than the subclasses: a drained budget and
an unreachable host both mean "ask someone else", and every call site
does the same thing for both. The subclasses exist so the caller can
decide whether WAITING would help - a rate limit refills on a known
schedule, an unreachable host does not.
"""


class ProviderUnavailable(Exception):
    """This channel cannot answer, and nothing has been streamed yet."""


class RateLimited(ProviderUnavailable):
    """The channel's per-minute allowance is spent. Refills on a clock."""


class Unreachable(ProviderUnavailable):
    """The channel could not answer at all - DNS, TLS, timeout, reset,
    or a server-side error before any content came back."""


def describe(exc: BaseException) -> str:
    """A few words for a person, never the exception's own text.

    A requests error carries the host, the port and the pool state,
    none of which belongs in a chat reply.
    """
    if isinstance(exc, RateLimited):
        return "busy"
    if isinstance(exc, Unreachable):
        return "not answering"
    return "failed"
