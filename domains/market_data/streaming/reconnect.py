"""Reconnect timing.

Separated from the connection so it can be tested without one. The properties
that matter are dull and easy to get wrong: the delay grows, it is capped, it is
jittered so a fleet does not reconnect in lockstep, and it resets only after a
connection has actually *worked* rather than merely opened.

That last point is the one that bites. A feed that accepts a connection and then
drops it immediately will otherwise be retried at the floor delay forever, which
is a denial-of-service attack on the provider carried out by our own client.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    initial_seconds: float = 1.0
    maximum_seconds: float = 60.0
    multiplier: float = 2.0
    #: Fraction of the delay that is randomised, in ``[0, 1]``.
    jitter: float = 0.2

    def delay_for(self, attempt: int, random_value: float | None = None) -> float:
        """Delay before attempt ``attempt`` (1 = the first retry).

        ``random_value`` is injectable so a test can pin the jitter rather than
        assert on a range and hope.
        """
        if attempt < 1:
            return 0.0
        raw = self.initial_seconds * (self.multiplier ** (attempt - 1))
        capped = min(raw, self.maximum_seconds)
        if self.jitter <= 0:
            return capped
        draw = random.random() if random_value is None else random_value
        # Jitter downward only, so the cap remains a real cap.
        return capped * (1.0 - self.jitter * draw)


@dataclass
class ConnectionAttempts:
    """Counts attempts and decides when the counter may be reset.

    ``succeeded()`` is called when the connection has delivered something, not
    when it opened.
    """

    policy: BackoffPolicy = BackoffPolicy()
    attempt: int = 0
    #: Consecutive connections that opened and produced nothing.
    empty_connections: int = 0

    def next_delay(self, random_value: float | None = None) -> float:
        self.attempt += 1
        return self.policy.delay_for(self.attempt, random_value)

    def succeeded(self) -> None:
        self.attempt = 0
        self.empty_connections = 0

    def opened_but_delivered_nothing(self) -> None:
        self.empty_connections += 1
