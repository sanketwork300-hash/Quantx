"""Built-in scenario templates, and derivation of scenarios from real history.

The templates are **hypothetical**. Their numbers are round because they were
chosen to be round, and each one says so in its own description. That is a
deliberate refusal: naming a template "COVID crash" and putting -35% in it would
assert a fact about March 2020 that nobody here measured, and a user would
reasonably read the number as history rather than illustration.

To get a scenario that is genuinely historical, derive one with
``derive_from_returns`` against a series the platform actually holds. The result
carries the series, its date range, and the date of the move it reproduces.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

import numpy as np

from domains.scenarios.models import (
    HistoricalDerivation,
    RiskFactorKind,
    Scenario,
    ScenarioError,
    ScenarioSource,
    Shock,
    ShockType,
)

#: uuid5 namespace for template ids, so a template has the same id in every
#: installation and a stored result can name the template it used.
TEMPLATE_NAMESPACE = uuid.UUID("6b6a1a4e-6f5a-5c39-9b0a-2f5d3f8a7c11")

HYPOTHETICAL_NOTE = (
    "A round-number hypothetical chosen for illustration. It is not a historical "
    "event and makes no claim about any market's past."
)


def _template(name: str, description: str, shocks: tuple[Shock, ...]) -> Scenario:
    return Scenario(
        id=uuid.uuid5(TEMPLATE_NAMESPACE, name),
        name=name,
        description=f"{description} {HYPOTHETICAL_NOTE}",
        shocks=shocks,
        source=ScenarioSource.HYPOTHETICAL,
    )


def templates() -> tuple[Scenario, ...]:
    """The shipped templates. Every one is a hypothetical, by construction."""
    return (
        _template(
            "Underlying -5%",
            "A single directional move in the underlying, volatility unchanged.",
            (Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, -0.05),),
        ),
        _template(
            "Underlying -10%",
            "A larger directional move, still with volatility unchanged. Useful "
            "against the Greek approximation, which starts to visibly disagree here.",
            (Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, -0.10),),
        ),
        _template(
            "Underlying +10%",
            "The upside counterpart, because a short-gamma book is not symmetric.",
            (Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, 0.10),),
        ),
        _template(
            "Volatility +5 points",
            "A pure volatility move with the underlying unchanged.",
            (Shock(RiskFactorKind.VOLATILITY, ShockType.VOL_POINTS, 0.05),),
        ),
        _template(
            "Volatility -5 points",
            "A volatility collapse with the underlying unchanged.",
            (Shock(RiskFactorKind.VOLATILITY, ShockType.VOL_POINTS, -0.05),),
        ),
        _template(
            "Sell-off with volatility spike",
            "Down and up together, which is how equity index volatility usually "
            "moves and what a delta hedge alone does not protect against.",
            (
                Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, -0.08),
                Shock(RiskFactorKind.VOLATILITY, ShockType.VOL_POINTS, 0.08),
            ),
        ),
        _template(
            "Rally with volatility fall",
            "Up and down together.",
            (
                Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, 0.08),
                Shock(RiskFactorKind.VOLATILITY, ShockType.VOL_POINTS, -0.04),
            ),
        ),
        _template(
            "Rates +100 bp",
            "A parallel move in the discount rate.",
            (Shock(RiskFactorKind.RISK_FREE_RATE, ShockType.BASIS_POINTS, 100.0),),
        ),
    )


def template_by_name(name: str) -> Scenario | None:
    return next((s for s in templates() if s.name == name), None)


def derive_from_returns(
    name: str,
    dates: Sequence[date],
    prices: Sequence[float],
    series_label: str,
    window_days: int = 1,
    target: str | None = None,
    percentile: float | None = None,
    volatility_dates: Sequence[date] | None = None,
    volatility_levels: Sequence[float] | None = None,
) -> Scenario:
    """Build a scenario from the worst move a real series actually contains.

    ``percentile`` selects a quantile of the return distribution instead of the
    single worst move; ``None`` means the worst. Either way the scenario names
    the date it came from, so the number can be checked against the series.

    When a volatility series is supplied, the volatility move **observed on the
    same date** is added as a second shock. It is not modelled, assumed, or
    scaled from the price move: it is what that series did on that day, or the
    shock is simply omitted and the caller told.
    """
    if len(dates) != len(prices):
        raise ScenarioError("dates and prices must be the same length")
    if len(prices) < 2:
        raise ScenarioError(
            "deriving a scenario from history needs at least two observations; "
            "a scenario cannot be invented from a series that has no moves in it"
        )

    order = np.argsort(np.asarray([d.toordinal() for d in dates]))
    ordered_dates = [dates[i] for i in order]
    ordered_prices = np.asarray([prices[i] for i in order], dtype=float)
    if np.any(ordered_prices <= 0):
        raise ScenarioError("a price series for a return calculation must be positive")

    step = max(1, int(window_days))
    if ordered_prices.size <= step:
        raise ScenarioError(
            f"a {step}-day window needs more than {step} observations, "
            f"and the series has {ordered_prices.size}"
        )

    returns = ordered_prices[step:] / ordered_prices[:-step] - 1.0
    if percentile is None:
        index = int(np.argmin(returns))
        method = f"worst {step}-day return in the series"
    else:
        if not 0.0 < percentile < 1.0:
            raise ScenarioError("percentile must be in (0, 1)")
        target_value = float(np.quantile(returns, percentile, method="linear"))
        index = int(np.argmin(np.abs(returns - target_value)))
        method = f"{percentile:.1%} quantile of {step}-day returns, nearest observation"

    event_date = ordered_dates[index + step]
    shocks: list[Shock] = [
        Shock(
            RiskFactorKind.UNDERLYING_PRICE,
            ShockType.PERCENTAGE,
            float(returns[index]),
            target=target,
        )
    ]

    if volatility_dates and volatility_levels:
        move = _volatility_move_on(
            volatility_dates, volatility_levels, ordered_dates[index], event_date
        )
        if move is not None:
            shocks.append(
                Shock(RiskFactorKind.VOLATILITY, ShockType.VOL_POINTS, move, target=target)
            )

    return Scenario(
        id=uuid.uuid4(),
        name=name,
        description=(
            f"Derived from {series_label}: the {method}, which occurred on "
            f"{event_date.isoformat()}."
        ),
        shocks=tuple(shocks),
        source=ScenarioSource.DERIVED_FROM_HISTORY,
        derivation=HistoricalDerivation(
            series=series_label,
            observations=int(ordered_prices.size),
            start_date=ordered_dates[0],
            end_date=ordered_dates[-1],
            event_date=event_date,
            window_days=step,
            method=method,
        ),
    )


def _volatility_move_on(
    dates: Sequence[date], levels: Sequence[float], start: date, end: date
) -> float | None:
    """The volatility change actually observed between two dates, or nothing."""
    lookup = dict(zip(dates, levels, strict=False))
    first, second = lookup.get(start), lookup.get(end)
    if first is None or second is None:
        return None
    return float(second - first)
