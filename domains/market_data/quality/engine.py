"""Market-data quality engine.

Answers one question for every downstream model: *how much should this be
trusted?* It classifies; it never deletes. The decision to exclude a quote
belongs to the ingestion pipeline, which applies a configured severity
threshold and records the reason (build spec 12).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from domains.instruments.enums import AssetClass, OptionType
from domains.market_data.models import OptionQuote, Quote
from domains.market_data.quality.config import MarketDataQualityConfig
from domains.market_data.quality.flags import (
    MarketDataQuality,
    QualityCode,
    QualityFlag,
    Severity,
)
from quant.numerical.tolerances import clamp
from quant.statistics.scoring import (
    exponential_decay_score,
    ratio_penalty_score,
    saturating_score,
    weighted_geometric_mean,
)

#: Fields each asset class is expected to publish, used for completeness.
EXPECTED_FIELDS: dict[AssetClass, tuple[str, ...]] = {
    AssetClass.OPTION: ("bid_price", "ask_price", "last_price", "volume", "open_interest"),
    AssetClass.FUTURE: ("bid_price", "ask_price", "last_price", "volume", "open_interest"),
    AssetClass.CRYPTO_PERPETUAL: (
        "bid_price",
        "ask_price",
        "last_price",
        "volume",
        "open_interest",
    ),
    AssetClass.EQUITY: ("bid_price", "ask_price", "last_price", "volume"),
    AssetClass.INDEX: ("last_price",),
    AssetClass.CRYPTO_SPOT: ("bid_price", "ask_price", "last_price", "volume"),
    AssetClass.FX: ("bid_price", "ask_price"),
}


@dataclass(frozen=True, slots=True)
class QuoteContext:
    """Everything the engine needs beyond the quote itself."""

    asset_class: AssetClass
    as_of: datetime
    tick_size: Decimal = Decimal("0.01")
    previous_price: Decimal | None = None
    is_duplicate: bool = False
    provider_supplies: tuple[str, ...] | None = None
    multiplier_assumed: bool = False


class MarketDataQualityEngine:
    def __init__(self, config: MarketDataQualityConfig | None = None) -> None:
        self.config = config or MarketDataQualityConfig()

    # ------------------------------------------------------------ public API
    def score_quote(self, quote: Quote, context: QuoteContext) -> MarketDataQuality:
        flags: list[QualityFlag] = []
        thresholds = self.config.for_asset_class(context.asset_class)

        completeness = self._completeness(quote, context, flags)
        stale = self._staleness(quote, context, thresholds, flags)
        spread = self._spread(quote, thresholds, flags)
        liquidity = self._liquidity(quote, thresholds, flags)
        consistency = self._consistency(quote, context, thresholds, flags)

        if context.multiplier_assumed:
            flags.append(
                QualityFlag(
                    QualityCode.MULTIPLIER_ASSUMED,
                    Severity.INFO,
                    "Contract multiplier was not supplied by the data source.",
                )
            )

        return self._assemble(stale, spread, liquidity, consistency, completeness, flags)

    def score_option_quote(
        self, option_quote: OptionQuote, context: QuoteContext
    ) -> MarketDataQuality:
        base = self.score_quote(option_quote.quote, context)
        flags = list(base.flags)

        consistency_multiplier = 1.0
        consistency_multiplier *= self._option_structure(option_quote, flags)
        consistency_multiplier *= self._option_bounds(option_quote, context, flags)

        if option_quote.underlying_price is None:
            flags.append(
                QualityFlag(
                    QualityCode.MISSING_UNDERLYING_PRICE,
                    Severity.WARNING,
                    "No underlying price accompanies this option quote; "
                    "no-arbitrage bound checks were skipped.",
                )
            )

        consistency = clamp(base.consistency_score * consistency_multiplier, 0.0, 1.0)
        return self._assemble(
            base.stale_score,
            base.spread_score,
            base.liquidity_score,
            consistency,
            base.completeness_score,
            flags,
        )

    # -------------------------------------------------------------- sections
    def _completeness(self, quote: Quote, context: QuoteContext, flags: list[QualityFlag]) -> float:
        expected = context.provider_supplies or EXPECTED_FIELDS.get(
            context.asset_class, ("bid_price", "ask_price", "last_price", "volume")
        )
        present = sum(1 for name in expected if getattr(quote, name, None) is not None)

        has_bid = quote.bid_price is not None
        has_ask = quote.ask_price is not None
        if not has_bid and not has_ask:
            flags.append(
                QualityFlag(
                    QualityCode.MISSING_BOTH_SIDES,
                    Severity.ERROR,
                    "Neither bid nor ask is present; no market price can be derived.",
                )
            )
        else:
            if "bid_price" in expected and not has_bid:
                flags.append(
                    QualityFlag(QualityCode.MISSING_BID, Severity.WARNING, "Bid is absent.")
                )
            if "ask_price" in expected and not has_ask:
                flags.append(
                    QualityFlag(QualityCode.MISSING_ASK, Severity.WARNING, "Ask is absent.")
                )

        if "open_interest" in expected and quote.open_interest is None:
            flags.append(
                QualityFlag(
                    QualityCode.MISSING_OPEN_INTEREST, Severity.INFO, "Open interest is absent."
                )
            )
        if "volume" in expected and quote.volume is None:
            flags.append(
                QualityFlag(QualityCode.MISSING_VOLUME, Severity.INFO, "Volume is absent.")
            )
        if quote.bid_size is None and quote.ask_size is None:
            flags.append(
                QualityFlag(
                    QualityCode.MISSING_DEPTH,
                    Severity.INFO,
                    "No quoted sizes; liquidity scoring uses volume and open interest only.",
                )
            )
        if quote.sequence_number is None:
            flags.append(
                QualityFlag(
                    QualityCode.MISSING_SEQUENCE,
                    Severity.INFO,
                    "No sequence number; gap and duplicate detection is limited.",
                )
            )

        return present / len(expected) if expected else 1.0

    def _staleness(
        self, quote: Quote, context: QuoteContext, thresholds, flags: list[QualityFlag]
    ) -> float:
        age = quote.age_seconds(context.as_of)
        if age < 0:
            # Handled as a consistency violation; staleness itself is not the
            # problem with a quote timestamped in the future.
            return 1.0

        half_life = thresholds.stale_half_life_seconds
        if age >= half_life * self.config.stale_error_multiple:
            severity = Severity.ERROR
        elif age >= half_life * self.config.stale_warning_multiple:
            severity = Severity.WARNING
        else:
            severity = None

        if severity is not None:
            flags.append(
                QualityFlag(
                    QualityCode.STALE_QUOTE,
                    severity,
                    f"Quote is {age:.0f}s old at the requested as_of (half-life {half_life:.0f}s).",
                    {"age_seconds": age, "half_life_seconds": half_life},
                )
            )
        return exponential_decay_score(age, half_life)

    def _spread(self, quote: Quote, thresholds, flags: list[QualityFlag]) -> float:
        relative = quote.relative_spread
        if relative is None:
            # No usable two-sided market: the spread dimension carries no
            # information and scores zero rather than being quietly skipped.
            return 0.0

        relative_float = float(relative)
        reference = thresholds.reference_relative_spread
        if relative_float >= reference * self.config.wide_spread_warning_multiple:
            severity = Severity.WARNING
        elif relative_float >= reference * self.config.wide_spread_info_multiple:
            severity = Severity.INFO
        else:
            severity = None

        if severity is not None:
            flags.append(
                QualityFlag(
                    QualityCode.WIDE_SPREAD,
                    severity,
                    f"Relative spread {relative_float:.4%} against a reference of {reference:.4%}.",
                    {"relative_spread": relative_float, "reference": reference},
                )
            )
        return ratio_penalty_score(relative_float, reference)

    def _liquidity(self, quote: Quote, thresholds, flags: list[QualityFlag]) -> float:
        config = self.config
        volume = float(quote.volume or 0)
        open_interest = float(quote.open_interest or 0)
        quoted_size = 0.0
        if quote.bid_size is not None and quote.ask_size is not None:
            quoted_size = float(min(quote.bid_size, quote.ask_size))
        elif quote.bid_size is not None or quote.ask_size is not None:
            quoted_size = float(quote.bid_size or quote.ask_size or 0)

        score = (
            config.liquidity_weight_volume
            * saturating_score(volume, float(thresholds.reference_volume))
            + config.liquidity_weight_open_interest
            * saturating_score(open_interest, float(thresholds.reference_open_interest))
            + config.liquidity_weight_quoted_size
            * saturating_score(quoted_size, float(thresholds.reference_quoted_size))
        )
        total_weight = (
            config.liquidity_weight_volume
            + config.liquidity_weight_open_interest
            + config.liquidity_weight_quoted_size
        )
        score = score / total_weight if total_weight else 0.0

        volume_dead = quote.volume is not None and quote.volume <= config.illiquid_volume_floor
        oi_dead = (
            quote.open_interest is not None
            and quote.open_interest <= config.illiquid_open_interest_floor
        )
        if volume_dead and oi_dead:
            flags.append(
                QualityFlag(
                    QualityCode.ILLIQUID_CONTRACT,
                    Severity.WARNING,
                    "Volume and open interest are both at or below the floor; "
                    "this contract shows no trading interest.",
                    {"volume": float(quote.volume or 0), "open_interest": open_interest},
                )
            )
        if quoted_size == 0 and quote.has_two_sided_market:
            flags.append(
                QualityFlag(
                    QualityCode.NO_QUOTED_SIZE,
                    Severity.INFO,
                    "A two-sided market is quoted with no size.",
                )
            )
        return clamp(score, 0.0, 1.0)

    def _consistency(
        self, quote: Quote, context: QuoteContext, thresholds, flags: list[QualityFlag]
    ) -> float:
        config = self.config
        score = 1.0

        for name in ("bid_price", "ask_price", "last_price"):
            value = getattr(quote, name)
            if value is not None and value < 0:
                flags.append(
                    QualityFlag(
                        QualityCode.NEGATIVE_PRICE,
                        Severity.ERROR,
                        f"{name} is negative ({value}).",
                        {"field": name, "value": format(value, "f")},
                    )
                )
                score *= config.penalty_negative_price

        for name in ("bid_size", "ask_size", "last_size", "volume", "open_interest"):
            value = getattr(quote, name)
            if value is not None and value < 0:
                flags.append(
                    QualityFlag(
                        QualityCode.NEGATIVE_SIZE,
                        Severity.ERROR,
                        f"{name} is negative ({value}).",
                        {"field": name},
                    )
                )
                score *= config.penalty_negative_price

        if quote.bid_price is not None and quote.bid_price == 0:
            flags.append(
                QualityFlag(
                    QualityCode.ZERO_BID,
                    Severity.WARNING,
                    "Bid is zero; no one is bidding for this contract.",
                )
            )
        if quote.ask_price is not None and quote.ask_price == 0:
            flags.append(
                QualityFlag(
                    QualityCode.ZERO_ASK,
                    Severity.ERROR,
                    "Ask is zero, which is not a sellable market.",
                )
            )
            score *= config.penalty_negative_price

        if quote.is_crossed:
            flags.append(
                QualityFlag(
                    QualityCode.CROSSED_MARKET,
                    Severity.ERROR,
                    f"Bid {quote.bid_price} exceeds ask {quote.ask_price}.",
                    {
                        "bid": format(quote.bid_price, "f"),
                        "ask": format(quote.ask_price, "f"),
                    },
                )
            )
            score *= config.penalty_crossed_market
        elif quote.is_locked:
            flags.append(
                QualityFlag(
                    QualityCode.LOCKED_MARKET,
                    Severity.WARNING,
                    f"Bid equals ask at {quote.bid_price}.",
                )
            )
            score *= config.penalty_locked_market

        if quote.receive_timestamp < quote.exchange_timestamp:
            flags.append(
                QualityFlag(
                    QualityCode.INCONSISTENT_TIMESTAMPS,
                    Severity.WARNING,
                    "Receive timestamp precedes the exchange timestamp.",
                )
            )
            score *= config.penalty_inconsistent_timestamps

        if quote.exchange_timestamp > context.as_of:
            flags.append(
                QualityFlag(
                    QualityCode.FUTURE_TIMESTAMP,
                    Severity.ERROR,
                    "Exchange timestamp is after the requested as_of; this "
                    "observation cannot belong to the snapshot.",
                    {
                        "exchange_timestamp": quote.exchange_timestamp.isoformat(),
                        "as_of": context.as_of.isoformat(),
                    },
                )
            )
            score *= config.penalty_future_timestamp

        if context.is_duplicate:
            flags.append(
                QualityFlag(
                    QualityCode.DUPLICATE_OBSERVATION,
                    Severity.ERROR,
                    "A quote for this contract already appears in this snapshot.",
                )
            )
            score *= config.penalty_duplicate

        score *= self._jump(quote, context, thresholds, flags)
        return clamp(score, 0.0, 1.0)

    def _jump(
        self, quote: Quote, context: QuoteContext, thresholds, flags: list[QualityFlag]
    ) -> float:
        previous = context.previous_price
        if previous is None or previous <= 0:
            return 1.0
        current = quote.mid_price or quote.last_price
        if current is None:
            return 1.0
        move = abs(float(current) / float(previous) - 1.0)
        if move <= thresholds.extreme_jump_relative:
            return 1.0
        flags.append(
            QualityFlag(
                QualityCode.EXTREME_PRICE_JUMP,
                Severity.WARNING,
                f"Price moved {move:.2%} from the previous accepted observation "
                f"(threshold {thresholds.extreme_jump_relative:.2%}).",
                {"move": move, "previous": format(previous, "f")},
            )
        )
        return self.config.penalty_extreme_jump

    # -------------------------------------------------------- option checks
    def _option_structure(self, option_quote: OptionQuote, flags: list[QualityFlag]) -> float:
        multiplier = 1.0
        if option_quote.strike <= 0:
            flags.append(
                QualityFlag(
                    QualityCode.INVALID_STRIKE,
                    Severity.ERROR,
                    f"Strike must be strictly positive, got {option_quote.strike}.",
                )
            )
            multiplier *= 0.0
        return multiplier

    def _option_bounds(
        self, option_quote: OptionQuote, context: QuoteContext, flags: list[QualityFlag]
    ) -> float:
        """Static no-arbitrage bounds on the option's mid price.

        Two tiers, deliberately:

        * **Assumption-free** (always applied): ``C <= S`` and ``P <= K``. These
          hold for every ``r, q >= 0``, so they need no curve.
        * **Carry-dependent** (only when the config supplies both ``r`` and
          ``q``): the tighter discounted bounds, including the lower bounds.

        The lower bounds are gated because they are *not* assumption-free. A
        European put legitimately trades below ``K - S`` when rates are
        positive, so applying an invented ``r = 0`` would flag every deep
        in-the-money put in a real chain as sub-intrinsic. When carry is
        unknown the check is skipped and the pipeline says so, rather than
        producing a confident wrong answer.

        Severity is driven by violation size relative to the quoted spread: on a
        discrete strike grid with wide markets, violations smaller than the
        spread are ubiquitous and not exploitable, and treating them as errors
        would exclude most of a real illiquid chain.
        """
        underlying = option_quote.underlying_price
        if underlying is None:
            return 1.0

        price = option_quote.mid_price or option_quote.quote.last_price
        if price is None:
            return 1.0

        time_to_expiry = option_quote.time_to_expiry_years(context.as_of)
        if time_to_expiry is None:
            flags.append(
                QualityFlag(
                    QualityCode.UNKNOWN_EXPIRY_TIME,
                    Severity.INFO,
                    "Expiry instant unknown; bounds computed with zero time value "
                    "of the discount factors.",
                )
            )
            t = 0.0
        elif time_to_expiry <= 0:
            flags.append(
                QualityFlag(
                    QualityCode.OPTION_EXPIRED,
                    Severity.ERROR,
                    "Time to expiry is not positive at the requested as_of.",
                    {"time_to_expiry_years": float(time_to_expiry)},
                )
            )
            return 0.0
        else:
            t = float(time_to_expiry)

        # The carry assumption is reported once per chain by the ingestion
        # pipeline and recorded in provenance, not repeated on every quote; it
        # is echoed into the context of any bound violation it influenced.
        carry_known = self.config.carry_is_known
        rate = self.config.assumed_risk_free_rate or 0.0
        dividend = self.config.assumed_dividend_yield or 0.0
        assumption = (
            {"assumed_risk_free_rate": rate, "assumed_dividend_yield": dividend}
            if carry_known
            else {"carry_assumption": "unknown"}
        )

        spot = float(underlying) * math.exp(-dividend * t)
        strike = float(option_quote.strike) * math.exp(-rate * t)
        if option_quote.option_type is OptionType.CALL:
            lower, upper = max(spot - strike, 0.0), spot
        else:
            lower, upper = max(strike - spot, 0.0), strike
        if not carry_known:
            # Only the assumption-free upper bounds survive without a curve.
            lower = 0.0

        price_float = float(price)
        spread = option_quote.quote.spread
        spread_float = float(spread) if spread is not None and spread > 0 else 0.0
        tick = float(context.tick_size)

        if price_float < lower:
            return self._bound_flag(
                QualityCode.PRICE_BELOW_INTRINSIC,
                lower - price_float,
                spread_float,
                tick,
                f"Mid {price_float:.4f} is below the no-arbitrage lower bound {lower:.4f}.",
                {"bound": lower, "price": price_float, **assumption},
                flags,
            )
        if price_float > upper:
            return self._bound_flag(
                QualityCode.PRICE_ABOVE_BOUND,
                price_float - upper,
                spread_float,
                tick,
                f"Mid {price_float:.4f} is above the no-arbitrage upper bound {upper:.4f}.",
                {"bound": upper, "price": price_float, **assumption},
                flags,
            )
        return 1.0

    def _bound_flag(
        self,
        code: QualityCode,
        magnitude: float,
        spread: float,
        tick: float,
        message: str,
        context: dict,
        flags: list[QualityFlag],
    ) -> float:
        config = self.config
        info_tolerance = max(tick, spread * config.bound_violation_spread_multiple)
        error_tolerance = max(tick, spread * config.bound_violation_error_spread_multiple)

        if magnitude <= info_tolerance:
            severity = Severity.INFO
            penalty = 1.0
        elif magnitude <= error_tolerance:
            severity = Severity.WARNING
            penalty = 0.6
        else:
            severity = Severity.ERROR
            penalty = config.penalty_bound_violation

        flags.append(
            QualityFlag(
                code,
                severity,
                f"{message} Violation magnitude {magnitude:.4f} against a "
                f"tolerance of {info_tolerance:.4f}.",
                {**context, "magnitude": magnitude, "tolerance": info_tolerance},
            )
        )
        return penalty

    # -------------------------------------------------------------- assembly
    def _assemble(
        self,
        stale: float,
        spread: float,
        liquidity: float,
        consistency: float,
        completeness: float,
        flags: list[QualityFlag],
    ) -> MarketDataQuality:
        config = self.config
        scores = [stale, spread, liquidity, consistency, completeness]
        weights = [
            config.weight_stale,
            config.weight_spread,
            config.weight_liquidity,
            config.weight_consistency,
            config.weight_completeness,
        ]
        overall = weighted_geometric_mean([clamp(s, 0.0, 1.0) for s in scores], weights)
        return MarketDataQuality(
            stale_score=clamp(stale, 0.0, 1.0),
            spread_score=clamp(spread, 0.0, 1.0),
            liquidity_score=clamp(liquidity, 0.0, 1.0),
            consistency_score=clamp(consistency, 0.0, 1.0),
            completeness_score=clamp(completeness, 0.0, 1.0),
            overall_score=overall,
            flags=tuple(flags),
        )
