"""Derivatives application service: the database-facing wrapper.

Keeps the numerical pipeline (``ChainAnalysisService``) free of I/O so it stays
unit-testable without a database, and keeps cross-domain access going through
``MarketDataService`` rather than reaching into another domain's repository.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from domains.derivatives.anomaly import (
    ANOMALY_MODEL_VERSION,
    AnomalyPolicy,
    SurfaceAnomalyScanner,
)
from domains.derivatives.arbitrage import ArbitrageScope
from domains.derivatives.calibration import (
    CALIBRATION_MODEL_VERSION,
    SurfaceCalibrationRequest,
    SurfaceCalibrationService,
)
from domains.derivatives.characteristics import surface_term_structure
from domains.derivatives.forward import (
    ForwardEstimate,
    ForwardEstimateSet,
    ForwardEstimator,
    ForwardFailure,
    ForwardMethod,
)
from domains.derivatives.history import TenorHistory, build_tenor_history
from domains.derivatives.models import (
    ChainAnalysis,
    ImpliedVolPoint,
    PriceSource,
    SmileExclusion,
    SmileSlice,
)
from domains.derivatives.repository import DerivativesRepository
from domains.derivatives.service import (
    ANALYSIS_MODEL_VERSION,
    FORWARD_MODEL_VERSION,
    IV_MODEL_VERSION,
    ChainAnalysisRequest,
    ChainAnalysisService,
    DerivativesWarningCode,
    QuoteInput,
)
from domains.derivatives.surface import (
    SURFACE_MODEL,
    SURFACE_MODEL_VERSION,
    ReferencePoint,
    SurfaceSliceFit,
    VolatilitySurface,
)
from domains.derivatives.timeconv import ExpiryPolicy
from domains.instruments.enums import AssetClass, OptionType
from domains.instruments.service import InstrumentService
from domains.market_data.curves import YieldCurve
from domains.market_data.service import MarketDataService
from domains.reports.envelope import AnalyticalResult
from domains.reports.provenance import Provenance
from domains.reports.warnings import AnalyticalWarning
from infrastructure.settings import Settings
from quant.daycount import DEFAULT_DAY_COUNT, DayCount
from quant.pricing.black_scholes import bsm_greeks
from quant.pricing.greeks import GREEK_UNITS
from quant.volatility.implied import implied_vol_black76, implied_vol_bsm
from quant.volatility.svi import SVIParameters
from quant.volatility.svi_calibration import CalibrationStatus, SVICalibrationResult


class AnalysisError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CalibrateSurfaceParams:
    seed: int = 20_260_924
    use_weights: bool = True


@dataclass(frozen=True, slots=True)
class AnalyzeChainParams:
    """What the caller must decide before a chain can be analysed."""

    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0
    dividend_yield_assumed: bool = True
    settlement_time_utc: time | None = None
    day_count: DayCount = DEFAULT_DAY_COUNT
    include_excluded_quotes: bool = False
    underlying_price: Decimal | None = None


class DerivativesService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self.repository = DerivativesRepository(session)
        self.instruments = InstrumentService(session)

    # --------------------------------------------------------- chain analysis
    async def analyze_chain(
        self,
        user_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        params: AnalyzeChainParams,
        market_data: MarketDataService,
    ) -> AnalyticalResult[ChainAnalysis]:
        snapshot = await market_data.get_chain_snapshot(snapshot_id, user_id)
        if snapshot is None:
            raise AnalysisError(f"chain snapshot {snapshot_id} not found")

        rows = await market_data.get_chain_quotes(
            snapshot_id, include_excluded=params.include_excluded_quotes
        )
        # The venue's tick is what sets how finely a quote's price is known, and
        # therefore whether its implied volatility carries information.
        tick_sizes = {
            instrument.id: instrument.tick_size
            for instrument in await self.instruments.search(
                underlying_id=snapshot.underlying_id,
                asset_class=AssetClass.OPTION,
                limit=100_000,
            )
        }
        quotes = [
            QuoteInput(
                instrument_id=row.instrument_id,
                expiry=row.expiry,
                strike=row.strike,
                option_type=OptionType(row.option_type),
                bid_price=row.bid_price,
                ask_price=row.ask_price,
                last_price=row.last_price,
                spread_score=row.spread_score,
                liquidity_score=row.liquidity_score,
                quality_score=row.overall_score,
                tick_size=tick_sizes.get(row.instrument_id),
            )
            for row in rows
        ]

        curve = YieldCurve.flat(
            params.risk_free_rate,
            snapshot.as_of_timestamp,
            "INR",
            source="assumption",
            day_count=params.day_count,
        )
        request = ChainAnalysisRequest(
            as_of=snapshot.as_of_timestamp,
            expiry_policy=ExpiryPolicy(
                settlement_time_utc=params.settlement_time_utc, day_count=params.day_count
            ),
            curve=curve,
            dividend_yield=params.dividend_yield,
            dividend_yield_assumed=params.dividend_yield_assumed,
            underlying_price=params.underlying_price or snapshot.underlying_price,
        )

        analysis, warnings = ChainAnalysisService().analyze(
            snapshot_id, snapshot.underlying_id, quotes, request
        )

        if not quotes:
            warnings.append(
                AnalyticalWarning.error(
                    DerivativesWarningCode.IV_SOLVE_FAILURES,
                    "The snapshot contains no usable quotes to analyse.",
                )
            )

        provenance = self._provenance(snapshot, curve, params, request)
        await self.repository.upsert_curve(curve)
        row = await self.repository.create_analysis(
            user_id=user_id,
            chain_snapshot_id=snapshot_id,
            underlying_id=snapshot.underlying_id,
            as_of_timestamp=snapshot.as_of_timestamp,
            curve_id=curve.curve_id,
            day_count=str(params.day_count),
            settlement_time_utc=(
                params.settlement_time_utc.isoformat() if params.settlement_time_utc else None
            ),
            underlying_price=request.underlying_price,
            quotes_in=analysis.total_quotes,
            quotes_solved=analysis.total_solved,
            expiries=len(analysis.slices),
            summary=analysis.to_dict(include_points=False),
            provenance=provenance.to_dict(),
        )
        stored = ChainAnalysis(
            snapshot_id=analysis.snapshot_id,
            underlying_id=analysis.underlying_id,
            as_of=analysis.as_of,
            slices=analysis.slices,
            underlying_price=analysis.underlying_price,
            curve_id=analysis.curve_id,
        )
        await self.repository.add_slices(row.id, stored)

        result = AnalyticalResult.ok(stored, provenance, tuple(warnings))
        return result, row.id

    def _provenance(self, snapshot, curve, params, request) -> Provenance:
        return Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_timestamp=snapshot.as_of_timestamp,
            market_data_sources=(snapshot.source,),
            dataset_versions=(
                {snapshot.provider: snapshot.dataset_digest} if snapshot.dataset_digest else {}
            ),
            yield_curve_id=curve.curve_id,
            model_versions={
                "analysis": ANALYSIS_MODEL_VERSION,
                "implied_volatility": IV_MODEL_VERSION,
                "forward": FORWARD_MODEL_VERSION,
            },
            numerical_tolerances={
                "iv_price_abs_tol": 1e-10,
                "iv_bracket": [1e-8, 5.0],
            },
            parameters={
                "chain_snapshot_id": str(snapshot.id),
                "risk_free_rate": params.risk_free_rate,
                "dividend_yield": params.dividend_yield,
                "dividend_yield_assumed": params.dividend_yield_assumed,
                "include_excluded_quotes": params.include_excluded_quotes,
                "expiry_policy": request.expiry_policy.to_provenance(),
                "curve": curve.to_provenance(),
            },
        )

    # ---------------------------------------------------------------- reads
    async def get_analysis(self, analysis_id: uuid.UUID, user_id: uuid.UUID):
        return await self.repository.get_analysis(analysis_id, user_id)

    async def latest_analysis(self, snapshot_id: uuid.UUID, user_id: uuid.UUID):
        return await self.repository.latest_analysis_for_snapshot(snapshot_id, user_id)

    async def list_analyses(self, user_id: uuid.UUID, limit: int = 50, offset: int = 0):
        return await self.repository.list_analyses(user_id, limit=limit, offset=offset)

    # ------------------------------------------------ stateless calculators
    @staticmethod
    def implied_volatility(
        price: float,
        strike: float,
        tau: float,
        is_call: bool,
        spot: float | None = None,
        forward: float | None = None,
        rate: float = 0.0,
        dividend: float = 0.0,
    ) -> dict:
        """Invert one quote. Either a spot or a forward must be supplied."""
        if forward is not None:
            import math

            result = implied_vol_black76(
                price, forward, strike, tau, is_call, math.exp(-rate * tau)
            )
            basis = {"forward": forward, "parameterization": "BLACK76"}
        elif spot is not None:
            result = implied_vol_bsm(price, spot, strike, tau, rate, dividend, is_call)
            basis = {"spot": spot, "parameterization": "BLACK_SCHOLES_MERTON"}
        else:
            raise AnalysisError("supply either a spot or a forward")
        return {**result.to_dict(), **basis}

    @staticmethod
    def price_and_greeks(
        strike: float,
        tau: float,
        sigma: float,
        is_call: bool,
        spot: float | None = None,
        forward: float | None = None,
        rate: float = 0.0,
        dividend: float = 0.0,
    ) -> dict:
        """Price and Greeks for one contract, with units named in the payload."""
        if spot is None and forward is None:
            raise AnalysisError("supply either a spot or a forward")

        if spot is None:
            import math

            # Recover the spot implied by the supplied forward so the Greeks are
            # in the unambiguous spot parameterization.
            spot = float(forward) * math.exp(-(rate - dividend) * tau)

        greeks = bsm_greeks(spot, strike, tau, rate, dividend, sigma, is_call)
        payload = greeks.to_dict()
        payload["units"] = dict(GREEK_UNITS)
        payload["inputs"] = {
            "spot": spot,
            "strike": strike,
            "time_to_expiry": tau,
            "rate": rate,
            "dividend_yield": dividend,
            "sigma": sigma,
            "is_call": is_call,
        }
        payload["forward"] = float(forward) if forward is not None else None
        return payload

    @staticmethod
    def estimate_forward(
        tau: float,
        spot: float | None = None,
        rate: float = 0.0,
        dividend: float = 0.0,
        dividend_assumed: bool = True,
        strikes: list[float] | None = None,
        call_prices: list[float] | None = None,
        put_prices: list[float] | None = None,
        future_price: float | None = None,
    ) -> dict:
        estimates = []
        if strikes and call_prices and put_prices:
            estimates.append(
                ForwardEstimator.from_put_call_parity(strikes, call_prices, put_prices)
            )
        if spot is not None:
            estimates.append(
                ForwardEstimator.from_spot_carry(spot, tau, rate, dividend, dividend_assumed)
            )
        if future_price is not None:
            import math

            estimates.append(
                ForwardEstimator.from_future(future_price, tau, tau, math.exp(-rate * tau))
            )
        if not estimates:
            raise AnalysisError(
                "supply a spot, a future price, or put/call prices to estimate a forward"
            )
        return ForwardEstimator.select(estimates).to_dict()

    # ---------------------------------------------------- surface calibration
    async def calibrate_surface(
        self, user_id: uuid.UUID, analysis_id: uuid.UUID, params: CalibrateSurfaceParams
    ):
        """Fit SVI to a stored chain analysis and persist the surface.

        The analysis is **rehydrated from the database**, not held over from the
        Phase 1 run in memory. That is deliberate: it means the calibration acts
        on exactly what was persisted, so a surface refitted from stored rows in
        six months is the same surface.
        """
        row = await self.repository.get_analysis(analysis_id, user_id)
        if row is None:
            raise AnalysisError(f"analysis {analysis_id} not found")

        analysis = await self._rehydrate_analysis(row)
        request = SurfaceCalibrationRequest(seed=params.seed, use_weights=params.use_weights)
        result = SurfaceCalibrationService().calibrate(analysis, request)
        surface = result.surface

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_timestamp=row.as_of_timestamp,
            yield_curve_id=row.curve_id,
            surface_id=surface.surface_id,
            calibration_timestamp=surface.as_of,
            model_versions={
                "surface": CALIBRATION_MODEL_VERSION,
                "arbitrage": "arbitrage-validator@1.0.0",
            },
            parameters={
                "analysis_id": str(analysis_id),
                "calibration": request.to_provenance(),
            },
        )

        surface_row = await self.repository.create_surface(
            surface_id=surface.surface_id,
            user_id=user_id,
            analysis_id=analysis_id,
            underlying_id=row.underlying_id,
            as_of_timestamp=row.as_of_timestamp,
            model=SURFACE_MODEL,
            model_version=SURFACE_MODEL_VERSION,
            curve_id=row.curve_id,
            slices_total=len(surface.slices),
            slices_fitted=len(surface.fitted_slices),
            calibration_timestamp=datetime.now(UTC),
            summary=surface.to_dict(include_slices=True),
            provenance=provenance.to_dict(),
        )
        await self.repository.add_surface_slices(surface_row.id, surface)
        # Characteristics at standard tenors, so surfaces stay comparable as
        # expiries roll and a percentile history can accumulate.
        await self.repository.add_characteristics(
            surface_row.id,
            user_id,
            row.underlying_id,
            row.as_of_timestamp,
            surface_term_structure(surface),
        )

        # Both scopes are stored, always, and separately.
        for report in (result.raw_report, result.fitted_report):
            await self.repository.add_arbitrage_report(
                report,
                analysis_id=analysis_id,
                user_id=user_id,
                surface_id=surface_row.id,
            )

        payload = {
            **surface.to_dict(include_slices=True),
            "arbitrage": {
                str(ArbitrageScope.RAW_MARKET): result.raw_report.to_dict(max_violations=100),
                str(ArbitrageScope.FITTED_SURFACE): result.fitted_report.to_dict(
                    max_violations=100
                ),
            },
        }
        return (
            AnalyticalResult.ok(payload, provenance, result.warnings),
            surface_row.id,
        )

    async def _rehydrate_analysis(self, row) -> ChainAnalysis:
        """Rebuild a `ChainAnalysis` from persisted rows."""
        points = await self.repository.get_implied_vols(row.id)
        forwards = await self.repository.get_forward_estimates(row.id)

        by_expiry: dict[object, list[ImpliedVolPoint]] = {}
        for point in points:
            by_expiry.setdefault(point.expiry, []).append(
                ImpliedVolPoint(
                    instrument_id=point.instrument_id,
                    expiry=point.expiry,
                    strike=point.strike,
                    option_type=OptionType(point.option_type),
                    price_used=point.price_used,
                    price_source=PriceSource(point.price_source),
                    market_iv=point.market_iv,
                    price_spread=point.price_spread,
                    market_iv_bid=point.market_iv_bid,
                    market_iv_ask=point.market_iv_ask,
                    converged=point.converged,
                    iterations=point.iterations,
                    solver=point.solver,
                    error=point.error,
                    vega=point.vega,
                    uncertainty=point.uncertainty,
                    data_quality_score=point.data_quality_score,
                    liquidity_score=point.liquidity_score,
                    time_to_expiry=point.time_to_expiry,
                    log_moneyness=point.log_moneyness,
                    total_variance=point.total_variance,
                    weight=point.weight,
                    used_for_smile=point.used_for_smile,
                    smile_exclusion=(
                        SmileExclusion(point.smile_exclusion) if point.smile_exclusion else None
                    ),
                )
            )

        estimates_by_expiry: dict[object, list[ForwardEstimate]] = {}
        selected_by_expiry: dict[object, ForwardEstimate] = {}
        tau_by_expiry: dict[object, float | None] = {}
        for estimate in forwards:
            built = ForwardEstimate(
                value=estimate.value,
                method=ForwardMethod(estimate.method),
                confidence=estimate.confidence,
                observations=estimate.observations,
                residual_error=estimate.residual_error,
                discount_factor=estimate.discount_factor,
                error=ForwardFailure(estimate.error) if estimate.error else None,
                assumptions=tuple(estimate.assumptions or []),
            )
            estimates_by_expiry.setdefault(estimate.expiry, []).append(built)
            tau_by_expiry[estimate.expiry] = estimate.time_to_expiry
            if estimate.selected:
                selected_by_expiry[estimate.expiry] = built

        slices = []
        for expiry in sorted(set(by_expiry) | set(estimates_by_expiry)):
            slice_points = by_expiry.get(expiry, [])
            tau = tau_by_expiry.get(expiry)
            if tau is None and slice_points:
                tau = slice_points[0].time_to_expiry
            slices.append(
                SmileSlice(
                    expiry=expiry,
                    time_to_expiry=tau,
                    forward=ForwardEstimateSet(
                        estimates=tuple(estimates_by_expiry.get(expiry, ())),
                        selected=selected_by_expiry.get(expiry),
                    ),
                    points=tuple(slice_points),
                )
            )

        return ChainAnalysis(
            snapshot_id=row.chain_snapshot_id,
            underlying_id=row.underlying_id,
            as_of=row.as_of_timestamp.isoformat(),
            slices=tuple(slices),
            underlying_price=row.underlying_price,
            curve_id=row.curve_id,
        )

    # ----------------------------------------------------- surface retrieval
    async def load_surface(
        self, surface_row_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[object, VolatilitySurface] | None:
        """Rebuild a surface from its persisted parameters.

        Nothing is re-fitted: the five SVI numbers, the forward, the discount
        factor and the maturity are all that reference values depend on, so a
        surface read back is bit-for-bit the surface that was stored.
        """
        row = await self.repository.get_surface(surface_row_id, user_id)
        if row is None:
            return None
        return row, await self._surface_from_rows(row)

    async def _surface_from_rows(self, row) -> VolatilitySurface:
        pairs = await self.repository.get_surface_slices(row.id)
        slices = []
        for slice_row, parameter_row in pairs:
            parameters = (
                SVIParameters(
                    a=parameter_row.a,
                    b=parameter_row.b,
                    rho=parameter_row.rho,
                    m=parameter_row.m,
                    sigma=parameter_row.sigma,
                )
                if parameter_row is not None
                else None
            )
            slices.append(
                SurfaceSliceFit(
                    expiry=slice_row.expiry,
                    time_to_expiry=slice_row.time_to_expiry,
                    forward=slice_row.forward,
                    discount_factor=slice_row.discount_factor,
                    parameters=parameters,
                    calibration=SVICalibrationResult(
                        parameters=parameters,
                        status=CalibrationStatus(slice_row.status),
                        n_observations=slice_row.n_observations,
                        rmse_total_variance=slice_row.rmse_total_variance,
                        weighted_rmse=slice_row.weighted_rmse,
                        rmse_vol_points=slice_row.rmse_vol_points,
                        max_error_vol_points=slice_row.max_error_vol_points,
                        optimizer=slice_row.optimizer,
                        optimizer_message=slice_row.optimizer_message or "",
                        iterations=slice_row.iterations,
                        starts_attempted=slice_row.starts_attempted,
                        starts_feasible=slice_row.starts_feasible,
                        min_durrleman_g=slice_row.min_durrleman_g,
                        min_durrleman_k=slice_row.min_durrleman_k,
                        wing_slope=slice_row.wing_slope,
                        constraints_satisfied=slice_row.constraints_satisfied,
                        error=slice_row.error,
                    ),
                    k_min=slice_row.k_min,
                    k_max=slice_row.k_max,
                    forward_method=slice_row.forward_method,
                    forward_confidence=slice_row.forward_confidence,
                )
            )
        return VolatilitySurface(
            underlying_id=row.underlying_id,
            as_of=row.as_of_timestamp,
            slices=tuple(slices),
            curve_id=row.curve_id,
            analysis_id=row.analysis_id,
            model=row.model,
            model_version=row.model_version,
        )

    async def latest_surface(self, user_id: uuid.UUID, underlying_id: uuid.UUID):
        row = await self.repository.latest_surface_for_underlying(user_id, underlying_id)
        if row is None:
            return None
        return row, await self._surface_from_rows(row)

    async def list_surfaces(self, user_id: uuid.UUID, limit: int = 50, offset: int = 0):
        return await self.repository.list_surfaces(user_id, limit=limit, offset=offset)

    async def reference_values(
        self,
        surface_row_id: uuid.UUID,
        user_id: uuid.UUID,
        requests: list[tuple[Decimal, object, OptionType | None]],
    ) -> list[ReferencePoint]:
        loaded = await self.load_surface(surface_row_id, user_id)
        if loaded is None:
            raise AnalysisError("surface not found")
        _row, surface = loaded
        return [
            surface.reference(strike, expiry, option_type)
            for strike, expiry, option_type in requests
        ]

    # ------------------------------------------------------ anomaly scanning
    async def scan_anomalies(
        self,
        user_id: uuid.UUID,
        surface_row_id: uuid.UUID,
        policy: AnomalyPolicy,
    ):
        """Compare every observed implied volatility against the fitted surface.

        Produces measurements and explanations, never a recommendation. The
        detection policy is a request parameter and is recorded in provenance,
        because it decides the answer.
        """
        loaded = await self.load_surface(surface_row_id, user_id)
        if loaded is None:
            raise AnalysisError(f"surface {surface_row_id} not found")
        surface_row, surface = loaded

        analysis_row = await self.repository.get_analysis(surface_row.analysis_id, user_id)
        if analysis_row is None:
            raise AnalysisError("the analysis this surface was fitted from is missing")
        analysis = await self._rehydrate_analysis(analysis_row)

        instrument_ids = [
            point.instrument_id for slice_ in analysis.slices for point in slice_.points
        ]
        history = await self.repository.deviation_history(instrument_ids)

        scan = SurfaceAnomalyScanner().scan(analysis, surface, policy, history)

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_timestamp=surface_row.as_of_timestamp,
            surface_id=surface.surface_id,
            yield_curve_id=surface_row.curve_id,
            model_versions={
                "anomaly": ANOMALY_MODEL_VERSION,
                "surface": surface_row.model_version,
            },
            parameters={
                "surface_row_id": str(surface_row_id),
                "analysis_id": str(surface_row.analysis_id),
                "policy": policy.to_provenance(),
            },
        )

        scan_row = await self.repository.create_anomaly_scan(
            user_id=user_id,
            surface_id=surface_row_id,
            analysis_id=surface_row.analysis_id,
            underlying_id=surface_row.underlying_id,
            as_of_timestamp=surface_row.as_of_timestamp,
            quotes_examined=scan.quotes_examined,
            quotes_scored=scan.quotes_scored,
            flagged=len(scan.flagged),
            policy=policy.to_provenance(),
            provenance=provenance.to_dict(),
        )
        # Every scored quote is stored, not only the flagged ones: the rest is
        # the evidence the threshold was doing something, and the history a
        # later scan measures against.
        await self.repository.add_anomalies(scan_row.id, scan.anomalies)

        payload = scan.to_dict(include_all=False)
        payload["scan_id"] = str(scan_row.id)
        warnings: list[AnalyticalWarning] = []
        if not any(a.historical_observations for a in scan.anomalies):
            warnings.append(
                AnalyticalWarning.info(
                    "ANOMALY_NO_HISTORY",
                    "No prior scans exist for these contracts, so deviations are "
                    "measured only against what today's market width, fit error and "
                    "measurement resolution can explain. Historical z-scores appear "
                    "once a second scan has run.",
                )
            )
        if scan.quotes_scored == 0:
            warnings.append(
                AnalyticalWarning.error(
                    "ANOMALY_NOTHING_SCORED",
                    "No quote could be compared against the surface.",
                )
            )
        return AnalyticalResult.ok(payload, provenance, tuple(warnings)), scan_row.id

    async def latest_anomaly_scan(self, user_id: uuid.UUID, underlying_id: uuid.UUID):
        return await self.repository.latest_anomaly_scan(user_id, underlying_id)

    async def get_anomaly_scan(self, scan_id: uuid.UUID, user_id: uuid.UUID):
        return await self.repository.get_anomaly_scan(scan_id, user_id)

    # ------------------------------------------------------------- history
    async def tenor_history(
        self,
        user_id: uuid.UUID,
        underlying_id: uuid.UUID,
        tenor_days: int,
        limit: int = 500,
    ) -> TenorHistory:
        rows = await self.repository.get_characteristic_history(
            user_id, underlying_id, tenor_days, limit=limit
        )
        return build_tenor_history(tenor_days, rows)

    async def available_tenors(self, user_id: uuid.UUID, underlying_id: uuid.UUID):
        return await self.repository.available_tenors(user_id, underlying_id)
