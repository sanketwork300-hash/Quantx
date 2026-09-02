"""Cross-engine composition.

``domains.reports`` is the only domain permitted to fan out across the engines,
and it may only *compose* their results (docs/architecture.md section 4). This
module assembles a ``MarketState`` from the market-data half and the derivatives
half without either domain importing the other.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from domains.derivatives.application import DerivativesService
from domains.market_data.market_state import MarketState
from domains.market_data.service import MarketDataService


class MarketStateComposer:
    def __init__(self, market_data: MarketDataService, derivatives: DerivativesService) -> None:
        self._market_data = market_data
        self._derivatives = derivatives

    async def build(
        self,
        user_id: uuid.UUID,
        underlying_id: uuid.UUID,
        as_of: datetime | None = None,
        risk_free_rate: float | None = None,
        include_surface: bool = True,
    ) -> MarketState | None:
        builder = await self._market_data.build_market_state(
            user_id, underlying_id, as_of=as_of, risk_free_rate=risk_free_rate
        )
        if builder is None:
            return None

        if include_surface:
            loaded = await self._derivatives.latest_surface(user_id, underlying_id)
            if loaded is not None:
                row, surface = loaded
                builder.add_surface(underlying_id, surface.model, surface.surface_id)
                builder.add_source(f"surface:{row.model_version}", surface.surface_id)
        return builder.build()
