"""Instrument resolution.

The resolver has no "best effort" mode. ``AMBIGUOUS`` is a first-class outcome
that the caller must handle, because silently picking the most plausible
candidate is the single most common way a portfolio ends up holding the wrong
expiry while every downstream number looks fine.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from domains.instruments.enums import AssetClass, OptionType
from domains.instruments.errors import CanonicalKeyError
from domains.instruments.identity import build_canonical_key
from domains.instruments.models import Instrument
from domains.instruments.repository import InstrumentRepository


class ResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"


class ResolutionMethod(StrEnum):
    INSTRUMENT_ID = "INSTRUMENT_ID"
    CANONICAL_KEY = "CANONICAL_KEY"
    ALIAS = "ALIAS"
    STRUCTURED_MATCH = "STRUCTURED_MATCH"
    SYMBOL_MATCH = "SYMBOL_MATCH"


class ResolutionReason(StrEnum):
    NO_MATCH = "NO_MATCH"
    NO_ALIAS_FOR_SOURCE = "NO_ALIAS_FOR_SOURCE"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    INSUFFICIENT_FIELDS = "INSUFFICIENT_FIELDS"
    MALFORMED_REQUEST = "MALFORMED_REQUEST"


@dataclass(frozen=True, slots=True)
class ResolutionRequest:
    instrument_id: uuid.UUID | None = None
    canonical_key: str | None = None
    symbol: str | None = None
    exchange: str | None = None
    asset_class: AssetClass | None = None
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: OptionType | None = None
    source: str | None = None
    row_index: int | None = None


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    status: ResolutionStatus
    request: ResolutionRequest
    instrument: Instrument | None = None
    method: ResolutionMethod | None = None
    confidence: float = 0.0
    reason: ResolutionReason | None = None
    candidates: tuple[Instrument, ...] = field(default=())

    @property
    def is_resolved(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED


class InstrumentResolver:
    def __init__(self, repository: InstrumentRepository) -> None:
        self._repo = repository

    async def resolve(self, request: ResolutionRequest) -> ResolutionResult:
        for step in (
            self._by_instrument_id,
            self._by_canonical_key,
            self._by_derived_canonical_key,
            self._by_alias,
            self._by_structured_match,
            self._by_symbol,
        ):
            result = await step(request)
            if result is not None:
                return result

        return ResolutionResult(
            status=ResolutionStatus.UNRESOLVED,
            request=request,
            reason=ResolutionReason.NO_MATCH,
        )

    async def resolve_many(self, requests: list[ResolutionRequest]) -> list[ResolutionResult]:
        return [await self.resolve(request) for request in requests]

    # ----------------------------------------------------------------- steps
    async def _by_instrument_id(self, request: ResolutionRequest) -> ResolutionResult | None:
        if request.instrument_id is None:
            return None
        instrument = await self._repo.get(request.instrument_id)
        if instrument is None:
            return None
        return ResolutionResult(
            status=ResolutionStatus.RESOLVED,
            request=request,
            instrument=instrument,
            method=ResolutionMethod.INSTRUMENT_ID,
            confidence=1.0,
        )

    async def _by_canonical_key(self, request: ResolutionRequest) -> ResolutionResult | None:
        if request.canonical_key is None:
            return None
        instrument = await self._repo.get_by_canonical_key(request.canonical_key)
        if instrument is None:
            return None
        return ResolutionResult(
            status=ResolutionStatus.RESOLVED,
            request=request,
            instrument=instrument,
            method=ResolutionMethod.CANONICAL_KEY,
            confidence=1.0,
        )

    async def _by_derived_canonical_key(
        self, request: ResolutionRequest
    ) -> ResolutionResult | None:
        """Build the canonical key from structured fields and look it up directly.

        This is the fast path for a well-specified request and it is exact, so
        it can never be ambiguous.
        """
        if not (request.symbol and request.exchange and request.asset_class):
            return None
        try:
            key = build_canonical_key(
                exchange=request.exchange,
                asset_class=request.asset_class,
                symbol=request.symbol,
                expiry=request.expiry,
                strike=request.strike,
                option_type=request.option_type,
            )
        except CanonicalKeyError:
            return None
        instrument = await self._repo.get_by_canonical_key(key)
        if instrument is None:
            return None
        return ResolutionResult(
            status=ResolutionStatus.RESOLVED,
            request=request,
            instrument=instrument,
            method=ResolutionMethod.CANONICAL_KEY,
            confidence=1.0,
        )

    async def _by_alias(self, request: ResolutionRequest) -> ResolutionResult | None:
        if not (request.source and request.symbol):
            return None
        instrument = await self._repo.find_by_alias(request.source, request.symbol)
        if instrument is None:
            return None
        return ResolutionResult(
            status=ResolutionStatus.RESOLVED,
            request=request,
            instrument=instrument,
            method=ResolutionMethod.ALIAS,
            confidence=1.0,
        )

    async def _by_structured_match(self, request: ResolutionRequest) -> ResolutionResult | None:
        if not request.symbol:
            return None
        candidates = await self._repo.search(
            asset_class=request.asset_class,
            exchange=request.exchange,
            symbol=request.symbol,
            expiry=request.expiry,
            strike=request.strike,
            option_type=request.option_type,
            limit=25,
        )
        if not candidates:
            return None
        if len(candidates) == 1:
            return ResolutionResult(
                status=ResolutionStatus.RESOLVED,
                request=request,
                instrument=candidates[0],
                method=ResolutionMethod.STRUCTURED_MATCH,
                confidence=1.0,
            )
        return ResolutionResult(
            status=ResolutionStatus.AMBIGUOUS,
            request=request,
            reason=ResolutionReason.MULTIPLE_CANDIDATES,
            candidates=tuple(candidates),
        )

    async def _by_symbol(self, request: ResolutionRequest) -> ResolutionResult | None:
        if not request.symbol or request.exchange or request.expiry:
            return None
        candidates = await self._repo.search(
            symbol=request.symbol, asset_class=request.asset_class, limit=25
        )
        if not candidates:
            return None
        if len(candidates) == 1:
            return ResolutionResult(
                status=ResolutionStatus.RESOLVED,
                request=request,
                instrument=candidates[0],
                method=ResolutionMethod.SYMBOL_MATCH,
                # Lower than an exact match: the symbol was unique in this
                # database today, not unique by construction.
                confidence=0.8,
            )
        return ResolutionResult(
            status=ResolutionStatus.AMBIGUOUS,
            request=request,
            reason=ResolutionReason.MULTIPLE_CANDIDATES,
            candidates=tuple(candidates),
        )
