from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, LargeBinary, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from infrastructure.database.types import JSONDict, UTCDateTime


class BrokerConnectionORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One user's credential with one provider.

    The token columns hold ciphertext and nothing else: there is no column on
    this table capable of holding a token in the clear, so no code path can
    accidentally write one. ``encryption_key_id`` records which key sealed the
    row, which is what makes key rotation possible without a flag day.
    """

    __tablename__ = "broker_connections"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    encryption_key_id: Mapped[str | None] = mapped_column(String(32))
    access_token_nonce: Mapped[bytes | None] = mapped_column(LargeBinary(16))
    access_token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_token_nonce: Mapped[bytes | None] = mapped_column(LargeBinary(16))
    refresh_token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)

    provider_account_id: Mapped[str | None] = mapped_column(String(64))
    scopes: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)

    #: Populated only when the provider declared a lifetime; see ``expiry_source``.
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expiry_source: Mapped[str] = mapped_column(String(24), nullable=False)

    connected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(String(200))

    #: The in-flight authorization handoff. Holding the nonce here — rather than
    #: trusting the signed ``state`` alone — is what makes a state single-use:
    #: a replayed redirect finds the nonce already cleared.
    pending_state_nonce: Mapped[str | None] = mapped_column(String(64))
    pending_state_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_broker_connection_user_provider"),
        Index("ix_broker_connection_status", "provider", "status"),
    )
