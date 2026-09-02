# Risk Domain

Status: _(Phase 5)_. Formulas in `docs/methodology.md` §§12-13.

---

## 1. Snapshot

```
PortfolioRiskSnapshot
  portfolio_id, market_timestamp, market_state_id
  market_value, pnl
  delta, gamma, vega, theta
  gross_exposure, net_exposure
  var_95, var_99, expected_shortfall
  margin_estimate, margin_buffer
  model_versions, provenance
```

Every snapshot is timestamped and reproducible from its `market_state_id`.

## 2. VaR

Three methods, all available, each labelled with its assumptions:

| Method | Suitable for | Key parameters |
| --- | --- | --- |
| Historical simulation | anything, incl. nonlinear books | lookback, horizon, confidence, alignment policy |
| Parametric | approximately linear exposures only | covariance estimator, window |
| Monte Carlo | nonlinear books, custom factor models | factor model, paths, **seed** |

For nonlinear portfolios, historical and Monte Carlo VaR **fully reprice** under
each scenario. Scaling today's Greeks across a large shock is a linearisation
that fails precisely where risk numbers matter, and a test asserts the two
approaches diverge materially for a large shock on an option book.

Missing data handling (a factor with gaps in the lookback) is an explicit policy
recorded in the result, not an implicit `dropna`.

## 3. Expected shortfall

Always reported with VaR, with the distinction stated in the response payload so
it can be surfaced verbatim in the UI: VaR is a threshold loss, ES is the average
loss conditional on exceeding it.

## 4. Scenarios and stress

```
Scenario: id, name, description, shocks[]
Shock:    risk_factor, shock_type (ABSOLUTE|PERCENTAGE|VOL_POINTS|BASIS_POINTS), shock_value
```

Applying a scenario produces a new immutable `MarketState`; the portfolio is then
fully revalued. Volatility shocks act on the surface, so options are repriced
under the shocked surface rather than bumped by vega.

Result: `scenario_pnl`, `new_portfolio_value`, `new_greeks`, `new_margin`,
`margin_buffer`, `risk_contribution`.

### Historical stress events

The engine supports stored historical scenarios (COVID crash, volatility spikes,
rate and currency shocks). Their shock values are **derived from actual
historical data at import time** and carry the source and date range in their
metadata. Plausible-looking round numbers are not acceptable, per build spec §43.

## 5. Risk contribution

Loss decomposition by instrument, underlying, expiry and asset class, so a stress
number is actionable:

```
Stress loss = 380,000
  NIFTY short puts    54%
  BANKNIFTY futures   24%
  RELIANCE calls      12%
  Other               10%
```

Contributions are computed by re-running the scenario with each group held flat,
so they reflect the actual nonlinear book rather than a linear allocation, and
they are reported with the residual that nonlinearity leaves over.
