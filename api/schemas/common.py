from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer


def _serialize_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    # Tolerant of a declared string default that was never coerced.
    return format(value, "f") if isinstance(value, Decimal) else str(value)


#: Decimals cross the wire as strings. A JSON number would be parsed as a float
#: by every client, which is exactly the precision loss the platform's Decimal
#: discipline exists to prevent.
DecimalStr = Annotated[Decimal, PlainSerializer(_serialize_decimal, return_type=str)]


class APIModel(BaseModel):
    # validate_default so a declared default such as "0.05" becomes a Decimal
    # rather than surviving as a string into the domain layer.
    model_config = ConfigDict(from_attributes=True, populate_by_name=True, validate_default=True)


class WarningOut(APIModel):
    code: str
    severity: str
    message: str
    context: dict[str, Any] = {}


class ProvenanceOut(APIModel):
    computed_at: str
    code_commit: str
    market_state_timestamp: str | None = None
    market_state_id: str | None = None
    market_data_sources: list[str] = []
    dataset_versions: dict[str, str] = {}
    yield_curve_id: str | None = None
    surface_id: str | None = None
    model_versions: dict[str, str] = {}
    calibration_timestamp: str | None = None
    numerical_tolerances: dict[str, Any] = {}
    parameters: dict[str, Any] = {}


class Envelope(APIModel):
    """The analytical result envelope every analytical endpoint returns."""

    status: str
    results: Any | None
    warnings: list[WarningOut] = []
    provenance: ProvenanceOut


class PageMeta(APIModel):
    limit: int
    offset: int
    count: int
