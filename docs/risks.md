# Technical and Data Risks

Ordered by expected damage. Each risk names the mitigation that is actually in
the design, not an aspiration.

---

## R1. Market data availability is the binding constraint  — **HIGH**

Retail-accessible option-chain, tick and L2 data is limited, licence-encumbered,
and often prohibited from redistribution. The platform can be architecturally
perfect and still have nothing to compute on.

**Mitigation.** Three first-class modes from day one (build spec §8): provider
mode, user-data mode (upload your own CSV/Parquet), and research mode (import a
historical dataset). `SyntheticMarketDataProvider` makes the entire platform —
including demos, CI and tutorials — functional with zero external data. No
feature may be built such that it only works in provider mode.

**Residual.** Anomaly z-scores and impact-model training need *history*, which
synthetic data cannot substitute for. Phases 3, 10 and the ML impact model are
explicitly gated on real historical data existing.

## R2. Silent instrument mis-resolution  — **HIGH**

One symbol mapped to the wrong contract corrupts portfolio netting, Greeks,
margin and TCA at once, and nothing throws.

**Mitigation.** Deterministic uuid5 identity from a canonical key; a resolver
that returns `AMBIGUOUS` as a first-class outcome and never picks a "best"
candidate; import previews; unresolved/ambiguous rows blocked from committing.

## R3. Fabricated financial semantics  — **HIGH**

Guessed multipliers, invented margin formulas, assumed expiry calendars,
plausible-looking broker rules. The most dangerous failure mode because the
output looks authoritative.

**Mitigation.** Build spec §1.1 as a hard rule. Multipliers/lot sizes come from
data or are flagged `MULTIPLIER_ASSUMED`. Margin models state method,
assumptions and confidence, and no output claims broker equivalence. Exchange
calendars come from QuantLib rather than hand-written holiday lists. Product
language is constrained (`docs/methodology.md` §"Language").

## R4. Model risk presented as precision  — **HIGH**

A reference value printed to two decimals reads as truth. Users act on it.

**Mitigation.** Model consensus returns a *range* plus dispersion, never a point
estimate alone. Confidence is computed from calibration error, model
disagreement, spread, liquidity, extrapolation, data quality and quote age.
Explanations are grounded in those metrics. The anomaly vocabulary excludes
"underpriced" / "arbitrage" by policy, checked in review.

**Status: implemented in Phase 9, and enforced rather than reviewed.** The
consensus payload has no `best_model`, `fair_value` or `true_price` field, a
test scans every key at every depth for one, and the stored row has no column
that could hold it. Every confidence contribution carries the basis that
produced it, so "why is this 0.42?" is answerable from the response. The
`interpretation` field says in the response itself that the median is not a
price the contract is worth and that the spread is a statement about model risk
rather than about the market.

**Residual risk.** The four models are not independent — three of them read the
same fitted surface — so a narrow dispersion is weaker evidence than it looks.
That is stated in `docs/references.md` and is why the individual model values
are shown alongside the range rather than only summarised by it.

## R5. Timestamp inconsistency across a calculation  — **MEDIUM-HIGH**

Mixing a 09:15 delta with a 09:47 vega produces a risk number that never existed.

**Mitigation.** `MarketState` is immutable and content-addressed; all five
branches of order analysis share one `state_id`; provenance records it.

## R6. Numerical failure on real-world data  — **MEDIUM-HIGH**

IV solvers diverge on sub-intrinsic quotes; SVI calibration falls into local
minima on sparse or crossed smiles; Dupire blows up on noisy quotes.

**Mitigation.** Bracketed root finding with explicit bounds and a convergence
report; multi-start constrained calibration with recorded optimizer status;
local volatility only from a smooth arbitrage-aware surface, never from raw
quotes; invalid regions returned as invalid rather than clipped silently.
Failures are `PARTIAL` results with named warning codes, not exceptions.

## R7. Data-volume blowup  — **MEDIUM**

L2 histories and Monte Carlo paths will destroy a relational database.

**Mitigation.** Object store + Parquet + partitioning for anything that grows
with market activity; Postgres holds pointers. Rule of thumb stated in
`docs/database.md` §5.

## R8. Long calculations blocking the API  — **MEDIUM**

Monte Carlo, full historical repricing, surface backfills.

**Mitigation.** Job system from Phase 0, before any long calculation exists.
Anything with p95 > ~1s becomes a job type; the API returns `202 + job_id`.

## R9. Reproducibility decay  — **MEDIUM**

A six-month-old analysis cannot be reproduced because a formula, a default or a
library version changed underneath it.

**Mitigation.** Model registry with versions and code commit; provenance on every
persisted result; regression golden files that fail CI on drift; append-only
observation tables.

## R10. Over-fitting the ML impact model  — **MEDIUM**

Random splits on temporally ordered execution data leak the future and produce
excellent offline metrics and useless live estimates.

**Mitigation.** Time-series splits only; evaluation bucketed by order size;
baseline square-root model must be beaten out-of-time before an ML model ships.
Explicit gate in the Phase 8/backlog criteria.

## R11. Security of user financial data  — **MEDIUM**

Portfolios and trade logs are sensitive and identifying.

**Mitigation.** Ownership checks on every route (404 not 403), bcrypt, short JWT
TTLs, upload hardening (size/MIME/row caps, sanitized server-side names, no
spreadsheet formula evaluation, parse-after-store), rate limits, audit logging,
TLS at the proxy.

## R12. Scope collapse into a calculator collection  — **MEDIUM**

Fifteen half-finished modules that never integrate: the failure mode this
project is most likely to actually die of.

**Mitigation.** Strict phase gating with acceptance criteria; vertical slices
only; `docs/backlog.md` is the contract. Phase 11 (the integration) is the point
of the product, and every earlier phase is judged by whether it feeds it.

## R13. Dependency and licence risk  — **LOW-MEDIUM**

Copying GPL/AGPL implementations into an Apache-2.0 codebase.

**Mitigation.** `docs/references.md` records, per feature, the licence, the
version, and an explicit `USE / WRAP / VALIDATE AGAINST / ADAPT CONCEPTS /
IMPLEMENT INDEPENDENTLY` decision. QuantLib and vollib are used as **test
oracles** by default, which avoids derivative-work questions entirely for the
core numerics.

## R14. Python 3.14 / dependency drift  — **LOW**

The development environment runs 3.14 while the container targets 3.12.

**Mitigation.** `requires-python = ">=3.12"`, no version-specific syntax, CI
matrix over both, and containerized runtime as the source of truth.
