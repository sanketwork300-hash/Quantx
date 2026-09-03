"""Scenario reads and writes, always scoped to an owner."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.scenarios.models import (
    HistoricalDerivation,
    RiskFactorKind,
    Scenario,
    ScenarioSource,
    Shock,
    ShockType,
)
from domains.scenarios.orm import ScenarioORM


def to_scenario(row: ScenarioORM) -> Scenario:
    return Scenario(
        id=row.id,
        name=row.name,
        description=row.description,
        source=ScenarioSource(row.source),
        shocks=tuple(
            Shock(
                kind=RiskFactorKind(shock["kind"]),
                shock_type=ShockType(shock["shock_type"]),
                value=float(shock["value"]),
                target=shock.get("target"),
            )
            for shock in row.shocks
        ),
        derivation=_derivation(row.derivation),
        created_at=row.created_at,
        metadata=dict(row.scenario_metadata or {}),
    )


def _derivation(payload: dict | None) -> HistoricalDerivation | None:
    if not payload:
        return None
    from datetime import date

    return HistoricalDerivation(
        series=payload["series"],
        observations=int(payload["observations"]),
        start_date=date.fromisoformat(payload["start_date"]),
        end_date=date.fromisoformat(payload["end_date"]),
        event_date=date.fromisoformat(payload["event_date"]),
        window_days=int(payload["window_days"]),
        method=payload["method"],
    )


class ScenarioRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, user_id: uuid.UUID, scenario: Scenario) -> ScenarioORM:
        row = ScenarioORM(
            id=scenario.id,
            user_id=user_id,
            name=scenario.name,
            description=scenario.description,
            source=str(scenario.source),
            shocks=[shock.to_dict() for shock in scenario.shocks],
            derivation=scenario.derivation.to_dict() if scenario.derivation else None,
            scenario_metadata=dict(scenario.metadata),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, scenario_id: uuid.UUID, user_id: uuid.UUID) -> ScenarioORM | None:
        row = await self._session.get(ScenarioORM, scenario_id)
        if row is None or row.user_id != user_id:
            return None
        return row

    async def get_by_name(self, user_id: uuid.UUID, name: str) -> ScenarioORM | None:
        result = await self._session.execute(
            select(ScenarioORM).where(ScenarioORM.user_id == user_id, ScenarioORM.name == name)
        )
        return result.scalar_one_or_none()

    async def list(
        self, user_id: uuid.UUID, limit: int = 100, offset: int = 0
    ) -> Sequence[ScenarioORM]:
        result = await self._session.execute(
            select(ScenarioORM)
            .where(ScenarioORM.user_id == user_id)
            .order_by(ScenarioORM.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars())

    async def delete(self, scenario_id: uuid.UUID) -> None:
        row = await self._session.get(ScenarioORM, scenario_id)
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()
