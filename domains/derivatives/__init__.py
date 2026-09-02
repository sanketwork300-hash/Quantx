"""Derivatives intelligence.

Phase 1: time-to-expiry policy, forward estimation, implied volatility, Greeks
and the raw smile. Phase 2 adds SVI calibration and arbitrage diagnostics on top
of these; see docs/backlog.md.
"""

from domains.derivatives.application import AnalysisError, AnalyzeChainParams, DerivativesService
from domains.derivatives.forward import ForwardEstimate, ForwardEstimator, ForwardMethod
from domains.derivatives.models import ChainAnalysis, ImpliedVolPoint, PriceSource, SmileSlice
from domains.derivatives.service import ChainAnalysisRequest, ChainAnalysisService, QuoteInput
from domains.derivatives.timeconv import ExpiryPolicy, time_to_expiry

__all__ = [
    "AnalysisError",
    "AnalyzeChainParams",
    "ChainAnalysis",
    "ChainAnalysisRequest",
    "ChainAnalysisService",
    "DerivativesService",
    "ExpiryPolicy",
    "ForwardEstimate",
    "ForwardEstimator",
    "ForwardMethod",
    "ImpliedVolPoint",
    "PriceSource",
    "QuoteInput",
    "SmileSlice",
    "time_to_expiry",
]
