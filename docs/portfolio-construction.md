# Portfolio construction

## 1. What real-time Phase 5 delivers

```
Warehouse history (several instruments)
      │
      ▼
covariance estimate            sample, or Ledoit-Wolf shrinkage asked for by name
      │
      ├─ expected returns      supplied, Black-Litterman, or historical (labelled)
      ├─ constraints           budget, bounds, gross, net, groups, turnover
      ▼
POST /portfolio-optimisation/target
      │
      ▼
target portfolio + the risk of that portfolio
```

Five objectives: minimum variance, maximum Sharpe, mean-variance, risk parity
and minimum CVaR. Two of them — minimum variance and risk parity — need **no
return forecast at all**, and that is why they are there.

## 2. The problem is the expected returns, not the covariance

Mean-variance optimisation is an error maximiser. It puts the most weight exactly
where the estimation error in the inputs is largest, because a spuriously high
estimated return is indistinguishable from a real one. Historical sample means
are a famously poor forecast of future means — at any sample length a
practitioner has, the estimation error swamps the signal — so a portfolio built
on them is a portfolio built on noise, and it will look confident about it.

So the platform never invents a forecast:

| Where returns came from | What happens |
| --- | --- |
| `SUPPLIED` | used as given, recorded verbatim |
| `EQUILIBRIUM` | reverse-optimised from a prior portfolio you supplied |
| `BLACK_LITTERMAN` | that equilibrium, moved by your views |
| `HISTORICAL_MEAN` | **only if asked for**, and every result says so |

A return-seeking objective with none of those is **refused**, with a message
naming the three ways to proceed. The refusal is the feature: quietly
substituting sample means would produce a plausible portfolio built on the one
input the method is least able to tolerate error in.

`return_source` is on every result. Two portfolios from the same covariance and
different return sources are different objects, and a report that did not say
which is which would be comparing them as though they were not.

## 3. Black-Litterman: what the platform cannot supply

The model's appeal is that it answers the objection above directly. Rather than
asking for expected returns, it starts from the returns *implied* by a prior
portfolio — `Π = δ Σ w` — and moves them only as far as an explicit view,
weighted by that view's own stated confidence.

Two inputs are required rather than defaulted:

**The prior portfolio.** The textbook prior is market-capitalisation weights, and
the platform holds no market caps. So it is an argument. Equal weights is a
defensible prior and a *different* one, and the result records which was used.

**`δ` and `τ`.** Risk aversion scales the whole equilibrium vector; `τ` sets how
far a view can move it. Neither has a consensus value — `τ` is quoted anywhere
from 0.01 to 1 in the literature — so both are supplied and both are recorded.

The view uncertainty matrix `Ω` is diagonal. The alternative asks a user to state
how their views correlate, which nobody can, and filling it in for them would be
the platform inventing an opinion.

The posterior covariance is `Σ + Σ_posterior_mean`, not `Σ`. The estimation
uncertainty in the mean adds to the covariance of returns, and conflating the two
understates portfolio risk.

## 4. CVaR, and what a scenario sample can support

`MINIMUM_CVAR` solves the Rockafellar–Uryasev linear program over a return
sample. It assumes no distribution, which is the point: mean-variance optimises a
symmetric risk measure over returns that are not symmetric, and an option book's
returns are about as asymmetric as they come.

The catch is reported rather than hidden. CVaR at 95% from 100 scenarios is an
average over five points, and five points do not describe a tail. Both the
scenario count and the **tail** count are on the result, and a thin tail is
flagged with the same threshold the historical VaR estimator uses.

Gross-exposure and turnover limits are expressed with auxiliary variables so the
program stays linear, rather than being dropped because they involve an absolute
value.

## 5. Constraints, and infeasibility that names itself

Budget, long-only, per-asset bounds, gross exposure, net exposure, group (sector)
limits, and turnover against a stated current portfolio.

**Nothing is defaulted to a plausible limit.** A portfolio with no stated
gross-exposure limit has none; inventing 1.0 because it is the common case would
silently change what was asked for. A turnover limit with no `current_weights` is
**refused**, because turnover from nowhere is not a quantity.

When constraints contradict, the answer names which ones. "No solution" is a
useless message when six limits are in play, and the guess a user makes about
which one is wrong is usually the wrong guess. The pre-check catches what can be
seen without solving — minimum weights summing above the budget, a budget outside
the gross limit, a per-asset floor above its own ceiling — and reports all of them
together.

`binding_constraints` on every result says which limits the solution is sitting
on. A user looking at an odd portfolio usually wants to know what was preventing
something else.

## 6. Two diagnostics worth more than the weights

**Risk contributions.** Each asset's share of portfolio variance,
`w_i (Σw)_i / w'Σw`, summing to one. A holding with 5% of the weight and 40% of
the risk is the portfolio's real position, whatever the weights say.

**Effective assets.** The inverse Herfindahl of the absolute weights: how many
assets the portfolio is genuinely spread across. A twenty-name book with an
effective count of two holds one bet in twenty disguises.

Both are on every result, next to the weights, because the weights on their own
routinely mislead about both.

## 7. Shrinkage is asked for, never applied quietly

The sample covariance is noisy when observations are not many times the number of
assets, and the noise lands hardest on the smallest eigenvalues — which is
exactly where an optimiser goes looking for its cleverest trades.

Ledoit–Wolf shrinkage towards a constant-correlation target is available, with
the intensity derived from the data rather than tuned. It is **not** the default:
the platform's standing rule is that a regularisation which makes a matrix
invertible is a modelling decision the user should see, so it is requested by
name and the intensity it chose is reported. An intensity above 0.5 is warned
about — it means the sample said very little.

## 8. What is deliberately not here

**A default risk aversion.** It is a statement about a person's tolerance, not a
property of the market. Picking one would be choosing a portfolio on the user's
behalf.

**Market-cap weights.** The platform holds no market caps and will not
approximate them.

**Margin and liquidity constraints.** The build spec lists both. Margin needs a
broker's formula, which build spec 1.1 forbids inventing, and liquidity needs
volume data joined to a participation assumption. Each is a real constraint that
would be wrong if guessed, so neither is offered rather than being offered badly.

**Turning strategy signals into expected returns automatically.** A signal says
"hold 40% long"; it does not say what return is expected. `views_from_signals`
exists and requires a stated `return_scale` — what a full-weight signal is worth —
because a platform that picked one would be inventing the view rather than
translating it.
