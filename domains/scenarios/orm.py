"""Scenario persistence.

Only user-defined and history-derived scenarios are stored. The shipped
templates live in code (`domains/scenarios/library.py`) with deterministic
``uuid5`` ids, so they are the same everywhere and a stored result can name the
template it used without a row having to exist for it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from infrastructure.database.types import JSONDict


class ScenarioORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "stress_scenarios"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    shocks: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    #: Present exactly when ``source`` claims history, enforced below. A
    #: scenario that says it came from data must carry the data it came from.
    derivation: Mapped[dict | None] = mapped_column(JSONDict)
    scenario_metadata: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_scenarios_user", "user_id", "created_at"),
        Index("ix_scenarios_user_name", "user_id", "name", unique=True),
        CheckConstraint(
            "source <> 'DERIVED_FROM_HISTORY' OR derivation IS NOT NULL",
            name="ck_scenario_historical_claim_has_derivation",
        ),
    )
