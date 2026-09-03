"""Implementation shortfall and the model-based cost decomposition.

Two things this module refuses to do.

It will not report a shortfall against a benchmark that is not available. A
missing benchmark produces a missing shortfall with the benchmark's own reason
attached, not a zero and not a substitution.

And it will not present the decomposition as a measurement. Spread, impact and
timing are not separately observable. What is measured is the total; what is
modelled is the spread charge; what is left over is called timing and the
response says in words that it is a residual carrying everything not separated
out — including market impact, which this phase does not model at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from domains.execution.benchmarks import (
    Benchmark,
    BenchmarkKind,
    MarketWindow,
)
from domains.execution.models import ParentOrder

#: One basis point of one, as a Decimal, so the conversion never goes via float.
BPS = Decimal(10_000)


class CostComponentStatus(StrEnum):
    MEASURED = "MEASURED"
    MODELLED = "MODELLED"
    RESIDUAL = "RESIDUAL"
    NOT_MODELLED = "NOT_MODELLED"
    UNAVAILABLE = "UNAVAILABLE"


class TCAWarning(StrEnum):
    NO_BENCHMARK = "TCA_NO_BENCHMARK_AVAILABLE"
    ARRIVAL_PROXY = "ARRIVAL_PROXY_USED"
    NO_SPREAD_DATA = "TCA_NO_SPREAD_DATA"
    IMPACT_NOT_MODELLED = "TCA_IMPACT_NOT_MODELLED"
    NO_ORDER_QUANTITY = "TCA_NO_ORDER_QUANTITY"
    INFERRED_GROUPING = "TCA_INFERRED_PARENT_GROUPING"
    DATA_COVERAGE_LOW = "TCA_DATA_COVERAGE_LOW"


@dataclass(frozen=True, slots=True)
class Shortfall:
    """Cost against one benchmark, in the three units people ask for.

    Positive is always a cost: paying above the benchmark on a buy and receiving
    below it on a sell are the same thing, and the side's sign is applied once,
    here, so that they read the same way.
    """

    benchmark: BenchmarkKind
    benchmark_price: Decimal
    average_price: Decimal
    currency_amount: Decimal
    basis_points: float
    percent: float
    currency: str
    quantity: Decimal
    multiplier: Decimal

    def to_dict(self) -> dict:
        return {
            "benchmark": str(self.benchmark),
            "benchmark_price": format(self.benchmark_price, "f"),
            "average_price": format(self.average_price, "f"),
            "currency_amount": format(self.currency_amount, "f"),
            "basis_points": self.basis_points,
            "percent": self.percent,
            "currency": self.currency,
            "quantity": format(self.quantity, "f"),
            "multiplier": format(self.multiplier, "f"),
            "convention": (
                "Positive is a cost. For a buy that is paying above the "
                "benchmark; for a sell it is receiving below it."
            ),
        }


@dataclass(frozen=True, slots=True)
class UnavailableShortfall:
    benchmark: BenchmarkKind
    reason: str

    def to_dict(self) -> dict:
        return {
            "benchmark": str(self.benchmark),
            "available": False,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CostComponent:
    name: str
    amount: Decimal | None
    status: CostComponentStatus
    #: What this number is and where it came from, in a sentence.
    basis: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "amount": format(self.amount, "f") if self.amount is not None else None,
            "status": str(self.status),
            "basis": self.basis,
        }


@dataclass(frozen=True, slots=True)
class CostDecomposition:
    """A split of the measured cost, labelled by what each part actually is."""

    benchmark: BenchmarkKind
    total: Decimal
    components: tuple[CostComponent, ...]
    currency: str

    def to_dict(self) -> dict:
        return {
            "benchmark": str(self.benchmark),
            "total": format(self.total, "f"),
            "currency": self.currency,
            "components": [item.to_dict() for item in self.components],
            "caveat": (
                "Model-based, not a measurement. Spread, impact and timing are "
                "not separately observable: the total is measured, the spread "
                "charge is modelled from the quoted spread at each fill, fees are "
                "observed, and timing is what is left over — including market "
                "impact, which this phase does not model."
            ),
        }


@dataclass(frozen=True, slots=True)
class ExecutionAnalysis:
    """One parent order, benchmarked and decomposed."""

    parent: ParentOrder
    benchmarks: tuple[Benchmark, ...]
    shortfalls: tuple[Shortfall, ...]
    unavailable: tuple[UnavailableShortfall, ...]
    decomposition: CostDecomposition | None
    window: MarketWindow
    primary: BenchmarkKind
    warnings: tuple[str, ...] = ()

    @property
    def primary_shortfall(self) -> Shortfall | None:
        return next((item for item in self.shortfalls if item.benchmark is self.primary), None)

    def to_dict(self, include_observations: bool = False) -> dict:
        return {
            "parent_order": self.parent.to_dict(),
            "primary_benchmark": str(self.primary),
            "benchmarks": [item.to_dict() for item in self.benchmarks],
            "shortfalls": [item.to_dict() for item in self.shortfalls],
            "unavailable_shortfalls": [item.to_dict() for item in self.unavailable],
            "decomposition": self.decomposition.to_dict() if self.decomposition else None,
            "market_window": self.window.to_dict(include_observations=include_observations),
            "warnings": list(self.warnings),
        }


def implementation_shortfall(parent: ParentOrder, benchmark: Benchmark) -> Shortfall | None:
    """Cost of this order against one benchmark, or nothing when it has no price."""
    if benchmark.price is None or benchmark.price == 0:
        return None

    average = parent.average_price
    difference = (average - benchmark.price) * parent.side.sign
    quantity = parent.filled_quantity
    amount = difference * quantity * parent.multiplier
    fraction = difference / benchmark.price

    return Shortfall(
        benchmark=benchmark.kind,
        benchmark_price=benchmark.price,
        average_price=average,
        currency_amount=amount,
        basis_points=float(fraction * BPS),
        percent=float(fraction * 100),
        currency=parent.currency,
        quantity=quantity,
        multiplier=parent.multiplier,
    )


def spread_cost(parent: ParentOrder, window: MarketWindow) -> tuple[Decimal | None, str, int]:
    """Half the quoted spread at each fill, weighted by that fill's quantity.

    Half, because crossing a two-sided market from the mid costs half of it.
    Fills for which the platform holds no two-sided quote contribute nothing and
    are counted, so a decomposition built from two fills out of twenty is
    visible as such.
    """
    total = Decimal(0)
    covered = 0
    for execution in parent.ordered:
        observation, _age = window.at(execution.exchange_timestamp)
        if observation is None or observation.spread is None:
            continue
        covered += 1
        total += (observation.spread / 2) * execution.quantity * parent.multiplier

    if covered == 0:
        return (
            None,
            (
                "No two-sided quote was available at any fill, so no spread "
                "charge can be attributed. It is inside the residual below."
            ),
            0,
        )
    return (
        total,
        (
            f"Half the quoted spread at each fill, weighted by that fill's "
            f"quantity, across {covered} of {len(parent.executions)} fills."
        ),
        covered,
    )


def decompose(
    parent: ParentOrder, window: MarketWindow, shortfall: Shortfall
) -> tuple[CostDecomposition, list[str]]:
    """Split a measured shortfall into what can be attributed and what cannot."""
    warnings: list[str] = []
    spread, spread_basis, covered = spread_cost(parent, window)
    if spread is None:
        warnings.append(TCAWarning.NO_SPREAD_DATA)

    fees = parent.fees * Decimal(1)
    attributed = (spread or Decimal(0)) + fees
    residual = shortfall.currency_amount - attributed
    warnings.append(TCAWarning.IMPACT_NOT_MODELLED)

    components = [
        CostComponent(
            "spread",
            spread,
            CostComponentStatus.MODELLED if spread is not None else CostComponentStatus.UNAVAILABLE,
            spread_basis,
        ),
        CostComponent(
            "fees",
            fees,
            CostComponentStatus.MEASURED,
            "Fees as recorded on the fills. The only component here that is an "
            "observation rather than a model.",
        ),
        CostComponent(
            "impact",
            None,
            CostComponentStatus.NOT_MODELLED,
            "Market impact is not modelled in this phase. It is not zero — it is "
            "inside the residual below, and a number here would be an invention.",
        ),
        CostComponent(
            "timing_residual",
            residual,
            CostComponentStatus.RESIDUAL,
            "What the total leaves after spread and fees. By construction it "
            "carries market impact, adverse selection and genuine price drift "
            "together, and no part of it is separately measured.",
        ),
    ]
    return (
        CostDecomposition(
            benchmark=shortfall.benchmark,
            total=shortfall.currency_amount,
            components=tuple(components),
            currency=parent.currency,
        ),
        warnings,
    )


def opportunity_cost(
    parent: ParentOrder, benchmark: Benchmark, reference: Benchmark
) -> CostComponent:
    """What the unfilled quantity would have cost at the later reference price.

    Requires the order's intended quantity, which only the trade log can supply.
    Assuming the order was fully filled because the log shows only fills is how
    an unfilled order silently reports no opportunity cost at all.
    """
    unfilled = parent.unfilled_quantity
    if unfilled is None:
        return CostComponent(
            "opportunity",
            None,
            CostComponentStatus.UNAVAILABLE,
            "The trade log did not state the order's intended quantity, so how "
            "much went unfilled is unknown. It is not assumed to be zero.",
        )
    if unfilled == 0:
        return CostComponent(
            "opportunity",
            Decimal(0),
            CostComponentStatus.MEASURED,
            "The order filled completely, so nothing went unfilled.",
        )
    if benchmark.price is None or reference.price is None:
        return CostComponent(
            "opportunity",
            None,
            CostComponentStatus.UNAVAILABLE,
            f"{unfilled} unit(s) went unfilled, but the reference price needed to "
            "value them is not available for this window.",
        )
    difference = (reference.price - benchmark.price) * parent.side.sign
    return CostComponent(
        "opportunity",
        difference * unfilled * parent.multiplier,
        CostComponentStatus.MODELLED,
        f"{unfilled} unfilled unit(s) valued at the move from "
        f"{benchmark.kind} to {reference.kind}. What they would have cost had "
        "they been chased, not what they did cost.",
    )


def analyse(
    parent: ParentOrder,
    window: MarketWindow,
    benchmarks: Sequence[Benchmark],
    primary: BenchmarkKind = BenchmarkKind.ARRIVAL,
) -> ExecutionAnalysis:
    """Benchmark and decompose one parent order."""
    shortfalls: list[Shortfall] = []
    unavailable: list[UnavailableShortfall] = []
    warnings: list[str] = []

    for benchmark in benchmarks:
        computed = implementation_shortfall(parent, benchmark)
        if computed is None:
            unavailable.append(
                UnavailableShortfall(
                    benchmark=benchmark.kind,
                    reason=benchmark.unavailable_reason
                    or "The benchmark has no price for this window.",
                )
            )
        else:
            shortfalls.append(computed)
        warnings.extend(benchmark.flags)

    if parent.is_inferred:
        warnings.append(TCAWarning.INFERRED_GROUPING)
    if not window.coverage.is_sufficient:
        warnings.append(TCAWarning.DATA_COVERAGE_LOW)
    if not shortfalls:
        warnings.append(TCAWarning.NO_BENCHMARK)

    primary_shortfall = next((item for item in shortfalls if item.benchmark is primary), None)
    if primary_shortfall is None and shortfalls:
        primary_shortfall = shortfalls[0]
        primary = primary_shortfall.benchmark

    decomposition = None
    if primary_shortfall is not None:
        decomposition, decomposition_warnings = decompose(parent, window, primary_shortfall)
        warnings.extend(decomposition_warnings)

    if parent.order_quantity is None:
        warnings.append(TCAWarning.NO_ORDER_QUANTITY)

    return ExecutionAnalysis(
        parent=parent,
        benchmarks=tuple(benchmarks),
        shortfalls=tuple(shortfalls),
        unavailable=tuple(unavailable),
        decomposition=decomposition,
        window=window,
        primary=primary,
        warnings=tuple(dict.fromkeys(warnings)),
    )
