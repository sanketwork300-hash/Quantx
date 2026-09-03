"""Scenario management: templates, user definitions, and derivation from data."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from domains.scenarios.library import derive_from_returns, template_by_name, templates
from domains.scenarios.models import (
    RiskFactorKind,
    Scenario,
    ScenarioError,
    ScenarioSource,
    Shock,
    ShockType,
)
from domains.scenarios.repository import ScenarioRepository, to_scenario


class ScenarioNotFound(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ShockInput:
    kind: str
    shock_type: str
    value: float
    target: str | None = None

    def to_shock(self) -> Shock:
        return Shock(
            kind=RiskFactorKind(self.kind),
            shock_type=ShockType(self.shock_type),
            value=self.value,
            target=self.target,
        )


class ScenarioService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.repository = ScenarioRepository(session)

    # ------------------------------------------------------------- reading
    @staticmethod
    def templates() -> tuple[Scenario, ...]:
        return templates()

    async def list(self, user_id: uuid.UUID, include_templates: bool = True) -> list[Scenario]:
        stored = [to_scenario(row) for row in await self.repository.list(user_id)]
        return [*(templates() if include_templates else ()), *stored]

    async def resolve(self, user_id: uuid.UUID, reference: str) -> Scenario:
        """Find a scenario by id or by name, template or stored.

        Templates are resolved by name as well as by id because their ids are
        derived, not memorable, and a saved request that names one should keep
        working.
        """
        template = template_by_name(reference)
        if template is not None:
            return template

        try:
            scenario_id = uuid.UUID(reference)
        except ValueError:
            row = await self.repository.get_by_name(user_id, reference)
            if row is None:
                raise ScenarioNotFound(reference) from None
            return to_scenario(row)

        for candidate in templates():
            if candidate.id == scenario_id:
                return candidate
        row = await self.repository.get(scenario_id, user_id)
        if row is None:
            raise ScenarioNotFound(reference)
        return to_scenario(row)

    # ------------------------------------------------------------- writing
    async def create(
        self,
        user_id: uuid.UUID,
        name: str,
        shocks: Sequence[ShockInput],
        description: str | None = None,
    ) -> Scenario:
        if await self.repository.get_by_name(user_id, name.strip()):
            raise ScenarioError(f"a scenario named {name!r} already exists")
        scenario = Scenario(
            id=uuid.uuid4(),
            name=name.strip(),
            description=description,
            shocks=tuple(shock.to_shock() for shock in shocks),
            source=ScenarioSource.USER_DEFINED,
        )
        row = await self.repository.create(user_id, scenario)
        return to_scenario(row)

    async def derive(
        self,
        user_id: uuid.UUID,
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
        """Compute a scenario from a series the platform holds, and store it."""
        if await self.repository.get_by_name(user_id, name.strip()):
            raise ScenarioError(f"a scenario named {name!r} already exists")
        scenario = derive_from_returns(
            name=name.strip(),
            dates=dates,
            prices=prices,
            series_label=series_label,
            window_days=window_days,
            target=target,
            percentile=percentile,
            volatility_dates=volatility_dates,
            volatility_levels=volatility_levels,
        )
        row = await self.repository.create(user_id, scenario)
        return to_scenario(row)

    async def delete(self, user_id: uuid.UUID, scenario_id: uuid.UUID) -> None:
        row = await self.repository.get(scenario_id, user_id)
        if row is None:
            raise ScenarioNotFound(str(scenario_id))
        await self.repository.delete(scenario_id)
