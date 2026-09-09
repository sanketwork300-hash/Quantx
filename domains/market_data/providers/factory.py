"""Choosing a market-data provider.

One place decides, so that no other module has to know which providers exist.
The rule the selection enforces is build spec 48.4: **synthetic data is never
used in production unless it was explicitly asked for.** A deployment configured
for a live provider that cannot be built fails loudly rather than falling back
to a market that does not exist — a fallback that would produce plausible prices
for instruments nobody is trading.

Development is different, and the difference is stated rather than inferred: the
fallback happens only when the settings ask for it.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from domains.market_data.providers.base import MarketDataProvider, ProviderError
from domains.market_data.providers.csv_provider import CSVMarketDataProvider
from domains.market_data.providers.synthetic import (
    SyntheticMarketConfig,
    SyntheticMarketDataProvider,
)
from domains.market_data.providers.upstox import (
    AccessTokenSource,
    InstrumentDirectory,
    UpstoxEndpoints,
    UpstoxMarketDataProvider,
)


class ProviderKind(StrEnum):
    SYNTHETIC = "synthetic"
    CSV = "csv"
    UPSTOX = "upstox"


class ProviderNotAvailable(ProviderError):
    """The configured provider cannot be built here.

    Deliberately fatal. The alternative — quietly using the synthetic market —
    means a production deployment serving invented prices with no error anywhere,
    which is the failure this exception exists to make impossible.
    """

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(
            f"market data provider {kind!r} is configured but cannot be built: {reason}"
        )
        self.kind = kind
        self.reason = reason


def build_provider(
    kind: ProviderKind | str,
    *,
    directory: InstrumentDirectory | None = None,
    token_source: AccessTokenSource | None = None,
    endpoints: UpstoxEndpoints | None = None,
    csv_root: Path | None = None,
    synthetic_config: SyntheticMarketConfig | None = None,
    production_like: bool = False,
) -> MarketDataProvider:
    """Build the configured provider, or refuse and say why.

    ``production_like`` gates the synthetic market. The check lives here rather
    than at startup because a deployment that only analyses uploaded files never
    builds a provider at all, and refusing to start such a deployment would be
    refusing something legitimate. What must never happen is the synthetic
    market being *served* in production, and that happens here.
    """
    selected = ProviderKind(str(kind).lower())

    if selected is ProviderKind.SYNTHETIC and production_like:
        raise ProviderNotAvailable(
            str(selected),
            "the synthetic market generates prices that describe nothing real; it is a "
            "development and test fixture and will not be served in a production-like "
            "environment",
        )

    if selected is ProviderKind.UPSTOX:
        if directory is None or token_source is None:
            raise ProviderNotAvailable(
                str(selected),
                "an instrument directory and a credential source are both required; the "
                "credential comes from the user's broker connection, not from configuration",
            )
        return UpstoxMarketDataProvider(
            directory=directory, token_source=token_source, endpoints=endpoints
        )

    if selected is ProviderKind.CSV:
        if csv_root is None:
            raise ProviderNotAvailable(str(selected), "QIP_MARKET_DATA_CSV_ROOT is not set")
        return CSVMarketDataProvider(csv_root)

    if synthetic_config is None:
        raise ProviderNotAvailable(
            str(selected), "the synthetic market needs an as-of timestamp to generate from"
        )
    return SyntheticMarketDataProvider(synthetic_config)
