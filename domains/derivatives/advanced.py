"""Phase 9 application service: global surface, local volatility, consensus.

The database-facing wrapper for the advanced derivatives pipeline, kept apart
from :mod:`domains.derivatives.application` because it composes a *different*
set of models over the same analysis rather than extending the Phase 1-3 one.

Everything here is derived from a persisted ``ChainAnalysis``, rehydrated from
the database rather than carried over in memory, so a surface refitted from
stored rows in six months is the same surface.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from domains.derivatives.application import AnalysisError, DerivativesService
from domains.derivatives.consensus import (
    CONSENSUS_MODEL_VERSION,
    ConfidenceContribution,
    ConsensusInputs,
    ConsensusResult,
    ModelConsensusService,
    PricingModelKind,
)
from domains.derivatives.global_surface import (
    GLOBAL_SURFACE_MODEL,
    GLOBAL_SURFACE_MODEL_VERSION,
    LOCAL_VOL_MODEL_VERSION,
    GlobalSurface,
    GlobalSurfaceCalibrationRequest,
    GlobalSurfaceCalibrationService,
    GlobalSurfaceSlice,
)
from domains.derivatives.repository import DerivativesRepository
from domains.instruments.enums import AssetClass, OptionType
from domains.instruments.service import InstrumentService
from domains.market_data.service import MarketDataService
from domains.reports.envelope import AnalyticalResult
from domains.reports.provenance import Provenance
from domains.reports.warnings import AnalyticalWarning
from infrastructure.settings import Settings
from quant.numerical.pde import GridSpec
from quant.pricing.black_scholes import bsm_greeks
from quant.pricing.heston import HestonParameters
from quant.pricing.heston_calibration import (
    HestonObservation,
    calibrate_heston,
)
from quant.pricing.higher_order import bsm_higher_order_greeks
from quant.volatility.density import risk_neutral_density
from quant.volatility.ssvi import SSVIParameters, ThetaTermStructure
from quant.volatility.ssvi_calibration import SSVISliceDiagnostics
from quant.volatility.svi_calibration import CalibrationStatus

HESTON_MODEL_VERSION = "heston-slsqp@1.0.0"
DENSITY_MODEL_VERSION = "breeden-litzenberger@1.0.0"

#: Quantiles stored for an admissible density. Fixed rather than caller-chosen
#: so that two densities are always comparable at the same points.
STORED_PERCENTILES = (0.05, 0.25, 0.50, 0.75, 0.95)

#: How far back to look for an observed two-sided mid for the contract. Bounded
#: rather than unbounded so that a comparison against a quote from last month is
#: an absence rather than a silently ancient number.
OBSERVED_MID_LOOKBACK_DAYS = 1


class AdvancedWarningCode:
    NO_HESTON = "ADVANCED_HESTON_NOT_CALIBRATED"
    HESTON_CAVEATED = "ADVANCED_HESTON_FITTED_WITH_CAVEATS"
    NO_LOCAL_VOL = "ADVANCED_LOCAL_VOLATILITY_UNAVAILABLE"
    NO_DENSITY = "ADVANCED_DENSITY_UNAVAILABLE"
    NO_SURFACE = "ADVANCED_NO_GLOBAL_SURFACE"
    NO_MARKET_PRICE = "ADVANCED_NO_OBSERVED_PRICE"
    NOT_AN_OPTION = "ADVANCED_INSTRUMENT_IS_NOT_AN_OPTION"


@dataclass(frozen=True, slots=True)
class CalibrateGlobalSurfaceParams:
    seed: int = 20_260_924
    use_weights: bool = True
    enforce_butterfly_bounds: bool = True
    calibrate_heston: bool = True
    require_feller: bool = False
    build_local_volatility: bool = True
    local_vol_k_range: float = 0.5
    local_vol_nodes: int = 41
    build_densities: bool = True
    density_points: int = 81
    density_k_range: float = 0.5

    def to_provenance(self) -> dict:
        return {
            "seed": self.seed,
            "use_weights": self.use_weights,
            "enforce_butterfly_bounds": self.enforce_butterfly_bounds,
            "calibrate_heston": self.calibrate_heston,
            "require_feller": self.require_feller,
            "local_vol_k_range": self.local_vol_k_range,
            "local_vol_nodes": self.local_vol_nodes,
            "density_points": self.density_points,
            "density_k_range": self.density_k_range,
        }


@dataclass(frozen=True, slots=True)
class PriceConsensusParams:
    """One contract, and the models to compare on it."""

    instrument_id: uuid.UUID
    models: tuple[PricingModelKind, ...] = field(default=())
    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0
    paths: int = 100_000
    seed: int = 20_260_924
    grid_nodes: int = 401
    grid_steps: int = 200
    global_surface_row_id: uuid.UUID | None = None

    def to_provenance(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "models": [str(model) for model in self.models],
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "paths": self.paths,
            "seed": self.seed,
            "grid": {"nodes": self.grid_nodes, "steps": self.grid_steps},
        }


class AdvancedDerivativesService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self.repository = DerivativesRepository(session)
        self.instruments = InstrumentService(session)
        self._derivatives = DerivativesService(session, settings)

    # ------------------------------------------------------- global surface
    async def calibrate_global_surface(
        self, user_id: uuid.UUID, analysis_id: uuid.UUID, params: CalibrateGlobalSurfaceParams
    ):
        row = await self.repository.get_analysis(analysis_id, user_id)
        if row is None:
            raise AnalysisError(f"analysis {analysis_id} not found")

        analysis = await self._derivatives._rehydrate_analysis(row)
        request = GlobalSurfaceCalibrationRequest(
            seed=params.seed,
            use_weights=params.use_weights,
            enforce_butterfly_bounds=params.enforce_butterfly_bounds,
        )
        outcome = GlobalSurfaceCalibrationService().calibrate(analysis, request)
        surface = outcome.surface
        calibration = outcome.calibration
        warnings = list(outcome.warnings)

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_timestamp=row.as_of_timestamp,
            yield_curve_id=row.curve_id,
            surface_id=surface.surface_id,
            calibration_timestamp=surface.as_of,
            model_versions={
                "global_surface": GLOBAL_SURFACE_MODEL_VERSION,
                "local_volatility": LOCAL_VOL_MODEL_VERSION,
                "density": DENSITY_MODEL_VERSION,
                "heston": HESTON_MODEL_VERSION,
            },
            parameters={"analysis_id": str(analysis_id), "calibration": request.to_provenance()},
            numerical_tolerances=params.to_provenance(),
        )

        surface_row = await self.repository.create_global_surface(
            surface_id=surface.surface_id,
            user_id=user_id,
            analysis_id=analysis_id,
            underlying_id=row.underlying_id,
            as_of_timestamp=row.as_of_timestamp,
            model=GLOBAL_SURFACE_MODEL,
            model_version=GLOBAL_SURFACE_MODEL_VERSION,
            curve_id=row.curve_id,
            status=str(calibration.status),
            rho=calibration.parameters.rho if calibration.parameters else None,
            eta=calibration.parameters.eta if calibration.parameters else None,
            gamma=calibration.parameters.gamma if calibration.parameters else None,
            n_observations=calibration.n_observations,
            n_slices=calibration.n_slices,
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
            max_butterfly_quantity=calibration.max_butterfly_quantity,
            butterfly_bounds_satisfied=calibration.butterfly_bounds_satisfied,
            calendar_arbitrage_free=calibration.calendar_arbitrage_free,
            error=(calibration.error or None) and calibration.error[:255],
            calibration_timestamp=datetime.now(UTC),
            provenance=provenance.to_dict(),
        )
        await self.repository.add_global_surface_slices(surface_row.id, surface)

        payload: dict = {
            **surface.to_dict(include_slices=True),
            "global_surface_row_id": str(surface_row.id),
            "calibration": calibration.to_dict(),
        }

        payload["local_volatility"] = await self._store_local_volatility(
            user_id, surface_row.id, row.underlying_id, surface, params, warnings, provenance
        )
        payload["densities"] = await self._store_densities(
            user_id, surface_row.id, row.underlying_id, surface, params, warnings, provenance
        )
        payload["heston"] = await self._store_heston(
            user_id, analysis_id, row, analysis, params, warnings, provenance
        )

        return AnalyticalResult.ok(payload, provenance, tuple(warnings)), surface_row.id

    async def _store_local_volatility(
        self,
        user_id: uuid.UUID,
        surface_row_id: uuid.UUID,
        underlying_id: uuid.UUID,
        surface: GlobalSurface,
        params: CalibrateGlobalSurfaceParams,
        warnings: list[AnalyticalWarning],
        provenance: Provenance,
    ) -> dict | None:
        if not params.build_local_volatility:
            return None
        grid = (
            surface.local_volatility_grid(
                k_range=params.local_vol_k_range, nodes=params.local_vol_nodes
            )
            if surface.usable
            else None
        )
        if grid is None:
            warnings.append(
                AnalyticalWarning.info(
                    AdvancedWarningCode.NO_LOCAL_VOL,
                    "No local-volatility grid was produced: Dupire's formula needs "
                    "a calibrated global surface and there is none.",
                )
            )
            return None

        total = len(grid.points)
        valid = len(grid.valid)
        flag_counts = grid.flag_counts()
        matrix = grid.grid()
        stored = grid.to_dict(include_points=True)
        # The numeric matrix alongside the point detail: the detail is the
        # record, the matrix is what a surface plot reads, and ``None`` marks a
        # hole so a plotting layer cannot draw a line through one.
        stored["values"] = [
            [None if not np.isfinite(value) else float(value) for value in row_values]
            for row_values in matrix
        ]
        row = await self.repository.add_local_volatility_surface(
            user_id=user_id,
            global_surface_id=surface_row_id,
            underlying_id=underlying_id,
            as_of_timestamp=surface.as_of,
            model_version=LOCAL_VOL_MODEL_VERSION,
            spot=float(surface.underlying_price or 0.0),
            carry=surface.carry,
            total_points=total,
            valid_points=valid,
            flagged_points=total - valid,
            coverage=grid.coverage,
            flag_counts=flag_counts,
            grid=stored,
            provenance=provenance.to_dict(),
        )
        if valid < total:
            warnings.append(
                AnalyticalWarning.info(
                    AdvancedWarningCode.NO_LOCAL_VOL,
                    f"{total - valid} of {total} local-volatility grid points have no "
                    "value. Dupire's denominator vanishes where the implied surface is "
                    "locally flat or inverted; those points are stored as holes with "
                    "their reasons rather than interpolated over.",
                    flag_counts=flag_counts,
                )
            )
        payload = grid.to_dict(include_points=False)
        payload["local_volatility_row_id"] = str(row.id)
        return payload

    async def _store_densities(
        self,
        user_id: uuid.UUID,
        surface_row_id: uuid.UUID,
        underlying_id: uuid.UUID,
        surface: GlobalSurface,
        params: CalibrateGlobalSurfaceParams,
        warnings: list[AnalyticalWarning],
        provenance: Provenance,
    ) -> list[dict]:
        if not params.build_densities or not surface.usable:
            return []
        ssvi = surface.ssvi
        assert ssvi is not None

        stored: list[dict] = []
        for slice_ in surface.slices:
            tau = slice_.time_to_expiry
            forward = slice_.forward
            strikes = forward * np.exp(
                np.linspace(-params.density_k_range, params.density_k_range, params.density_points)
            )

            def volatility(at: np.ndarray, _f=forward, _t=tau) -> np.ndarray:
                return np.asarray(ssvi.implied_volatility(np.log(at / _f), _t), dtype=float)

            try:
                density = risk_neutral_density(
                    strikes,
                    volatility,
                    forward=forward,
                    maturity=tau,
                    discount_factor=slice_.discount_factor,
                )
            except ValueError as exc:
                warnings.append(
                    AnalyticalWarning.info(
                        AdvancedWarningCode.NO_DENSITY,
                        f"No implied density for {slice_.expiry}: {exc}.",
                        expiry=slice_.expiry.isoformat(),
                    )
                )
                continue

            quantiles = (
                {p: density.percentile(p) for p in STORED_PERCENTILES}
                if density.is_admissible
                else dict.fromkeys(STORED_PERCENTILES)
            )
            if not density.is_admissible:
                warnings.append(
                    AnalyticalWarning.warn(
                        AdvancedWarningCode.NO_DENSITY,
                        f"The implied density for {slice_.expiry} is not admissible "
                        f"({', '.join(str(flag) for flag in density.flags)}). Its shape "
                        "is stored because it is evidence about the surface, but no "
                        "quantile is: a quantile of a curve that does not integrate to "
                        "one is a number with no meaning.",
                        expiry=slice_.expiry.isoformat(),
                        flags=[str(flag) for flag in density.flags],
                    )
                )

            row = await self.repository.add_density(
                user_id=user_id,
                global_surface_id=surface_row_id,
                underlying_id=underlying_id,
                expiry=slice_.expiry,
                time_to_expiry=tau,
                forward=forward,
                discount_factor=slice_.discount_factor,
                total_mass=density.total_mass,
                implied_mean=density.implied_mean,
                negative_mass=density.negative_mass,
                mean_error=density.mean_error,
                is_admissible=density.is_admissible,
                flags=[str(flag) for flag in density.flags],
                percentile_5=quantiles[0.05],
                percentile_25=quantiles[0.25],
                percentile_50=quantiles[0.50],
                percentile_75=quantiles[0.75],
                percentile_95=quantiles[0.95],
                strikes=[float(value) for value in density.strikes],
                density=[float(value) for value in density.density],
                provenance=provenance.to_dict(),
            )
            payload = density.to_dict(include_points=False)
            payload["expiry"] = slice_.expiry.isoformat()
            payload["density_row_id"] = str(row.id)
            payload["percentiles"] = {str(p): quantiles[p] for p in STORED_PERCENTILES}
            stored.append(payload)
        return stored

    async def _store_heston(
        self,
        user_id: uuid.UUID,
        analysis_id: uuid.UUID,
        analysis_row,
        analysis,
        params: CalibrateGlobalSurfaceParams,
        warnings: list[AnalyticalWarning],
        provenance: Provenance,
    ) -> dict | None:
        if not params.calibrate_heston:
            return None
        spot = float(analysis.underlying_price) if analysis.underlying_price is not None else None
        if spot is None or spot <= 0:
            warnings.append(
                AnalyticalWarning.info(
                    AdvancedWarningCode.NO_HESTON,
                    "Heston was not calibrated: the analysis carries no underlying "
                    "price, and a spot-parameterized model cannot be fitted without "
                    "the spot it is parameterized in.",
                )
            )
            return None

        analysis_parameters = (analysis_row.provenance or {}).get("parameters", {})
        rate = float(analysis_parameters.get("risk_free_rate", 0.0))
        dividend = float(analysis_parameters.get("dividend_yield", 0.0))

        observations: list[HestonObservation] = []
        for slice_ in analysis.slices:
            tau = slice_.time_to_expiry
            if tau is None or tau <= 0:
                continue
            for point in slice_.points:
                if not point.used_for_smile or point.market_iv is None or not point.price_used:
                    continue
                greeks = bsm_greeks(
                    spot,
                    float(point.strike),
                    tau,
                    rate,
                    dividend,
                    point.market_iv,
                    point.option_type is OptionType.CALL,
                )
                observations.append(
                    HestonObservation(
                        strike=float(point.strike),
                        maturity=tau,
                        price=float(point.price_used),
                        is_call=point.option_type is OptionType.CALL,
                        vega=float(np.asarray(greeks.vega)),
                        rate=rate,
                        dividend=dividend,
                        market_volatility=point.market_iv,
                        weight=point.weight or 1.0,
                    )
                )

        result = calibrate_heston(
            spot, observations, seed=params.seed, require_feller=params.require_feller
        )
        if result.parameters is None:
            warnings.append(
                AnalyticalWarning.warn(
                    AdvancedWarningCode.NO_HESTON,
                    f"Heston did not calibrate: {result.error}. The consensus will "
                    "run without it and report the reduced model count.",
                )
            )

        row = await self.repository.add_heston_calibration(
            user_id=user_id,
            analysis_id=analysis_id,
            underlying_id=analysis_row.underlying_id,
            as_of_timestamp=analysis_row.as_of_timestamp,
            model_version=HESTON_MODEL_VERSION,
            status=str(result.status),
            v0=result.parameters.v0 if result.parameters else None,
            kappa=result.parameters.kappa if result.parameters else None,
            theta=result.parameters.theta if result.parameters else None,
            xi=result.parameters.xi if result.parameters else None,
            rho=result.parameters.rho if result.parameters else None,
            n_observations=result.n_observations,
            n_maturities=result.n_maturities,
            rmse_price=result.rmse_price,
            rmse_vol_points=result.rmse_vol_points,
            max_error_vol_points=result.max_error_vol_points,
            optimizer=result.optimizer,
            optimizer_message=(result.optimizer_message or "")[:255],
            iterations=result.iterations,
            starts_attempted=result.starts_attempted,
            starts_feasible=result.starts_feasible,
            feller=result.feller,
            satisfies_feller=result.satisfies_feller,
            feller_enforced=result.feller_enforced,
            warnings=[str(warning) for warning in result.warnings],
            error=(result.error or None) and result.error[:255],
            provenance=provenance.to_dict(),
        )
        payload = result.to_dict()
        payload["heston_calibration_row_id"] = str(row.id)
        return payload

    # ------------------------------------------------------------ retrieval
    async def load_global_surface(
        self, row_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[object, GlobalSurface] | None:
        row = await self.repository.get_global_surface(row_id, user_id)
        if row is None:
            return None
        return row, await self._global_surface_from_rows(row)

    async def latest_global_surface(self, user_id: uuid.UUID, underlying_id: uuid.UUID):
        row = await self.repository.latest_global_surface(user_id, underlying_id)
        if row is None:
            return None
        return row, await self._global_surface_from_rows(row)

    async def list_global_surfaces(self, user_id: uuid.UUID, limit: int = 50, offset: int = 0):
        return await self.repository.list_global_surfaces(user_id, limit=limit, offset=offset)

    async def _global_surface_from_rows(self, row) -> GlobalSurface:
        """Rebuild a surface from its stored parameters. Nothing is re-fitted.

        Three shape parameters and one theta per expiry are all a reference
        value depends on, so a surface read back is the surface that was stored.
        """
        slice_rows = await self.repository.get_global_surface_slices(row.id)
        parameters = (
            SSVIParameters(rho=row.rho, eta=row.eta, gamma=row.gamma)
            if row.rho is not None and row.eta is not None and row.gamma is not None
            else None
        )
        term_structure = (
            ThetaTermStructure(
                maturities=tuple(s.time_to_expiry for s in slice_rows),
                thetas=tuple(s.theta for s in slice_rows),
            )
            if slice_rows
            else None
        )
        spot = None
        analysis = await self.repository.get_analysis(row.analysis_id)
        if analysis is not None and analysis.underlying_price is not None:
            spot = float(analysis.underlying_price)

        slices = tuple(
            GlobalSurfaceSlice(
                expiry=s.expiry,
                time_to_expiry=s.time_to_expiry,
                forward=s.forward,
                discount_factor=s.discount_factor,
                theta=s.theta,
                forward_method=s.forward_method,
                forward_confidence=s.forward_confidence,
                diagnostics=SSVISliceDiagnostics(
                    maturity=s.time_to_expiry,
                    label=s.expiry.isoformat(),
                    n_observations=s.n_observations,
                    theta=s.theta,
                    atm_volatility=s.atm_volatility,
                    rmse_vol_points=s.rmse_vol_points or 0.0,
                    max_error_vol_points=s.max_error_vol_points or 0.0,
                    k_min=s.k_min if s.k_min is not None else -math.inf,
                    k_max=s.k_max if s.k_max is not None else math.inf,
                    butterfly_first=s.butterfly_first or 0.0,
                    butterfly_second=s.butterfly_second or 0.0,
                    butterfly_bounds_satisfied=s.butterfly_bounds_satisfied,
                    min_durrleman_g=s.min_durrleman_g or 0.0,
                ),
            )
            for s in slice_rows
        )
        return GlobalSurface(
            underlying_id=row.underlying_id,
            as_of=row.as_of_timestamp,
            parameters=parameters,
            term_structure=term_structure,
            slices=slices,
            underlying_price=spot,
            curve_id=row.curve_id,
            analysis_id=row.analysis_id,
            model=row.model,
            model_version=row.model_version,
        )

    async def local_volatility(self, global_surface_row_id: uuid.UUID, user_id: uuid.UUID):
        row = await self.repository.get_global_surface(global_surface_row_id, user_id)
        if row is None:
            return None
        return await self.repository.get_local_volatility_surface(global_surface_row_id)

    async def densities(self, global_surface_row_id: uuid.UUID, user_id: uuid.UUID):
        row = await self.repository.get_global_surface(global_surface_row_id, user_id)
        if row is None:
            return None
        return await self.repository.get_densities(global_surface_row_id)

    async def get_consensus(self, row_id: uuid.UUID, user_id: uuid.UUID):
        row = await self.repository.get_consensus(row_id, user_id)
        if row is None:
            return None
        return row, await self.repository.get_model_values(row.id)

    async def list_consensus_runs(self, user_id: uuid.UUID, limit: int = 50, offset: int = 0):
        return await self.repository.list_consensus_runs(user_id, limit=limit, offset=offset)

    # ------------------------------------------------------------ consensus
    async def price_consensus(
        self,
        user_id: uuid.UUID,
        params: PriceConsensusParams,
        market_data: MarketDataService,
    ):
        instrument = await self.instruments.get(params.instrument_id)
        if instrument is None:
            raise AnalysisError(f"instrument {params.instrument_id} not found")
        if (
            instrument.asset_class is not AssetClass.OPTION
            or instrument.strike is None
            or instrument.expiry is None
            or instrument.option_type is None
            or instrument.underlying_id is None
        ):
            raise AnalysisError(
                "model consensus prices vanilla options; "
                f"{instrument.symbol} is a {instrument.asset_class}"
            )

        loaded = (
            await self.load_global_surface(params.global_surface_row_id, user_id)
            if params.global_surface_row_id is not None
            else await self.latest_global_surface(user_id, instrument.underlying_id)
        )
        warnings: list[AnalyticalWarning] = []
        if loaded is None:
            raise AnalysisError(
                "no global surface is available for this underlying; calibrate one first"
            )
        surface_row, surface = loaded

        reference = surface.reference(instrument.strike, instrument.expiry, instrument.option_type)
        tau = reference.time_to_expiry or surface.time_to_expiry(instrument.expiry)
        spot = surface.underlying_price
        if spot is None or spot <= 0:
            raise AnalysisError("the surface's analysis carries no underlying price")

        heston_row = await self.repository.latest_heston_calibration(
            user_id, instrument.underlying_id
        )
        heston = None
        if heston_row is not None and heston_row.v0 is not None:
            heston = HestonParameters(
                v0=heston_row.v0,
                kappa=heston_row.kappa,
                theta=heston_row.theta,
                xi=heston_row.xi,
                rho=heston_row.rho,
            )
            if heston_row.warnings:
                warnings.append(
                    AnalyticalWarning.info(
                        AdvancedWarningCode.HESTON_CAVEATED,
                        "The Heston parameters below were fitted with caveats: "
                        f"{', '.join(heston_row.warnings)}. They still reproduce the "
                        f"observed surface to {heston_row.rmse_vol_points:.3f} "
                        "volatility points, which is what the price depends on, but "
                        "the parameters should not be read individually.",
                        heston_warnings=list(heston_row.warnings),
                        rmse_vol_points=heston_row.rmse_vol_points,
                        n_maturities=heston_row.n_maturities,
                    )
                )
        else:
            warnings.append(
                AnalyticalWarning.info(
                    AdvancedWarningCode.NO_HESTON,
                    "No Heston calibration exists for this underlying, so that model "
                    "reports itself unavailable rather than being run on invented "
                    "parameters.",
                )
            )

        local_vol = (
            surface.local_volatility(reference.reference_iv)
            if reference.reference_iv is not None
            else None
        )

        market_price = await self._observed_mid(
            user_id, instrument, market_data, surface_row.as_of_timestamp
        )
        if market_price is None:
            warnings.append(
                AnalyticalWarning.info(
                    AdvancedWarningCode.NO_MARKET_PRICE,
                    "There is no two-sided observed price for this contract, so the "
                    "deviation between the models and the market is not reported. It "
                    "is absent rather than zero.",
                )
            )

        inputs = ConsensusInputs(
            spot=spot,
            strike=float(instrument.strike),
            tau=tau,
            rate=params.risk_free_rate,
            dividend=params.dividend_yield,
            is_call=instrument.option_type is OptionType.CALL,
            reference_volatility=reference.reference_iv,
            local_volatility=local_vol,
            heston=heston,
            grid=GridSpec(nodes=params.grid_nodes, steps=params.grid_steps),
            paths=params.paths,
            seed=params.seed,
        )

        models = params.models or None
        service = ModelConsensusService()
        result = (
            service.price(
                inputs,
                models=models,
                market_price=market_price,
                external_contributions=self._external_confidence(surface_row, reference),
            )
            if models
            else service.price(
                inputs,
                market_price=market_price,
                external_contributions=self._external_confidence(surface_row, reference),
            )
        )

        higher_order = (
            bsm_higher_order_greeks(
                spot,
                float(instrument.strike),
                tau,
                params.risk_free_rate,
                params.dividend_yield,
                reference.reference_iv,
                instrument.option_type is OptionType.CALL,
            )
            if reference.reference_iv
            else None
        )

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_timestamp=surface_row.as_of_timestamp,
            surface_id=surface_row.surface_id,
            yield_curve_id=surface_row.curve_id,
            calibration_timestamp=surface_row.calibration_timestamp,
            model_versions={
                "consensus": CONSENSUS_MODEL_VERSION,
                "global_surface": surface_row.model_version,
                "heston": HESTON_MODEL_VERSION if heston else "unavailable",
            },
            parameters=params.to_provenance(),
            numerical_tolerances={
                "grid": {"nodes": params.grid_nodes, "steps": params.grid_steps},
                "paths": params.paths,
                "seed": params.seed,
            },
        )

        consensus_row = await self.repository.create_consensus(
            user_id=user_id,
            global_surface_id=surface_row.id,
            heston_calibration_id=heston_row.id if heston_row is not None else None,
            instrument_id=instrument.id,
            underlying_id=instrument.underlying_id,
            as_of_timestamp=surface_row.as_of_timestamp,
            model_version=CONSENSUS_MODEL_VERSION,
            expiry=instrument.expiry,
            strike=instrument.strike,
            option_type=str(instrument.option_type),
            spot=spot,
            time_to_expiry=tau,
            risk_free_rate=params.risk_free_rate,
            dividend_yield=params.dividend_yield,
            reference_volatility=reference.reference_iv,
            models_requested=result.models_requested,
            models_available=result.models_available,
            reference_value=result.reference_value,
            reference_low=result.reference_range[0] if result.reference_range else None,
            reference_high=result.reference_range[1] if result.reference_range else None,
            dispersion_absolute=result.dispersion_absolute,
            dispersion_relative=result.dispersion_relative,
            standard_deviation=result.standard_deviation,
            market_price=market_price,
            market_deviation=result.market_deviation,
            market_deviation_relative=result.market_deviation_relative,
            confidence=result.confidence.score,
            confidence_contributions=[c.to_dict() for c in result.confidence.contributions],
            vanna=float(np.asarray(higher_order.vanna)) if higher_order else None,
            volga=float(np.asarray(higher_order.volga)) if higher_order else None,
            charm_per_day=float(np.asarray(higher_order.charm_per_day)) if higher_order else None,
            seed=params.seed,
            paths=params.paths,
            grid={"nodes": params.grid_nodes, "steps": params.grid_steps},
            warnings=[w.to_dict() for w in (*warnings, *result.warnings)],
            provenance=provenance.to_dict(),
        )
        await self.repository.add_model_values(consensus_row.id, result.values)

        payload = self._consensus_payload(result, reference, higher_order)
        payload["consensus_row_id"] = str(consensus_row.id)
        payload["global_surface_row_id"] = str(surface_row.id)
        payload["instrument_id"] = str(instrument.id)
        payload["expiry"] = instrument.expiry.isoformat()
        payload["strike"] = format(instrument.strike, "f")
        payload["option_type"] = str(instrument.option_type)

        return (
            AnalyticalResult.ok(payload, provenance, (*warnings, *result.warnings)),
            consensus_row.id,
        )

    @staticmethod
    def _consensus_payload(result: ConsensusResult, reference, higher_order) -> dict:
        payload = result.to_dict()
        payload["reference_point"] = reference.to_dict()
        payload["higher_order_greeks"] = higher_order.to_dict() if higher_order else None
        return payload

    @staticmethod
    def _external_confidence(surface_row, reference) -> tuple[ConfidenceContribution, ...]:
        """What the consensus cannot see from the contract alone."""
        rmse = reference.calibration_rmse_vol_points
        contributions = [
            ConfidenceContribution(
                name="surface_admissibility",
                score=(
                    1.0
                    if surface_row.status == str(CalibrationStatus.CONVERGED)
                    else 0.4
                    if surface_row.status == str(CalibrationStatus.DEGRADED)
                    else 0.0
                ),
                weight=1.5,
                basis=(
                    f"the global surface calibrated to status {surface_row.status}. A "
                    "degraded surface still produces reference values and they are "
                    "still worth less."
                ),
            ),
            ConfidenceContribution(
                name="extrapolation",
                score=0.5 if reference.flags else 1.0,
                weight=1.0,
                basis=(
                    "the reference value carries "
                    + (
                        ", ".join(str(flag) for flag in reference.flags)
                        if reference.flags
                        else "no extrapolation flag"
                    )
                    + "."
                ),
            ),
        ]
        if rmse is not None:
            contributions.append(
                ConfidenceContribution(
                    name="surface_fit",
                    score=max(0.0, min(1.0, 1.0 / (1.0 + (rmse / 1.0) ** 2))),
                    weight=1.0,
                    basis=(
                        f"the nearest fitted expiry has {rmse:.2f} volatility points of "
                        "RMSE, scored against a one-point reference. Every model below "
                        "that reads the surface inherits that error."
                    ),
                )
            )
        return tuple(contributions)

    async def _observed_mid(self, user_id, instrument, market_data: MarketDataService, as_of):
        """The observed two-sided mid at or before the surface's own moment.

        Two rules are doing work here. The window ends at ``as_of`` rather than
        at now, because comparing a model built from one moment's chain against
        a quote from a later one would report the market having moved as a model
        deviation. And the history only ever contains two-sided observations —
        the same contract ``Quote.mid_price`` honours by returning ``None``
        rather than falling back to the last trade — so a one-sided market
        yields an absence rather than a print dressed up as a mid.
        """
        window_start = as_of - timedelta(days=OBSERVED_MID_LOOKBACK_DAYS)
        history = await market_data.instrument_quote_history(
            user_id, instrument.id, window_start, as_of
        )
        if not history:
            return None
        _timestamp, mid, _spread = history[-1]
        return float(mid)
