"""Who is subscribed to what.

The registry is reference-counted per instrument, because two users watching
NIFTY is one subscription at the provider and unsubscribing one of them must not
stop the other's prices. It also records what has actually been *sent* to the
feed, which is what makes replay after a reconnect correct: on a new connection
the desired set is resent in full rather than the delta since the drop, since
the delta since a drop is precisely the thing that was lost.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class SubscriptionRegistry:
    #: instrument id -> how many subscribers want it.
    _wanted: dict[uuid.UUID, int] = field(default_factory=dict)
    #: What the current connection has been told about.
    _sent: set[uuid.UUID] = field(default_factory=set)

    def add(self, instrument_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        """Register interest. Returns the ids that are newly wanted."""
        new: set[uuid.UUID] = set()
        for instrument_id in instrument_ids:
            count = self._wanted.get(instrument_id, 0)
            if count == 0:
                new.add(instrument_id)
            self._wanted[instrument_id] = count + 1
        return new

    def remove(self, instrument_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        """Drop interest. Returns the ids nobody wants any more."""
        dropped: set[uuid.UUID] = set()
        for instrument_id in instrument_ids:
            count = self._wanted.get(instrument_id, 0)
            if count <= 1:
                self._wanted.pop(instrument_id, None)
                if instrument_id in self._sent:
                    dropped.add(instrument_id)
            else:
                self._wanted[instrument_id] = count - 1
        self._sent -= dropped
        return dropped

    @property
    def wanted(self) -> set[uuid.UUID]:
        return set(self._wanted)

    @property
    def sent(self) -> set[uuid.UUID]:
        return set(self._sent)

    def pending(self) -> set[uuid.UUID]:
        """Wanted but not yet sent to the current connection."""
        return self.wanted - self._sent

    def mark_sent(self, instrument_ids: set[uuid.UUID]) -> None:
        self._sent |= set(instrument_ids)

    def connection_lost(self) -> None:
        """A new connection knows nothing. Everything must be resent."""
        self._sent.clear()

    def subscriber_count(self, instrument_id: uuid.UUID) -> int:
        return self._wanted.get(instrument_id, 0)
