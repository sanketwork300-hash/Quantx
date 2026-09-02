"""Central numerical tolerances.

Declared in one place so a test never hides a loosened bound in a magic number,
and so tightening a tolerance is a single reviewable diff. See docs/testing.md.
"""

#: Our closed-form price against an independent analytic evaluation.
PRICE_VS_ANALYTIC_REL = 1e-10
#: Our closed-form price against a reference library (vollib, QuantLib).
PRICE_VS_REFERENCE_ABS = 1e-8
#: Implied volatility round-trip: price(sigma) -> solve -> sigma.
IV_ROUNDTRIP_ABS = 1e-6
#: Analytic Greeks against central finite differences.
GREEK_VS_FD_REL = 1e-5
#: Put-call parity residual on model prices.
PARITY_ABS = 1e-8
#: PDE against closed form (grid dependent; asserted with convergence order).
PDE_VS_CLOSED_FORM_REL = 5e-3
#: Monte Carlo against closed form, in standard errors.
MC_STANDARD_ERRORS = 3.0
#: Quality scores are deterministic arithmetic.
SCORE_ABS = 1e-9
#: Regression golden files.
GOLDEN_SCORE_ABS = 1e-9
