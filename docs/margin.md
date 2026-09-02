# Margin Engine

Status: _(Phase 6)_. Methodology in `docs/methodology.md` §14.

---

## 1. The constraint that shapes everything here

Exchange and broker margin methodologies are proprietary, versioned, and change
without notice. **The platform does not know your broker's margin.** Any design
that pretends otherwise produces confidently wrong numbers about the one quantity
that can force a user out of a position.

So every margin output is an explicitly labelled approximation:

```
MarginResult
  method              e.g. "SimpleRiskMarginModel@1.0.0"
  assumptions[]       every stated assumption, in plain language
  estimated_margin
  confidence
  warnings[]
  provenance
```

There is no field that could be read as "your broker will require X".

## 2. Interface

```python
class MarginModel(ABC):
    name: str
    version: str

    def calculate(self, portfolio, market_state) -> MarginResult: ...
```

| Model | Phase | Status |
| --- | --- | --- |
| `SimpleRiskMarginModel` | 6 | worst loss over a declared shock grid + short-option minimum + concentration add-on |
| `SPANApproximation` | later | approximation of a *published* methodology, named as an approximation |
| `CryptoCrossMarginModel` / `CryptoIsolatedMarginModel` | later | venue rules are published for some venues; only implemented where they are |
| `BrokerApproximationModel` | later | **only** where an official methodology or API is available, and then it is no longer an approximation |

## 3. Utilisation and buffer

```
utilisation = margin_required / eligible_capital
buffer      = eligible_capital - margin_required
```

`eligible_capital` is user-supplied. When it is unknown, utilisation is `null`
with a warning — not defaulted to portfolio value, which would be a different and
usually wrong quantity.

## 4. Liquidation vulnerability

A shock-grid scan, not a price:

```
shock:   0%   -1%   -2%  ...  -20%
         equity / margin requirement / available capital / buffer at each point
```

The output is the **region** where `buffer <= 0`:

> Under `SimpleRiskMarginModel` with a +5 vol-point co-shock and stated capital
> of 1,000,000, the portfolio enters an estimated margin-shortfall region after
> an approximately 8.4% adverse move in NIFTY. This is a model estimate, not a
> broker liquidation level.

The UI shows the buffer curve with comfortable / warning / estimated-shortfall
bands and no guaranteed-liquidation marker, unless and until the number comes
from an official exchange or broker model.
