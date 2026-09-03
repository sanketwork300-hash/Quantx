"""Portfolio risk: Value at Risk, Expected Shortfall and scenario stress.

Historical and Monte Carlo VaR fully reprice the book under every scenario;
parametric does not and says so. Stress applies a scenario from
``domains.scenarios`` and revalues, reporting the Greek approximation of the same
move beside the answer so the size of the linearisation is visible.

See docs/risk.md for the design and docs/methodology.md §§12-13 for the formulas.
"""
