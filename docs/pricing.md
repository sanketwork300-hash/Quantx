# Pricing — engine design

Status: Black-76 (price, vega, bounds) and Black-Scholes-Merton (price and all
first- and second-order Greeks) are **implemented** (Phase 1). The
`PricingModel` abstraction and the numerical engines are Phase 9. Formulas and
conventions are in `docs/methodology.md`; this document covers engine design.

---

## 1. Interface

```python
class PricingModel(ABC):
    name: str
    version: str
    supported: frozenset[AssetClass]

    def price(self, contract: Contract, market_state: MarketState) -> PricingResult: ...
    def greeks(self, contract: Contract, market_state: MarketState) -> GreeksResult: ...
```

`PricingResult` carries `value`, `model`, `model_version`, `inputs_used`,
`warnings`, `diagnostics` and `provenance` — never a bare float. The bare float
is what makes model risk invisible.

Contracts are described by the canonical `Instrument`; the model reads what it
needs from `MarketState` and reports what it could not find rather than
substituting a default.

## 2. Planned implementations

| Pricer | Phase | Notes |
| --- | --- | --- |
| `bsm_price` / `bsm_greeks` | 1 `[x]` | spot-based with continuous carry; the canonical **Greeks** engine |
| `black76_price` / `black76_vega` | 1 `[x]` | forward-based; the canonical **pricing** parameterization |
| `BinomialPricer` | 9 | American exercise (CRR with Richardson extrapolation) |
| `LocalVolPDEPricer` | 9 | Crank-Nicolson + Rannacher start-up |
| `HestonPricer` | 9 | QuantLib-wrapped, cross-checked |
| `MonteCarloPricer` | 9 | seeded, antithetic + control variates |

## 3. Why Black-76 is the default

Listed option markets quote against a forward that already embeds carry,
dividends and borrow. Using Black-76 on an estimated forward keeps those effects
in **one** explicitly estimated quantity with its own confidence
(`ForwardEstimator`), instead of scattering a guessed dividend yield through
every price and Greek. It also makes the smile coordinate `k = ln(K/F)` fall out
directly.

## 4. Greeks

Analytic wherever a closed form exists. Where it does not, central finite
differences with documented bump sizes; the method used is recorded per Greek so
a consumer knows whether a number is exact or bumped.

Greeks are taken in the **spot** parameterization even though pricing is done on
the forward. A forward-space theta is ambiguous — it depends on whether the
forward and the discount factor are held fixed as time passes — and an ambiguous
theta is worse than none. Black-76 and Black-Scholes-Merton agreeing on price
for the same inputs is asserted as a test, which is what makes the split safe.

Units are always attached (`docs/methodology.md` section 2.3): vega per +1 vol
point, theta per calendar day, rho per basis point. The **raw partials are kept
alongside** the scaled values (`vega_raw_per_unit_vol`, `theta_raw_per_year`),
so nothing downstream has to back out a scaling factor, and `/derivatives/greeks`
returns a `units` block naming each convention in plain language.

Degenerate inputs (`tau <= 0`, `sigma = 0`) collapse to their analytic limits —
delta becomes an indicator, every second-order Greek becomes zero — rather than
producing `nan`.

## 5. Model consensus

`ModelConsensusService` prices one contract with several models over one
`MarketState` and returns `reference_value` (median), `reference_range`,
`model_median`, `model_dispersion`, `market_deviation` and `confidence`, plus the
individual model values. It never returns a single price. If models disagree by
5%, the user must see that they disagree by 5% — that disagreement is the most
honest thing the platform can tell them.

## 6. Failure modes

`OPTION_EXPIRED`, `MISSING_UNDERLYING_PRICE`, `MISSING_CURVE`,
`MISSING_SURFACE`, `EXTRAPOLATED_STRIKE`, `EXTRAPOLATED_MATURITY`,
`CALIBRATION_UNAVAILABLE`, `NUMERICAL_NONCONVERGENCE`. Each degrades one model to
`null` with a warning; the consensus continues with the remaining models and
reports the reduced model count in its confidence.
