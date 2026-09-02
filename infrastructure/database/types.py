"""Portable column types.

Two problems this module solves:

1. **Decimals must survive the round trip.** PostgreSQL ``NUMERIC`` is exact;
   SQLite has no decimal type and SQLAlchemy would silently route values
   through ``float``. Since the platform's whole premise is that prices and
   quantities are exact, ``DecimalType`` stores decimals as text on dialects
   without a native numeric type.

2. **Timestamps must always be timezone-aware UTC.** SQLite drops the tzinfo,
   which yields naive datetimes on read and an exception the first time they
   are compared with an aware one. ``UTCDateTime`` normalises both directions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON, TypeDecorator

# Wide enough for crypto quantities (8+ dp) and index option notionals alike.
NUMERIC_PRECISION = 28
NUMERIC_SCALE = 10


class DecimalType(TypeDecorator):
    """Exact decimal storage: NUMERIC on Postgres, TEXT elsewhere."""

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int = NUMERIC_PRECISION, scale: int = NUMERIC_SCALE):
        super().__init__(precision=precision, scale=scale, asdecimal=True)

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(
                Numeric(precision=self.impl.precision, scale=self.impl.scale, asdecimal=True)
            )
        return dialect.type_descriptor(String(64))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        if dialect.name == "postgresql":
            return value
        return format(value, "f")

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every dialect."""

    impl = DateTime
    cache_ok = True

    def __init__(self):
        super().__init__(timezone=True)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected datetime, got {type(value)!r}")
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime rejected; all platform timestamps are timezone-aware UTC"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class JSONDict(TypeDecorator):
    """JSONB on Postgres, JSON text elsewhere. Always round-trips a dict."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return json.dumps(value, default=_json_default)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)


def _json_default(obj):
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, datetime):
        return obj.astimezone(UTC).isoformat()
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")
