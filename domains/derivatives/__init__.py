"""Derivatives intelligence.

Phase 1: time-to-expiry policy, forward estimation, implied volatility, Greeks
and the raw smile. Phase 2 adds SVI calibration and arbitrage diagnostics on
top of these. Phase 9 adds the SSVI global surface, Dupire local volatility,
the implied density, Heston, and a model consensus that never returns a single
price; see docs/backlog.md.
"""

from domains.derivatives.advanced import (
    AdvancedDerivativesService,
    CalibrateGlobalSurfaceParams,
    PriceConsensusParams,
)
from domains.derivatives.application import AnalysisError, AnalyzeChainParams, DerivativesService
from domains.derivatives.consensus import (
    ConsensusInputs,
    ConsensusResult,
    ModelConsensusService,
    PricingModel,
    PricingModelKind,
)
from domains.derivatives.forward import ForwardEstimate, ForwardEstimator, ForwardMethod
from domains.derivatives.global_surface import (
    GlobalSurface,
    GlobalSurfaceCalibrationService,
)
from domains.derivatives.models import ChainAnalysis, ImpliedVolPoint, PriceSource, SmileSlice
from domains.derivatives.service import ChainAnalysisRequest, ChainAnalysisService, QuoteInput
from domains.derivatives.timeconv import ExpiryPolicy, time_to_expiry

__all__ = [
    "AdvancedDerivativesService",
    "AnalysisError",
    "AnalyzeChainParams",
    "CalibrateGlobalSurfaceParams",
    "ChainAnalysis",
    "ChainAnalysisRequest",
    "ChainAnalysisService",
    "ConsensusInputs",
    "ConsensusResult",
    "DerivativesService",
    "ExpiryPolicy",
    "ForwardEstimate",
    "ForwardEstimator",
    "ForwardMethod",
    "GlobalSurface",
    "GlobalSurfaceCalibrationService",
    "ImpliedVolPoint",
    "ModelConsensusService",
    "PriceConsensusParams",
    "PriceSource",
    "PricingModel",
    "PricingModelKind",
    "QuoteInput",
    "SmileSlice",
    "time_to_expiry",
]
