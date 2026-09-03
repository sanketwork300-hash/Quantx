"""Market impact models, and the coefficient this platform will not invent.

The functional forms here are from the literature and are uncontroversial. The
**coefficients are not**: they are regime-, venue- and period-dependent, and the
published estimates come from markets nobody here has observed. So the default
coefficient is `1.0` — not because one is the right answer, but because it is
the identity, and it makes the output read as the shape of the model in units of
`sigma * sqrt(Q / ADV)` rather than as a magnitude the platform is claiming.

Every result computed at the default carries
`IMPACT_COEFFICIENT_NOT_CALIBRATED`. Supply a coefficient measured on your own
executions and that warning goes away; until then the number is a shape, and it
says so.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

#: The identity. Chosen because a dimensionless one asserts nothing, and every
#: result computed with it is flagged as uncalibrated.
UNCALIBRATED_COEFFICIENT = 1.0

#: Below this a participation rate is treated as zero: an order that is a
#: millionth of the day's volume moves nothing this model can resolve.
NEGLIGIBLE_PARTICIPATION = 1e-12


class ImpactWarning(StrEnum):
    NOT_CALIBRATED = "IMPACT_COEFFICIENT_NOT_CALIBRATED"
    NO_ADV = "IMPACT_NO_AVERAGE_DAILY_VOLUME"
    NO_VOLATILITY = "IMPACT_NO_VOLATILITY"
    PARTICIPATION_ABOVE_ONE = "IMPACT_PARTICIPATION_EXCEEDS_DAILY_VOLUME"
    EXTRAPOLATED = "IMPACT_EXTRAPOLATED_BEYOND_CALIBRATION_RANGE"


class ImpactError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ImpactEstimate:
    """What a model says an order costs to push through the book.

    Split into permanent and temporary because a simulator needs them
    separately: permanent impact moves the reference price for every later
    slice, temporary impact is paid on the slice and then decays. Reporting only
    the total would make the two indistinguishable and the simulation wrong in a
    way that is invisible.
    """

    model: str
    #: Fraction of the reference price, e.g. 0.001 is ten basis points.
    permanent: float
    temporary: float
    reference_price: float
    participation: float
    parameters: dict
    basis: str
    warnings: tuple[str, ...] = ()

    @property
    def total(self) -> float:
        return self.permanent + self.temporary

    @property
    def permanent_price(self) -> float:
        return self.permanent * self.reference_price

    @property
    def temporary_price(self) -> float:
        return self.temporary * self.reference_price

    @property
    def total_price(self) -> float:
        return self.total * self.reference_price

    @property
    def is_calibrated(self) -> bool:
        return ImpactWarning.NOT_CALIBRATED not in self.warnings

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "permanent_fraction": self.permanent,
            "temporary_fraction": self.temporary,
            "total_fraction": self.total,
            "permanent_price": self.permanent_price,
            "temporary_price": self.temporary_price,
            "total_price": self.total_price,
            "total_basis_points": self.total * 10_000,
            "reference_price": self.reference_price,
            "participation": self.participation,
            "parameters": self.parameters,
            "basis": self.basis,
            "is_calibrated": self.is_calibrated,
            "warnings": list(self.warnings),
            "caveat": (
                "A model estimate of what an order would cost to execute, not a "
                "measurement of what it did. Nothing here was fitted to your "
                "executions unless you supplied the coefficient yourself."
            ),
        }


class MarketImpactModel(ABC):
    """A named, versioned way of estimating market impact."""

    name: str
    version: str

    @abstractmethod
    def estimate(
        self,
        quantity: float,
        average_daily_volume: float,
        volatility: float,
        reference_price: float,
        participation: float | None = None,
    ) -> ImpactEstimate:
        """Impact of trading ``quantity`` against a day that trades ``adv``."""

    @property
    def identifier(self) -> str:
        return f"{self.name}@{self.version}"

    @staticmethod
    def _check(
        quantity: float,
        average_daily_volume: float,
        volatility: float,
        reference_price: float,
    ) -> list[str]:
        if quantity < 0:
            raise ImpactError("impact is estimated for a quantity, not a direction")
        if reference_price <= 0:
            raise ImpactError(f"a non-positive reference price is not a market: {reference_price}")

        warnings: list[str] = []
        if average_daily_volume <= 0:
            raise ImpactError(
                "average daily volume must be positive: impact is a function of "
                "how large the order is relative to the day, and there is no "
                "such ratio against a day that traded nothing"
            )
        if volatility <= 0:
            warnings.append(ImpactWarning.NO_VOLATILITY)
        if quantity > average_daily_volume:
            warnings.append(ImpactWarning.PARTICIPATION_ABOVE_ONE)
        return warnings


class SquareRootImpactModel(MarketImpactModel):
    """The square-root law: impact grows with the square root of size.

        permanent = eta   * sigma * sqrt(Q / ADV)
        temporary = gamma * sigma * sqrt(participation)

    The square-root dependence on relative size is the most robust empirical
    regularity in the impact literature (Almgren et al. 2005; Gatheral 2010).
    The coefficients are not: they vary by market, venue and period, so they
    default to the identity and every result computed that way is flagged.

    The temporary term uses the *participation rate* — the share of volume the
    order takes while it is working — rather than total size, because that is
    the quantity a trader actually controls by choosing a schedule. Trading the
    same order more slowly lowers it; the permanent term does not move.
    """

    name = "SquareRootImpactModel"
    version = "1.0.0"

    def __init__(
        self,
        permanent_coefficient: float = UNCALIBRATED_COEFFICIENT,
        temporary_coefficient: float = UNCALIBRATED_COEFFICIENT,
    ) -> None:
        if permanent_coefficient < 0 or temporary_coefficient < 0:
            raise ImpactError("impact coefficients cannot be negative")
        self.permanent_coefficient = permanent_coefficient
        self.temporary_coefficient = temporary_coefficient

    def estimate(
        self,
        quantity: float,
        average_daily_volume: float,
        volatility: float,
        reference_price: float,
        participation: float | None = None,
    ) -> ImpactEstimate:
        warnings = self._check(quantity, average_daily_volume, volatility, reference_price)
        if (
            self.permanent_coefficient == UNCALIBRATED_COEFFICIENT
            and self.temporary_coefficient == UNCALIBRATED_COEFFICIENT
        ):
            warnings.append(ImpactWarning.NOT_CALIBRATED)

        size_ratio = quantity / average_daily_volume
        rate = size_ratio if participation is None else participation
        rate = max(rate, 0.0)

        permanent = self.permanent_coefficient * volatility * math.sqrt(size_ratio)
        temporary = (
            0.0
            if rate < NEGLIGIBLE_PARTICIPATION
            else self.temporary_coefficient * volatility * math.sqrt(rate)
        )

        return ImpactEstimate(
            model=self.identifier,
            permanent=permanent,
            temporary=temporary,
            reference_price=reference_price,
            participation=rate,
            parameters={
                "permanent_coefficient": self.permanent_coefficient,
                "temporary_coefficient": self.temporary_coefficient,
                "volatility": volatility,
                "average_daily_volume": average_daily_volume,
                "quantity": quantity,
                "size_ratio": size_ratio,
            },
            basis=(
                f"eta * sigma * sqrt(Q/ADV) permanent and gamma * sigma * "
                f"sqrt(participation) temporary, with eta="
                f"{self.permanent_coefficient:g}, gamma="
                f"{self.temporary_coefficient:g}, sigma={volatility:g} and "
                f"Q/ADV={size_ratio:.6g}."
            ),
            warnings=tuple(dict.fromkeys(warnings)),
        )


class LinearImpactModel(MarketImpactModel):
    """Impact proportional to relative size, kept as a comparison baseline.

        permanent = eta   * sigma * (Q / ADV)
        temporary = gamma * sigma * participation

    Included because seeing it disagree with the square-root model is the point.
    Linear impact overstates large orders badly — the empirical regularity is
    concave — and a result that is the same under both is a result that does not
    depend on the choice.
    """

    name = "LinearImpactModel"
    version = "1.0.0"

    def __init__(
        self,
        permanent_coefficient: float = UNCALIBRATED_COEFFICIENT,
        temporary_coefficient: float = UNCALIBRATED_COEFFICIENT,
    ) -> None:
        if permanent_coefficient < 0 or temporary_coefficient < 0:
            raise ImpactError("impact coefficients cannot be negative")
        self.permanent_coefficient = permanent_coefficient
        self.temporary_coefficient = temporary_coefficient

    def estimate(
        self,
        quantity: float,
        average_daily_volume: float,
        volatility: float,
        reference_price: float,
        participation: float | None = None,
    ) -> ImpactEstimate:
        warnings = self._check(quantity, average_daily_volume, volatility, reference_price)
        if (
            self.permanent_coefficient == UNCALIBRATED_COEFFICIENT
            and self.temporary_coefficient == UNCALIBRATED_COEFFICIENT
        ):
            warnings.append(ImpactWarning.NOT_CALIBRATED)

        size_ratio = quantity / average_daily_volume
        rate = max(size_ratio if participation is None else participation, 0.0)

        return ImpactEstimate(
            model=self.identifier,
            permanent=self.permanent_coefficient * volatility * size_ratio,
            temporary=self.temporary_coefficient * volatility * rate,
            reference_price=reference_price,
            participation=rate,
            parameters={
                "permanent_coefficient": self.permanent_coefficient,
                "temporary_coefficient": self.temporary_coefficient,
                "volatility": volatility,
                "average_daily_volume": average_daily_volume,
                "quantity": quantity,
                "size_ratio": size_ratio,
            },
            basis=(
                f"eta * sigma * (Q/ADV) permanent and gamma * sigma * "
                f"participation temporary, with eta="
                f"{self.permanent_coefficient:g}, gamma="
                f"{self.temporary_coefficient:g}, sigma={volatility:g} and "
                f"Q/ADV={size_ratio:.6g}. Linear in size, which overstates large "
                f"orders: the empirical regularity is concave."
            ),
            warnings=tuple(dict.fromkeys(warnings)),
        )


class ZeroImpactModel(MarketImpactModel):
    """No impact at all, for isolating the schedule from the impact model.

    Not a claim that trading is free. It exists so a simulation can answer "what
    would this schedule have paid against the observed prices alone?", which is
    the only part of a counterfactual that does not depend on an uncalibrated
    coefficient.
    """

    name = "ZeroImpactModel"
    version = "1.0.0"

    def estimate(
        self,
        quantity: float,
        average_daily_volume: float,
        volatility: float,
        reference_price: float,
        participation: float | None = None,
    ) -> ImpactEstimate:
        return ImpactEstimate(
            model=self.identifier,
            permanent=0.0,
            temporary=0.0,
            reference_price=reference_price,
            participation=0.0 if participation is None else participation,
            parameters={"quantity": quantity, "average_daily_volume": average_daily_volume},
            basis=(
                "No impact is modelled. This is not a claim that trading is free; "
                "it isolates what the schedule paid against the observed prices "
                "alone, which is the part of the answer that does not depend on "
                "an uncalibrated coefficient."
            ),
        )


IMPACT_MODELS: dict[str, type[MarketImpactModel]] = {
    SquareRootImpactModel.name: SquareRootImpactModel,
    LinearImpactModel.name: LinearImpactModel,
    ZeroImpactModel.name: ZeroImpactModel,
}


def build_impact_model(
    name: str,
    permanent_coefficient: float = UNCALIBRATED_COEFFICIENT,
    temporary_coefficient: float = UNCALIBRATED_COEFFICIENT,
) -> MarketImpactModel:
    try:
        factory = IMPACT_MODELS[name]
    except KeyError:
        raise ImpactError(
            f"unknown impact model {name!r}; available: {', '.join(sorted(IMPACT_MODELS))}"
        ) from None
    if factory is ZeroImpactModel:
        return factory()
    return factory(permanent_coefficient, temporary_coefficient)
