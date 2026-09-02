from __future__ import annotations


class InstrumentError(Exception):
    """Base class for instrument-domain errors."""


class InvalidInstrument(InstrumentError, ValueError):
    """An instrument violates a structural invariant.

    Raised at construction so an invalid instrument cannot exist in memory and
    no downstream code has to defend against one.
    """


class CanonicalKeyError(InstrumentError, ValueError):
    """A canonical key is malformed or inconsistent with its asset class."""
