"""How many times one address may post the public form in an hour.  # SPEC §4.2

A sliding window per client address, in memory. The public form is the one route a
stranger can write through, and the queue it writes into is read by people; a bot that
posts a thousand junk deals has cost the team an afternoon. The honeypot on the page catches
the dumbest of those for nothing, and this catches the rest - and a person who keeps pressing
the button - without a CAPTCHA, which would cost every real borrower something to stop a few
bots.

In memory, deliberately. The count survives nothing - a restart or a second process starts
from zero - and that is the right trade for what it protects: the harm is a flood, and a
flood that has to re-start after every deploy is not one. Storing attempts in the database
would mean writing a row for every bot, which is the thing being prevented. Every attempt
counts, refused ones included, so an address that is over the line stays over it while it
keeps trying.

The limit is config (``web_intake.submissions_per_hour_per_ip``) and the window is an hour.

The client address is what the nearest trusted proxy says it is. On Render the service sits
behind one, which appends the real address to ``X-Forwarded-For``; a client can put whatever
it likes at the *front* of that header, so it is the last entry that is believed, and the
socket's own peer when there is no header at all - which is a developer's machine.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime, timedelta

from fastapi import Request

WINDOW = timedelta(hours=1)
FORWARDED_FOR = "x-forwarded-for"


def client_address(request: Request) -> str:
    """The address the post came from, as the nearest proxy reports it."""
    forwarded = request.headers.get(FORWARDED_FOR, "")
    if forwarded:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client is not None else "unknown"


class RateLimiter:
    """Attempts per key over a sliding window; thread-safe, because uvicorn is."""

    def __init__(self, window: timedelta = WINDOW) -> None:
        self._window = window
        self._attempts: dict[str, deque[datetime]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, now: datetime | None = None) -> bool:
        """Record one attempt and say whether it is within ``limit`` for the window.

        The attempt is recorded either way, so a refused caller is not quietly handed a fresh
        window by being refused.
        """
        moment = now or datetime.now(UTC)
        with self._lock:
            attempts = self._attempts.setdefault(key, deque())
            cutoff = moment - self._window
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            attempts.append(moment)
            return len(attempts) <= limit

    def reset(self) -> None:
        """Forget everything; for tests, and for nothing else."""
        with self._lock:
            self._attempts.clear()


# One per process, which is one per deploy on the shape ``render.yaml`` describes.
LIMITER = RateLimiter()
