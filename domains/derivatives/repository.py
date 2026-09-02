"""Derivatives persistence operations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.derivatives.arbitrage import ArbitrageReport
from domains.derivatives.models import ChainAnalysis, ImpliedVolPoint, SmileSlice
from domains.derivatives.orm import (
    AnomalyScanORM,
    ArbitrageReportORM,
    ArbitrageViolationORM,
    ChainAnalysisORM,
    ForwardEstimateORM,
    ImpliedVolORM,
    SurfaceAnomalyORM,
    SurfaceCharacteristicORM,
    SurfaceParametersORM,
    SurfaceSliceORM,
    VolatilitySurfaceORM,
    YieldCurveORM,
)
from domains.derivatives.surface import VolatilitySurface
from domains.market_data.curves import YieldCurve


class DerivativesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------------------------------------------------------- curves
    async def upsert_curve(self, curve: YieldCurve) -> YieldCurveORM:
        """Curves are content-addressed, so storing one twice is a no-op."""
        stmt = select(YieldCurveORM).where(YieldCurveORM.curve_id == curve.curve_id)
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing
        row = YieldCurveORM(
            curve_id=curve.curve_id,
            as_of_timestamp=curve.as_of,
            currency=curve.currency,
            source=curve.source,
            day_count=str(curve.day_count),
            interpolation=str(curve.interpolation),
            label=curve.label,
            points={"times": list(curve.times), "zero_rates": list(curve.zero_rates)},
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_curve(self, curve_id: str) -> YieldCurveORM | None:
        stmt = select(YieldCurveORM).where(YieldCurveORM.curve_id == curve_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # -------------------------------------------------------------- analyses
    async def create_analysis(self, **kwargs) -> ChainAnalysisORM:
        row = ChainAnalysisORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_analysis(
        self, analysis_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> ChainAnalysisORM | None:
        row = await self._session.get(ChainAnalysisORM, analysis_id)
        if row is None:
            return None
        if user_id is not None and row.user_id != user_id:
            return None
        return row

    async def latest_analysis_for_snapshot(
        self, snapshot_id: uuid.UUID, user_id: uuid.UUID
    ) -> ChainAnalysisORM | None:
        stmt = (
            select(ChainAnalysisORM)
            .where(
                ChainAnalysisORM.chain_snapshot_id == snapshot_id,
                ChainAnalysisORM.user_id == user_id,
            )
            .order_by(ChainAnalysisORM.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_analyses(
        self, user_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[ChainAnalysisORM]:
        stmt = (
            select(ChainAnalysisORM)
            .where(ChainAnalysisORM.user_id == user_id)
            .order_by(ChainAnalysisORM.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------- children
    async def add_slices(self, analysis_id: uuid.UUID, analysis: ChainAnalysis) -> None:
        for slice_ in analysis.slices:
            self._add_forwards(analysis_id, analysis.underlying_id, slice_)
            self._add_points(analysis_id, slice_.points)
        await self._session.flush()

    def _add_forwards(
        self, analysis_id: uuid.UUID, underlying_id: uuid.UUID, slice_: SmileSlice
    ) -> None:
        selected = slice_.forward.selected
        for estimate in slice_.forward.estimates:
            self._session.add(
                ForwardEstimateORM(
                    analysis_id=analysis_id,
                    underlying_id=underlying_id,
                    expiry=slice_.expiry,
                    method=str(estimate.method),
                    selected=selected is not None and estimate is selected,
                    value=estimate.value,
                    confidence=estimate.confidence,
                    observations=estimate.observations,
                    residual_error=estimate.residual_error,
                    discount_factor=estimate.discount_factor,
                    time_to_expiry=slice_.time_to_expiry,
                    error=str(estimate.error) if estimate.error else None,
                    assumptions=list(estimate.assumptions),
                )
            )

    def _add_points(self, analysis_id: uuid.UUID, points: Sequence[ImpliedVolPoint]) -> None:
        for point in points:
            self._session.add(
                ImpliedVolORM(
                    analysis_id=analysis_id,
                    instrument_id=point.instrument_id,
                    expiry=point.expiry,
                    strike=point.strike,
                    option_type=str(point.option_type),
                    price_used=point.price_used,
                    price_source=str(point.price_source),
                    price_spread=point.price_spread,
                    market_iv=point.market_iv,
                    market_iv_bid=point.market_iv_bid,
                    market_iv_ask=point.market_iv_ask,
                    converged=point.converged,
                    iterations=point.iterations,
                    solver=point.solver,
                    # The CHECK constraint requires a value or a reason; a point
                    # with neither would be a silent hole in the surface.
                    error=point.error or (None if point.market_iv is not None else "UNKNOWN"),
                    vega=point.vega,
                    uncertainty=point.uncertainty,
                    data_quality_score=point.data_quality_score,
                    liquidity_score=point.liquidity_score,
                    time_to_expiry=point.time_to_expiry,
                    log_moneyness=point.log_moneyness,
                    total_variance=point.total_variance,
                    weight=point.weight,
                    used_for_smile=point.used_for_smile,
                    smile_exclusion=(str(point.smile_exclusion) if point.smile_exclusion else None),
                )
            )

    async def get_forward_estimates(self, analysis_id: uuid.UUID) -> list[ForwardEstimateORM]:
        stmt = (
            select(ForwardEstimateORM)
            .where(ForwardEstimateORM.analysis_id == analysis_id)
            .order_by(ForwardEstimateORM.expiry, ForwardEstimateORM.method)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_implied_vols(
        self,
        analysis_id: uuid.UUID,
        expiry: object | None = None,
        used_for_smile_only: bool = False,
        limit: int = 20000,
    ) -> list[ImpliedVolORM]:
        stmt = select(ImpliedVolORM).where(ImpliedVolORM.analysis_id == analysis_id)
        if expiry is not None:
            stmt = stmt.where(ImpliedVolORM.expiry == expiry)
        if used_for_smile_only:
            stmt = stmt.where(ImpliedVolORM.used_for_smile.is_(True))
        stmt = stmt.order_by(
            ImpliedVolORM.expiry, ImpliedVolORM.strike, ImpliedVolORM.option_type
        ).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    # -------------------------------------------------------------- surfaces
    async def create_surface(self, **kwargs) -> VolatilitySurfaceORM:
        row = VolatilitySurfaceORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def add_surface_slices(
        self, surface_row_id: uuid.UUID, surface: VolatilitySurface
    ) -> None:
        for slice_ in surface.slices:
            calibration = slice_.calibration
            slice_row = SurfaceSliceORM(
                surface_id=surface_row_id,
                expiry=slice_.expiry,
                time_to_expiry=slice_.time_to_expiry,
                forward=slice_.forward,
                discount_factor=slice_.discount_factor,
                forward_method=slice_.forward_method,
                forward_confidence=slice_.forward_confidence,
                status=str(calibration.status),
                n_observations=calibration.n_observations,
                rmse_total_variance=calibration.rmse_total_variance,
                weighted_rmse=calibration.weighted_rmse,
                rmse_vol_points=calibration.rmse_vol_points,
                max_error_vol_points=calibration.max_error_vol_points,
                optimizer=calibration.optimizer,
                optimizer_message=(calibration.optimizer_message or "")[:255],
                iterations=calibration.iterations,
                starts_attempted=calibration.starts_attempted,
                starts_feasible=calibration.starts_feasible,
                min_durrleman_g=calibration.min_durrleman_g,
                min_durrleman_k=calibration.min_durrleman_k,
                wing_slope=calibration.wing_slope,
                constraints_satisfied=calibration.constraints_satisfied,
                k_min=slice_.k_min,
                k_max=slice_.k_max,
                error=(calibration.error or None) and calibration.error[:255],
            )
            self._session.add(slice_row)
            await self._session.flush()

            if slice_.parameters is not None:
                self._session.add(
                    SurfaceParametersORM(
                        slice_id=slice_row.id,
                        parameterization="RAW_SVI",
                        a=slice_.parameters.a,
                        b=slice_.parameters.b,
                        rho=slice_.parameters.rho,
                        m=slice_.parameters.m,
                        sigma=slice_.parameters.sigma,
                    )
                )
        await self._session.flush()

    async def get_surface(
        self, surface_row_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> VolatilitySurfaceORM | None:
        row = await self._session.get(VolatilitySurfaceORM, surface_row_id)
        if row is None:
            return None
        if user_id is not None and row.user_id != user_id:
            return None
        return row

    async def latest_surface_for_underlying(
        self, user_id: uuid.UUID, underlying_id: uuid.UUID
    ) -> VolatilitySurfaceORM | None:
        stmt = (
            select(VolatilitySurfaceORM)
            .where(
                VolatilitySurfaceORM.user_id == user_id,
                VolatilitySurfaceORM.underlying_id == underlying_id,
            )
            .order_by(
                VolatilitySurfaceORM.as_of_timestamp.desc(),
                VolatilitySurfaceORM.created_at.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_surfaces(
        self, user_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[VolatilitySurfaceORM]:
        stmt = (
            select(VolatilitySurfaceORM)
            .where(VolatilitySurfaceORM.user_id == user_id)
            .order_by(VolatilitySurfaceORM.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_surface_slices(
        self, surface_row_id: uuid.UUID
    ) -> list[tuple[SurfaceSliceORM, SurfaceParametersORM | None]]:
        stmt = (
            select(SurfaceSliceORM, SurfaceParametersORM)
            .outerjoin(SurfaceParametersORM, SurfaceParametersORM.slice_id == SurfaceSliceORM.id)
            .where(SurfaceSliceORM.surface_id == surface_row_id)
            .order_by(SurfaceSliceORM.expiry)
        )
        return [tuple(row) for row in (await self._session.execute(stmt)).all()]

    # ------------------------------------------------------------- arbitrage
    async def add_arbitrage_report(
        self, report: ArbitrageReport, *, analysis_id, user_id, surface_id=None
    ) -> ArbitrageReportORM:
        row = ArbitrageReportORM(
            analysis_id=analysis_id,
            user_id=user_id,
            surface_id=surface_id,
            scope=str(report.scope),
            severity=str(report.severity) if report.severity else None,
            violations_total=len(report.violations),
            observations=report.observations,
            checks_run=list(report.checks_run),
            summary=report.summary,
        )
        self._session.add(row)
        await self._session.flush()

        for violation in report.violations:
            self._session.add(
                ArbitrageViolationORM(
                    report_id=row.id,
                    scope=str(violation.scope),
                    violation_type=str(violation.violation_type),
                    severity=str(violation.severity),
                    magnitude=violation.magnitude,
                    tolerance=violation.tolerance,
                    expiry=violation.expiry,
                    strike=violation.strike,
                    option_type=(str(violation.option_type) if violation.option_type else None),
                    detail=violation.detail,
                    affected_instruments=[str(i) for i in violation.affected_instruments],
                )
            )
        await self._session.flush()
        return row

    async def get_arbitrage_reports(self, analysis_id: uuid.UUID) -> list[ArbitrageReportORM]:
        stmt = (
            select(ArbitrageReportORM)
            .where(ArbitrageReportORM.analysis_id == analysis_id)
            .order_by(ArbitrageReportORM.scope)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_arbitrage_violations(
        self,
        report_id: uuid.UUID,
        min_severity: str | None = None,
        limit: int = 2000,
    ) -> list[ArbitrageViolationORM]:
        stmt = select(ArbitrageViolationORM).where(ArbitrageViolationORM.report_id == report_id)
        if min_severity:
            order = {"INFO": 0, "WARNING": 1, "ERROR": 2}
            allowed = [k for k, v in order.items() if v >= order[min_severity]]
            stmt = stmt.where(ArbitrageViolationORM.severity.in_(allowed))
        stmt = stmt.order_by(ArbitrageViolationORM.magnitude.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    # ------------------------------------------------------- characteristics
    async def add_characteristics(
        self,
        surface_row_id: uuid.UUID,
        user_id: uuid.UUID,
        underlying_id: uuid.UUID,
        as_of,
        characteristics,
    ) -> None:
        for characteristic in characteristics:
            self._session.add(
                SurfaceCharacteristicORM(
                    surface_id=surface_row_id,
                    user_id=user_id,
                    underlying_id=underlying_id,
                    as_of_timestamp=as_of,
                    tenor_days=characteristic.tenor_days,
                    time_to_expiry=characteristic.time_to_expiry,
                    forward=characteristic.forward,
                    atm_volatility=characteristic.atm_volatility,
                    skew=characteristic.skew,
                    curvature=characteristic.curvature,
                    atm_total_variance=characteristic.atm_total_variance,
                    method=str(characteristic.method),
                    flags=[str(flag) for flag in characteristic.flags],
                )
            )
        await self._session.flush()

    async def get_characteristic_history(
        self,
        user_id: uuid.UUID,
        underlying_id: uuid.UUID,
        tenor_days: int,
        limit: int = 500,
    ) -> list[SurfaceCharacteristicORM]:
        """Oldest first, so the last row is the current observation."""
        stmt = (
            select(SurfaceCharacteristicORM)
            .where(
                SurfaceCharacteristicORM.user_id == user_id,
                SurfaceCharacteristicORM.underlying_id == underlying_id,
                SurfaceCharacteristicORM.tenor_days == tenor_days,
            )
            .order_by(SurfaceCharacteristicORM.as_of_timestamp.desc())
            .limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        return list(reversed(rows))

    async def available_tenors(self, user_id: uuid.UUID, underlying_id: uuid.UUID) -> list[int]:
        stmt = (
            select(SurfaceCharacteristicORM.tenor_days)
            .where(
                SurfaceCharacteristicORM.user_id == user_id,
                SurfaceCharacteristicORM.underlying_id == underlying_id,
            )
            .distinct()
            .order_by(SurfaceCharacteristicORM.tenor_days)
        )
        return [int(value) for value in (await self._session.execute(stmt)).scalars().all()]

    # ------------------------------------------------------------- anomalies
    async def create_anomaly_scan(self, **kwargs) -> AnomalyScanORM:
        row = AnomalyScanORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def add_anomalies(self, scan_id: uuid.UUID, anomalies) -> None:
        for anomaly in anomalies:
            self._session.add(
                SurfaceAnomalyORM(
                    scan_id=scan_id,
                    instrument_id=anomaly.instrument_id,
                    expiry=anomaly.expiry,
                    strike=anomaly.strike,
                    option_type=str(anomaly.option_type),
                    market_iv=anomaly.market_iv,
                    reference_iv=anomaly.reference_iv,
                    iv_difference=anomaly.iv_difference,
                    relative_deviation=anomaly.relative_deviation,
                    market_iv_bid=anomaly.market_iv_bid,
                    market_iv_ask=anomaly.market_iv_ask,
                    envelope_position=str(anomaly.envelope_position),
                    excess_over_envelope=anomaly.excess_over_envelope,
                    explained_scale=anomaly.explained_scale,
                    z_score=anomaly.z_score,
                    historical_z_score=anomaly.historical_z_score,
                    historical_observations=anomaly.historical_observations,
                    liquidity_score=anomaly.liquidity_score,
                    data_quality_score=anomaly.data_quality_score,
                    calibration_rmse_vol_points=anomaly.calibration_rmse_vol_points,
                    iv_uncertainty=anomaly.iv_uncertainty,
                    reference_method=str(anomaly.reference_method),
                    reference_flags=[str(f) for f in anomaly.reference_flags],
                    confidence=anomaly.confidence,
                    flagged=anomaly.flagged,
                    explanation=[entry.to_dict() for entry in anomaly.explanation],
                )
            )
        await self._session.flush()

    async def get_anomaly_scan(
        self, scan_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> AnomalyScanORM | None:
        row = await self._session.get(AnomalyScanORM, scan_id)
        if row is None:
            return None
        if user_id is not None and row.user_id != user_id:
            return None
        return row

    async def latest_anomaly_scan(
        self, user_id: uuid.UUID, underlying_id: uuid.UUID
    ) -> AnomalyScanORM | None:
        stmt = (
            select(AnomalyScanORM)
            .where(
                AnomalyScanORM.user_id == user_id,
                AnomalyScanORM.underlying_id == underlying_id,
            )
            .order_by(AnomalyScanORM.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_anomalies(
        self,
        scan_id: uuid.UUID,
        flagged_only: bool = True,
        min_confidence: float = 0.0,
        limit: int = 1000,
    ) -> list[SurfaceAnomalyORM]:
        stmt = select(SurfaceAnomalyORM).where(SurfaceAnomalyORM.scan_id == scan_id)
        if flagged_only:
            stmt = stmt.where(SurfaceAnomalyORM.flagged.is_(True))
        if min_confidence > 0:
            stmt = stmt.where(SurfaceAnomalyORM.confidence >= min_confidence)
        stmt = stmt.order_by(func.abs(SurfaceAnomalyORM.z_score).desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def deviation_history(
        self, instrument_ids: list[uuid.UUID], limit_per_instrument: int = 200
    ) -> dict[uuid.UUID, list[float]]:
        """Past deviations per contract, for the time-series z-score.

        Empty until a second scan exists, which is the honest state of a fresh
        installation and is reported as ``historical_observations = 0`` rather
        than as a z-score of zero.
        """
        if not instrument_ids:
            return {}
        stmt = (
            select(SurfaceAnomalyORM.instrument_id, SurfaceAnomalyORM.iv_difference)
            .where(SurfaceAnomalyORM.instrument_id.in_(instrument_ids))
            .order_by(SurfaceAnomalyORM.created_at.desc())
            .limit(limit_per_instrument * max(len(instrument_ids), 1))
        )
        history: dict[uuid.UUID, list[float]] = {}
        for instrument_id, difference in (await self._session.execute(stmt)).all():
            history.setdefault(instrument_id, []).append(float(difference))
        return history
