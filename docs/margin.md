# Margin Engine

Status: **implemented** (Phase 6). Methodology in `docs/methodology.md` §14.
Lives in `domains/risk/margin.py` and `domains/risk/vulnerability.py`; there is
no separate margin domain, because margin is a measurement of a portfolio's
risk and belongs beside the others.

---

## 1. The constraint that shapes everything here

Exchange and broker margin methodologies are proprietary, versioned, and change
without notice. **The platform does not know your broker's margin.** Any design
that pretends otherwise produces confidently wrong numbers about the one quantity
that can force a user out of a position.

So every margin output is an explicitly labelled approximation:

```
MarginResult
  method              "SimpleRiskMarginModel@1.0.0"
  model_version
  estimated_margin    the only number, and its name is the claim
  currency
  components[]        each with the basis it was computed on
  assumptions[]       every stated assumption, in plain language
  confidence
  worst_case          the grid point that produced it, and whether that
                      point sat on the grid's own boundary
  warnings[]
  disclaimer
```

There is no field that could be read as "your broker will require X". A test
scans the whole serialised payload for venue names and for affirmative claims,
and a second test allows the word "liquidation" to appear **only** when preceded
by "not a broker" — because the sentence doing the most work here is the one
that denies it.

### The defaults refuse to invent

`short_option_minimum_rate` and `concentration_add_on_rate` both default to
**zero**. A short option far out of the money shows almost no loss on a scan
grid while carrying unbounded tail risk, and a real margin system floors it for
exactly that reason — but the *rate* at which it does is a venue's rule.
Choosing a plausible-looking 2% here would manufacture the kind of number this
whole document exists to prevent.

So the default is zero, the response carries `MARGIN_NO_SHORT_OPTION_MINIMUM`,
and the warning says in words what the zero leaves out. A user who has their
venue's rule can supply it; a user who does not gets an estimate that is honest
about being a lower bound rather than one that is quietly wrong.

## 2. Interface

```python
class MarginModel(ABC):
    name: str
    version: str

    def calculate(self, portfolio, market_state) -> MarginResult: ...
```

`GET /margin/models` lists every implemented model with
`is_broker_equivalent: false`. That field would only ever be true for a model
implementing a *published* methodology, and nothing here does.

| Model | Phase | Status |
| --- | --- | --- |
| `SimpleRiskMarginModel` | 6 | **implemented** — worst loss over a declared shock grid, floored by an optional short-option minimum, plus an optional concentration add-on |
| `SPANApproximation` | later | approximation of a *published* methodology, named as an approximation |
| `CryptoCrossMarginModel` / `CryptoIsolatedMarginModel` | later | venue rules are published for some venues; only implemented where they are |
| `BrokerApproximationModel` | later | **only** where an official methodology or API is available, and then it is no longer an approximation |

## 2a. The model

    scan_loss     = max(0, -min P&L over the declared grid)
    floor         = rate x sum(notional of short options)
    concentration = rate x max(0, largest_underlying_gross - threshold x gross)
    margin        = max(scan_loss, floor) + concentration

The floor is a **floor** rather than an addition because that is what it is for:
a book whose scan loss is small only because every short option is far out of
the money is not a book with no risk. The concentration charge is an **addition**
because it says something different — about how little a grid of
single-underlying shocks reveals when the whole book is one name.

Every grid point is a genuine repricing of every position, vectorised across the
grid using the same machinery Phase 5's Monte Carlo uses.

**The grid is declared, because the grid is the model.** A margin number is the
worst loss over the moves someone chose to look at, and a reader who cannot see
those moves cannot judge the number. It travels in `parameters.grid` on every
result, and a grid without an unshocked point is refused — without one it cannot
show that the book is flat where the market actually is.

### Confidence

A weighted geometric mean of three sub-scores, so one bad dimension pulls the
whole score down rather than being averaged away — the same aggregation the data
quality engine uses:

| Sub-score | Meaning |
| --- | --- |
| coverage | repriceable positions over all positions |
| grid containment | 1.0 when the worst point is interior, 0.5 when it sat on a boundary the grid could be widened past |
| mark consistency | how closely the model at today's anchors reproduces what the book is marked at |

A short-option book's loss grows without bound, so *no* grid contains its worst
case and the containment score is permanently 0.5. That is not noise: a
scan-grid margin for an unbounded-loss book genuinely is a lower bound, and the
score says so. An axis with a single point is not counted as a boundary — there
is no wider setting of it to explore, and a flat volatility axis has its own
warning.

## 3. Utilisation and buffer

```
utilisation = estimated_margin / eligible_capital
buffer      = eligible_capital - estimated_margin
```

`eligible_capital` is user-supplied. When it is unknown, utilisation and buffer
are `null` with a warning — not defaulted to portfolio value, which would be a
different and usually wrong quantity. The database enforces it too:
`ck_margin_buffer_requires_capital` refuses a row with a buffer and no capital.

## 4. Margin vulnerability

A ladder scan, not a price:

```
move:   -25% ... -2%  -1%   0%   +1%  +2% ... +25%
        portfolio value / available capital / estimated margin / buffer at each
```

At **every** rung the book is fully repriced *and* the margin model is run again
on the moved market, because both sides of the buffer change as the market
moves: the portfolio is worth less and the requirement is larger. A curve that
moved only one of them would be flattering.

The ladder runs in **both directions**. A short-call book is short the upside,
and scanning only downwards would miss it entirely.

Available capital is the stated capital plus the mark-to-model change in the
portfolio. That assumes no cash is added, withdrawn or called for, and no
position is closed — stated as an assumption on every result, because all three
are things that happen.

The output is the **region** where `buffer <= 0`:

> Under `SimpleRiskMarginModel` with a +5 vol-point co-shock and stated capital
> of 1,000,000, the portfolio enters an estimated margin-shortfall region after
> an approximately 8.4% adverse move in NIFTY. This is a model estimate, not a
> broker liquidation level.

The crossing is interpolated between the two rungs that bracket it, and those
two rungs are reported alongside it. A crossing located between -8% and -10% is
a different statement from one located between -8.4% and -8.5%, and a reader
who is shown only the interpolated number cannot tell which they have.

Three outcomes, each with its own sentence:

| State | What is said |
| --- | --- |
| A crossing is found | "…enters an estimated margin-shortfall region after an approximately X% adverse move… This is a model estimate, not a broker liquidation level." |
| Already inside at rest | "…is already inside an estimated margin-shortfall region…", naming the volatility co-shock when that alone put it there |
| No crossing on the ladder | "…does not enter… anywhere within N% either way. It may do so beyond that range." |

The UI shows the buffer curve with the estimated-shortfall band shaded and **no
liquidation marker**, and says so in the caption. That would only change if the
number came from an official exchange or broker methodology.
