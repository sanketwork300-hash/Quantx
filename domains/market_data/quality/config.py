"""Quality-engine parameters.

Every threshold the engine uses lives here, is serialised into provenance, and
is therefore reproducible. There are no unnamed constants inside the checks: a
number that changes a result must be visible in the record of that result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from domains.instruments.enums import AssetClass


@dataclass(frozen=True, slots=True)
class AssetClassThresholds:
    #: Age at which a quote's staleness score falls to 0.5.
    stale_half_life_seconds: float
    #: Relative spread at which the spread score falls to 0.5.
    reference_relative_spread: float
    #: Volume/OI/size levels at which the liquidity components score 0.5.
    reference_volume: Decimal
    reference_open_interest: Decimal
    reference_quoted_size: Decimal
    #: Relative move beyond which a jump versus the previous observation is odd.
    extreme_jump_relative: float


DEFAULT_THRESHOLDS: dict[AssetClass, AssetClassThresholds] = {
    AssetClass.OPTION: AssetClassThresholds(
        stale_half_life_seconds=300.0,
        reference_relative_spread=0.02,
        reference_volume=Decimal(1_000),
        reference_open_interest=Decimal(5_000),
        reference_quoted_size=Decimal(50),
        extreme_jump_relative=0.5,
    ),
    AssetClass.FUTURE: AssetClassThresholds(
        stale_half_life_seconds=60.0,
        reference_relative_spread=0.0005,
        reference_volume=Decimal(10_000),
        reference_open_interest=Decimal(50_000),
        reference_quoted_size=Decimal(100),
        extreme_jump_relative=0.15,
    ),
    AssetClass.EQUITY: AssetClassThresholds(
        stale_half_life_seconds=60.0,
        reference_relative_spread=0.001,
        reference_volume=Decimal(100_000),
        reference_open_interest=Decimal(1),
        reference_quoted_size=Decimal(500),
        extreme_jump_relative=0.20,
    ),
    AssetClass.INDEX: AssetClassThresholds(
        stale_half_life_seconds=60.0,
        reference_relative_spread=0.001,
        reference_volume=Decimal(1),
        reference_open_interest=Decimal(1),
        reference_quoted_size=Decimal(1),
        extreme_jump_relative=0.15,
    ),
    AssetClass.CRYPTO_SPOT: AssetClassThresholds(
        stale_half_life_seconds=15.0,
        reference_relative_spread=0.0005,
        reference_volume=Decimal(100),
        reference_open_interest=Decimal(1),
        reference_quoted_size=Decimal(1),
        extreme_jump_relative=0.10,
    ),
    AssetClass.CRYPTO_PERPETUAL: AssetClassThresholds(
        stale_half_life_seconds=15.0,
        reference_relative_spread=0.0005,
        reference_volume=Decimal(100),
        reference_open_interest=Decimal(1_000),
        reference_quoted_size=Decimal(1),
        extreme_jump_relative=0.10,
    ),
    AssetClass.FX: AssetClassThresholds(
        stale_half_life_seconds=30.0,
        reference_relative_spread=0.0002,
        reference_volume=Decimal(1),
        reference_open_interest=Decimal(1),
        reference_quoted_size=Decimal(1_000_000),
        extreme_jump_relative=0.05,
    ),
}

_FALLBACK = DEFAULT_THRESHOLDS[AssetClass.EQUITY]


@dataclass(frozen=True, slots=True)
class MarketDataQualityConfig:
    thresholds: dict[AssetClass, AssetClassThresholds] = field(
        default_factory=lambda: dict(DEFAULT_THRESHOLDS)
    )

    # ------------------------------------------------------- score weighting
    weight_stale: float = 1.0
    weight_spread: float = 1.0
    weight_liquidity: float = 1.0
    weight_consistency: float = 1.5
    weight_completeness: float = 0.5

    # ------------------------------------- liquidity component weighting
    liquidity_weight_volume: float = 0.4
    liquidity_weight_open_interest: float = 0.4
    liquidity_weight_quoted_size: float = 0.2

    # ------------------------------------------------- consistency penalties
    penalty_crossed_market: float = 0.0
    penalty_locked_market: float = 0.6
    penalty_inconsistent_timestamps: float = 0.8
    penalty_future_timestamp: float = 0.0
    penalty_duplicate: float = 0.7
    penalty_extreme_jump: float = 0.4
    penalty_bound_violation: float = 0.2
    penalty_negative_price: float = 0.0

    # -------------------------------------------------------- spread flagging
    #: Relative spread above which WIDE_SPREAD is raised at WARNING severity.
    wide_spread_warning_multiple: float = 3.0
    wide_spread_info_multiple: float = 1.5

    # ------------------------------------------------------ staleness flagging
    stale_warning_multiple: float = 2.0
    stale_error_multiple: float = 10.0

    # ---------------------------------------------------------- bound checks
    #: Carry assumptions for option no-arbitrage bounds. ``None`` means
    #: *unknown*, which is the honest default: Phase 0 has no yield curve, and
    #: inventing r=0 makes every legitimate deep in-the-money European put look
    #: sub-intrinsic. When carry is unknown only the assumption-free bounds are
    #: applied (C <= S, P <= K, price >= 0), because those hold for any
    #: r, q >= 0. Supply both to enable the tighter carry-dependent bounds; the
    #: values are then recorded in provenance and echoed on every flag they
    #: influenced. The full arbitrage suite arrives in Phase 2 with a real curve.
    assumed_risk_free_rate: float | None = None
    assumed_dividend_yield: float | None = None
    #: A bound violation smaller than max(1 tick, this multiple of the quoted
    #: spread) is INFO, not ERROR. On a discrete strike grid with wide markets,
    #: tiny violations are ubiquitous and not exploitable; treating them as
    #: errors would exclude most of a real illiquid chain.
    bound_violation_spread_multiple: float = 1.0
    bound_violation_error_spread_multiple: float = 3.0

    # ----------------------------------------------------------- liquidity
    illiquid_volume_floor: Decimal = Decimal(0)
    illiquid_open_interest_floor: Decimal = Decimal(0)

    @property
    def carry_is_known(self) -> bool:
        return self.assumed_risk_free_rate is not None and self.assumed_dividend_yield is not None

    def for_asset_class(self, asset_class: AssetClass) -> AssetClassThresholds:
        return self.thresholds.get(asset_class, _FALLBACK)

    def to_provenance(self) -> dict:
        """Serialisable record of every parameter that influenced a score."""
        payload = asdict(self)
        payload["thresholds"] = {
            str(asset_class): {
                key: (format(value, "f") if isinstance(value, Decimal) else value)
                for key, value in asdict(thresholds).items()
            }
            for asset_class, thresholds in self.thresholds.items()
        }
        for key, value in list(payload.items()):
            if isinstance(value, Decimal):
                payload[key] = format(value, "f")
        return payload
