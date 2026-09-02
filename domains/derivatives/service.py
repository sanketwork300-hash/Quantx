"""Chain analysis: forwards, implied volatilities and raw smiles.

The Phase 1 pipeline, in order:

    stored chain snapshot (quality-scored, Phase 0)
        -> group by expiry
        -> time to expiry under a stated policy
        -> forward estimation (three methods, reported side by side)
        -> implied volatility per quote, from the observed mid
        -> OTM-preferred selection
        -> raw smile in (k, w)

Each step reports what it could not do. A slice with no usable forward yields a
slice object with a reason, not a missing entry, because "we could not estimate
a forward for the December expiry" is information the user needs.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import numpy as np

from domains.derivatives.forward import (
    ForwardEstimate,
    ForwardEstimateSet,
    ForwardEstimator,
)
from domains.derivatives.models import (
    ChainAnalysis,
    ImpliedVolPoint,
    PriceSource,
    SmileExclusion,
    SmileSlice,
)
from domains.derivatives.timeconv import ExpiryPolicy, time_to_expiry
from domains.instruments.enums import OptionType
from domains.market_data.curves import YieldCurve
from domains.reports.warnings import AnalyticalWarning
from quant.volatility.implied import implied_vol_black76_batch
from quant.volatility.smile import build_raw_smile

#: Inside this log-moneyness band both sides are near the money and the
#: selection rule below applies; outside it, the out-of-the-money side wins.
ATM_BAND = 0.02
#: A slice needs at least this many usable points before its summary statistics
#: (ATM level, skew, curvature) mean anything.
MIN_SMILE_POINTS = 3

#: Maximum implied-volatility uncertainty, in volatility units, for a quote to
#: carry the smile. Uncertainty here is economic, not numerical: half a spread
#: (or a tick, for a locked market) divided by vega.
#:
#: This threshold does real work. A deep out-of-the-money weekly is worth
#: essentially nothing, and a venue quotes it locked at the tick floor. Inverting
#: that price is numerically perfectly well behaved and yields, say, 50% against
#: a true 12% -- and a dozen such quotes will drag an entire slice. What makes
#: them unusable is not the arithmetic but that a price known only to plus or
#: minus half a tick pins down nothing: one tick divided by their vega is
#: several volatility points.
MAX_SMILE_UNCERTAINTY = 1e-2

ANALYSIS_MODEL_VERSION = "chain-analysis@1.0.0"
IV_MODEL_VERSION = "implied-vol-black76@1.0.0"
FORWARD_MODEL_VERSION = "forward-estimator@1.0.0"


class DerivativesWarningCode:
    NO_USABLE_FORWARD = "DERIVATIVES_NO_USABLE_FORWARD"
    SETTLEMENT_TIME_ASSUMED = "DERIVATIVES_SETTLEMENT_TIME_ASSUMED"
    SETTLEMENT_TIME_UNKNOWN = "DERIVATIVES_SETTLEMENT_TIME_UNKNOWN"
    EXPIRY_IN_THE_PAST = "DERIVATIVES_EXPIRY_IN_THE_PAST"
    FORWARD_DISAGREEMENT = "DERIVATIVES_FORWARD_DISAGREEMENT"
    IV_SOLVE_FAILURES = "DERIVATIVES_IV_SOLVE_FAILURES"
    INSUFFICIENT_SMILE_POINTS = "DERIVATIVES_INSUFFICIENT_SMILE_POINTS"
    NO_UNDERLYING_PRICE = "DERIVATIVES_NO_UNDERLYING_PRICE"
    CURVE_ASSUMED = "DERIVATIVES_CURVE_ASSUMED"


#: Relative disagreement between forward estimators worth telling the user about.
FORWARD_DISAGREEMENT_THRESHOLD = 0.002


@dataclass(frozen=True, slots=True)
class QuoteInput:
    """One option quote, as the analysis needs it.

    Deliberately a plain value object rather than an ORM row: the analysis is
    unit-testable with no database, and the market-data domain's persistence
    stays private to it.
    """

    instrument_id: uuid.UUID
    expiry: date
    strike: Decimal
    option_type: OptionType
    bid_price: Decimal | None
    ask_price: Decimal | None
    last_price: Decimal | None
    spread_score: float = 1.0
    liquidity_score: float = 1.0
    #: The Phase 0 overall quality score. Carried through so the anomaly scanner
    #: can ground its confidence in the same measurement the ingestion pipeline
    #: made, rather than inventing a second notion of a trustworthy quote.
    quality_score: float = 1.0
    #: The venue's minimum price increment. With half the spread it sets how
    #: finely this quote's price is actually known, which is what decides
    #: whether its implied volatility means anything.
    tick_size: Decimal | None = None

    @property
    def mid_price(self) -> Decimal | None:
        if self.bid_price is None or self.ask_price is None:
            return None
        if self.bid_price <= 0 or self.ask_price <= 0:
            return None
        return (self.bid_price + self.ask_price) / Decimal(2)

    @property
    def weight(self) -> float:
        """Calibration weight: spread quality times liquidity.

        Both come from the Phase 0 quality engine, which is the point of having
        scored quality at ingestion — the smile fitter does not need its own,
        inevitably divergent, notion of which quotes are good.
        """
        return float(self.spread_score * self.liquidity_score)

    @property
    def price_resolution(self) -> float | None:
        """How finely the mid is known: half the spread, floored at one tick.

        A market locked at the tick floor has zero spread and a price that could
        be anything from zero to one and a half ticks. Treating that as an exact
        observation is what lets a worthless deep-wing quote imply a 50%
        volatility and drag a whole calibration.
        """
        half_spread = (
            float(self.ask_price - self.bid_price) / 2.0
            if self.ask_price is not None and self.bid_price is not None
            else 0.0
        )
        tick = float(self.tick_size) if self.tick_size is not None else 0.0
        resolution = max(half_spread, tick)
        return resolution if resolution > 0 else None

    @property
    def relative_spread(self) -> float | None:
        mid = self.mid_price
        if mid is None or mid <= 0 or self.bid_price is None or self.ask_price is None:
            return None
        return float((self.ask_price - self.bid_price) / mid)


@dataclass(frozen=True, slots=True)
class ChainAnalysisRequest:
    as_of: datetime
    expiry_policy: ExpiryPolicy
    curve: YieldCurve
    dividend_yield: float = 0.0
    dividend_yield_assumed: bool = True
    underlying_price: Decimal | None = None
    #: (expiry -> observed future price), when the venue lists futures.
    future_prices: dict[date, Decimal] | None = None


class ChainAnalysisService:
    """Pure computation over quote value objects. No I/O, no session."""

    def analyze(
        self,
        snapshot_id: uuid.UUID,
        underlying_id: uuid.UUID,
        quotes: list[QuoteInput],
        request: ChainAnalysisRequest,
    ) -> tuple[ChainAnalysis, list[AnalyticalWarning]]:
        warnings: list[AnalyticalWarning] = []

        if request.curve.source == "assumption":
            warnings.append(
                AnalyticalWarning.info(
                    DerivativesWarningCode.CURVE_ASSUMED,
                    f"Discounting used an assumed flat curve ({request.curve.label}); "
                    "it is a stated assumption, not observed market data.",
                    curve_id=request.curve.curve_id,
                )
            )
        if request.underlying_price is None:
            warnings.append(
                AnalyticalWarning.warn(
                    DerivativesWarningCode.NO_UNDERLYING_PRICE,
                    "No underlying price is available, so the spot-carry forward "
                    "estimator could not run.",
                )
            )
        if request.expiry_policy.settlement_time_utc is None:
            warnings.append(
                AnalyticalWarning.error(
                    DerivativesWarningCode.SETTLEMENT_TIME_UNKNOWN,
                    "No settlement time was supplied, so time to expiry is undefined "
                    "and no implied volatility can be solved.",
                )
            )

        by_expiry: dict[date, list[QuoteInput]] = defaultdict(list)
        for quote in quotes:
            by_expiry[quote.expiry].append(quote)

        slices = [
            self._analyze_expiry(expiry, by_expiry[expiry], request, warnings)
            for expiry in sorted(by_expiry)
        ]

        analysis = ChainAnalysis(
            snapshot_id=snapshot_id,
            underlying_id=underlying_id,
            as_of=request.as_of.isoformat(),
            slices=tuple(slices),
            underlying_price=request.underlying_price,
            curve_id=request.curve.curve_id,
        )
        return analysis, warnings

    # ------------------------------------------------------------ per expiry
    def _analyze_expiry(
        self,
        expiry: date,
        quotes: list[QuoteInput],
        request: ChainAnalysisRequest,
        warnings: list[AnalyticalWarning],
    ) -> SmileSlice:
        tte = time_to_expiry(request.as_of, expiry, request.expiry_policy)

        if tte.years is None or tte.years <= 0:
            if tte.years is not None:
                warnings.append(
                    AnalyticalWarning.warn(
                        DerivativesWarningCode.EXPIRY_IN_THE_PAST,
                        f"Expiry {expiry} is at or before the requested as_of; "
                        f"{len(quotes)} quote(s) were not solved.",
                        expiry=expiry.isoformat(),
                    )
                )
            return SmileSlice(
                expiry=expiry,
                time_to_expiry=tte.years,
                forward=ForwardEstimateSet(estimates=(), selected=None),
                points=tuple(self._unsolvable_points(quotes, tte.reason)),
                settlement_time_assumed=tte.settlement_time_assumed,
                reason=tte.reason,
            )

        tau = tte.years
        if tte.settlement_time_assumed:
            warnings.append(
                AnalyticalWarning.info(
                    DerivativesWarningCode.SETTLEMENT_TIME_ASSUMED,
                    f"Time to expiry for {expiry} used the supplied settlement time "
                    f"of {request.expiry_policy.settlement_time_utc}; the data source "
                    "did not carry an expiry instant.",
                    expiry=expiry.isoformat(),
                )
            )

        forward_set = self._estimate_forward(expiry, quotes, tau, request)
        if forward_set.selected is None:
            warnings.append(
                AnalyticalWarning.error(
                    DerivativesWarningCode.NO_USABLE_FORWARD,
                    f"No forward could be estimated for {expiry}, so no implied "
                    "volatility was solved for its quotes.",
                    expiry=expiry.isoformat(),
                    attempted=[str(e.method) for e in forward_set.estimates],
                )
            )
            return SmileSlice(
                expiry=expiry,
                time_to_expiry=tau,
                forward=forward_set,
                points=tuple(self._unsolvable_points(quotes, "NO_FORWARD")),
                settlement_time_assumed=tte.settlement_time_assumed,
                reason="NO_FORWARD",
            )

        disagreement = forward_set.disagreement
        if disagreement is not None and disagreement > FORWARD_DISAGREEMENT_THRESHOLD:
            warnings.append(
                AnalyticalWarning.warn(
                    DerivativesWarningCode.FORWARD_DISAGREEMENT,
                    f"Forward estimators for {expiry} disagree by "
                    f"{disagreement:.2%}. That usually means an unstated carry or a "
                    "bad quote, and every log-moneyness in this slice depends on it.",
                    expiry=expiry.isoformat(),
                    disagreement=disagreement,
                )
            )

        forward = forward_set.selected.value
        discount = forward_set.selected.discount_factor
        if discount is None:
            discount = request.curve.discount_factor(tau)

        points = self._solve_slice(quotes, forward, discount, tau)
        self._select_for_smile(points, forward)

        failures = {}
        for point in points:
            if point.error:
                failures[point.error] = failures.get(point.error, 0) + 1
        if failures:
            warnings.append(
                AnalyticalWarning.info(
                    DerivativesWarningCode.IV_SOLVE_FAILURES,
                    f"{sum(failures.values())} quote(s) at {expiry} have no implied "
                    "volatility; each carries a named reason.",
                    expiry=expiry.isoformat(),
                    reasons=failures,
                )
            )

        atm, skew, curvature = self._summarise(points, forward, tau)
        if atm is None:
            warnings.append(
                AnalyticalWarning.info(
                    DerivativesWarningCode.INSUFFICIENT_SMILE_POINTS,
                    f"Expiry {expiry} has too few usable points, or none spanning the "
                    "money, for smile statistics.",
                    expiry=expiry.isoformat(),
                    used=sum(1 for p in points if p.used_for_smile),
                )
            )

        return SmileSlice(
            expiry=expiry,
            time_to_expiry=tau,
            forward=forward_set,
            points=tuple(points),
            atm_volatility=atm,
            skew=skew,
            curvature=curvature,
            settlement_time_assumed=tte.settlement_time_assumed,
        )

    # ------------------------------------------------------------- forwards
    def _estimate_forward(
        self,
        expiry: date,
        quotes: list[QuoteInput],
        tau: float,
        request: ChainAnalysisRequest,
    ) -> ForwardEstimateSet:
        estimates: list[ForwardEstimate] = []

        strikes, calls, puts, weights, half_spreads = [], [], [], [], []
        by_strike: dict[Decimal, dict[OptionType, QuoteInput]] = defaultdict(dict)
        for quote in quotes:
            by_strike[quote.strike][quote.option_type] = quote
        for strike, sides in sorted(by_strike.items()):
            call, put = sides.get(OptionType.CALL), sides.get(OptionType.PUT)
            if call is None or put is None:
                continue
            call_mid, put_mid = call.mid_price, put.mid_price
            if call_mid is None or put_mid is None:
                continue
            strikes.append(float(strike))
            calls.append(float(call_mid))
            puts.append(float(put_mid))
            weights.append(min(call.weight, put.weight))
            if call.bid_price is not None and call.ask_price is not None:
                half_spreads.append(float(call.ask_price - call.bid_price) / 2.0)

        scale = float(np.median(half_spreads)) if half_spreads else None
        estimates.append(
            ForwardEstimator.from_put_call_parity(strikes, calls, puts, weights, scale)
        )

        if request.underlying_price is not None:
            estimates.append(
                ForwardEstimator.from_spot_carry(
                    float(request.underlying_price),
                    tau,
                    request.curve.zero_rate(tau),
                    request.dividend_yield,
                    request.dividend_yield_assumed,
                )
            )

        future_price = (request.future_prices or {}).get(expiry)
        if future_price is not None:
            estimates.append(
                ForwardEstimator.from_future(
                    float(future_price), tau, tau, request.curve.discount_factor(tau)
                )
            )

        return ForwardEstimator.select(estimates)

    # ---------------------------------------------------------------- solve
    def _solve_slice(
        self, quotes: list[QuoteInput], forward: float, discount: float, tau: float
    ) -> list[ImpliedVolPoint]:
        strikes = np.array([float(q.strike) for q in quotes])
        is_call = np.array([q.option_type is OptionType.CALL for q in quotes])
        taus = np.full(len(quotes), tau)
        forwards = np.full(len(quotes), forward)
        discounts = np.full(len(quotes), discount)

        prices, sources, spreads = [], [], []
        for quote in quotes:
            spreads.append(
                float(quote.ask_price - quote.bid_price)
                if quote.ask_price is not None and quote.bid_price is not None
                else None
            )
            mid = quote.mid_price
            if mid is not None:
                prices.append(float(mid))
                sources.append(PriceSource.MID)
            elif quote.last_price is not None and quote.last_price > 0:
                # An explicit, recorded fallback. The source is stored so a
                # consumer can see this IV came from a print, not a market.
                prices.append(float(quote.last_price))
                sources.append(PriceSource.LAST)
            else:
                prices.append(float("nan"))
                sources.append(PriceSource.NONE)

        resolutions = np.array([quote.price_resolution or 0.0 for quote in quotes], dtype=float)
        batch = implied_vol_black76_batch(
            np.array(prices),
            forwards,
            strikes,
            taus,
            is_call,
            discounts,
            price_resolution=resolutions,
        )

        sides = (forwards, strikes, taus, is_call, discounts)
        bid_batch = self._solve_side(quotes, "bid_price", *sides)
        ask_batch = self._solve_side(quotes, "ask_price", *sides)

        points: list[ImpliedVolPoint] = []
        for index, quote in enumerate(quotes):
            result = batch.at(index)
            vol = result.implied_volatility
            points.append(
                ImpliedVolPoint(
                    instrument_id=quote.instrument_id,
                    expiry=quote.expiry,
                    strike=quote.strike,
                    option_type=quote.option_type,
                    price_used=None if np.isnan(prices[index]) else prices[index],
                    price_source=sources[index],
                    price_spread=spreads[index],
                    market_iv=vol,
                    market_iv_bid=bid_batch[index],
                    market_iv_ask=ask_batch[index],
                    converged=result.converged,
                    iterations=result.iterations,
                    solver=result.solver or "none",
                    error=str(result.error) if result.error else None,
                    vega=result.vega,
                    uncertainty=result.uncertainty,
                    data_quality_score=quote.quality_score,
                    liquidity_score=quote.liquidity_score,
                    time_to_expiry=tau,
                    log_moneyness=float(np.log(float(quote.strike) / forward)),
                    total_variance=None if vol is None else vol * vol * tau,
                    weight=quote.weight,
                )
            )
        return points

    def _solve_side(
        self,
        quotes: list[QuoteInput],
        attribute: str,
        forwards: np.ndarray,
        strikes: np.ndarray,
        taus: np.ndarray,
        is_call: np.ndarray,
        discounts: np.ndarray,
    ) -> list[float | None]:
        """Implied volatility at one side of the market, for the IV envelope."""
        prices = np.array(
            [
                float(getattr(q, attribute)) if getattr(q, attribute) else float("nan")
                for q in quotes
            ]
        )
        batch = implied_vol_black76_batch(prices, forwards, strikes, taus, is_call, discounts)
        return [None if np.isnan(value) else float(value) for value in batch.implied_volatility]

    # ------------------------------------------------------------ selection
    def _select_for_smile(self, points: list[ImpliedVolPoint], forward: float) -> None:
        """Choose one quote per strike to carry the smile.

        Out-of-the-money quotes win: they carry the time value that determines
        the smile, while an in-the-money quote's price is dominated by intrinsic
        value and its implied volatility is numerically fragile.

        Inside the at-the-money band both sides are informative, so the tie-break
        is the tighter relative spread, and an exact tie goes to the call. Which
        side wins matters less than the rule being deterministic: reproducibility
        requires that the same input always selects the same quote.
        """
        by_strike: dict[Decimal, list[int]] = defaultdict(list)
        for index, point in enumerate(points):
            by_strike[point.strike].append(index)

        ill_conditioned: set[int] = set()
        selected: set[int] = set()
        for strike, indices in by_strike.items():
            solved = [
                index
                for index in indices
                if points[index].market_iv is not None and points[index].converged
            ]
            usable = [
                index
                for index in solved
                if (points[index].uncertainty or 0.0) <= MAX_SMILE_UNCERTAINTY
            ]
            ill_conditioned.update(set(solved) - set(usable))
            if not usable:
                continue

            log_moneyness = float(np.log(float(strike) / forward))
            if abs(log_moneyness) <= ATM_BAND or len(usable) == 1:
                chosen = min(
                    usable,
                    key=lambda i: (
                        points[i].uncertainty
                        if points[i].uncertainty is not None
                        else float("inf"),
                        0 if points[i].option_type is OptionType.CALL else 1,
                    ),
                )
            else:
                otm = OptionType.CALL if log_moneyness > 0 else OptionType.PUT
                matching = [i for i in usable if points[i].option_type is otm]
                chosen = matching[0] if matching else usable[0]
            selected.add(chosen)

        for index, point in enumerate(points):
            if index in selected:
                points[index] = _with(point, used_for_smile=True)
            elif point.market_iv is None:
                points[index] = _with(point, smile_exclusion=SmileExclusion.NO_IMPLIED_VOL)
            elif index in ill_conditioned:
                points[index] = _with(point, smile_exclusion=SmileExclusion.ILL_CONDITIONED)
            else:
                points[index] = _with(point, smile_exclusion=SmileExclusion.NOT_SELECTED_SIDE)

    def _summarise(
        self, points: list[ImpliedVolPoint], forward: float, tau: float
    ) -> tuple[float | None, float | None, float | None]:
        used = [p for p in points if p.used_for_smile and p.market_iv is not None]
        if len(used) < MIN_SMILE_POINTS:
            return None, None, None
        smile = build_raw_smile(
            np.array([float(p.strike) for p in used]),
            np.array([p.market_iv for p in used]),
            forward,
            tau,
            np.array([p.weight for p in used]),
        )
        return smile.atm_volatility(), smile.skew(), smile.curvature()

    @staticmethod
    def _unsolvable_points(quotes: list[QuoteInput], reason: str | None) -> list[ImpliedVolPoint]:
        return [
            ImpliedVolPoint(
                instrument_id=quote.instrument_id,
                expiry=quote.expiry,
                strike=quote.strike,
                option_type=quote.option_type,
                price_used=None,
                price_source=PriceSource.NONE,
                market_iv=None,
                error=reason,
                weight=quote.weight,
                smile_exclusion=SmileExclusion.NO_IMPLIED_VOL,
            )
            for quote in quotes
        ]


def _with(point: ImpliedVolPoint, **changes) -> ImpliedVolPoint:
    from dataclasses import replace

    return replace(point, **changes)
