"""The Upstox order adapter.

Build spec §46 Phase 7. This is a translator and nothing more: it turns a
platform ``OrderRequest`` into a broker request, and a broker payload back into
a ``BrokerOrderUpdate``. It takes no risk decisions — those happened in the gate
before anything reached here — and it holds no state.

## The thing this module is careful about

Build spec 1.1 forbids inventing API behaviour, and an order API is where that
rule has teeth: a wrong field name returns an error, but a wrong *status*
mapping silently tells the platform an order is filled when it is not, and the
book is then wrong with nothing anywhere reporting a problem.

So three things are true here.

**The endpoints and the status vocabulary are declared as configuration, not
baked in as facts.** They live in :class:`UpstoxOrderEndpoints` and
:data:`DEFAULT_STATUS_MAP`, and they carry a flag saying whether anybody has
checked them against the broker's published contract.

**An unverified mapping cannot place a live order.** :meth:`place_order` refuses
until ``verified_against_documentation`` is set by whoever did the checking. A
paper account is unaffected; this gate exists exactly at the boundary where a
mistake costs money.

**An unrecognised status is an error, not a guess.** A broker state the map does
not contain raises rather than being rounded to the nearest plausible one. An
order in an unknown state is a thing to stop and look at.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from domains.execution.brokers.base import (
    BrokerAccount,
    BrokerAdapter,
    BrokerCapability,
    BrokerError,
    BrokerFill,
    BrokerOrderUpdate,
    BrokerPosition,
    BrokerRejected,
)
from domains.execution.oms.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    RejectionReason,
    TimeInForce,
)

BROKER_NAME = "upstox"

#: Fields the adapter reads out of an order payload. Anything else present is
#: reported as ``unmapped_fields`` rather than ignored: a field that appears and
#: is silently dropped is how a schema change becomes a silent bug.
ORDER_FIELDS = frozenset(
    {
        "order_id",
        "status",
        "filled_quantity",
        "pending_quantity",
        "quantity",
        "average_price",
        "price",
        "status_message",
        "order_timestamp",
        "exchange_timestamp",
        "exchange_order_id",
        "trading_symbol",
        "instrument_token",
        "transaction_type",
        "order_type",
        "product",
        "validity",
    }
)


@dataclass(frozen=True, slots=True)
class UpstoxOrderEndpoints:
    """Where the order API lives, and whether anybody has checked.

    ``verified_against_documentation`` is the field that matters. It defaults to
    ``False`` and nothing in this repository sets it, because nothing in this
    repository has read the broker's published contract. Setting it is a
    deliberate act by a deployment that has, and it is what unlocks live order
    placement.
    """

    base_url: str = "https://api.upstox.com"
    place_path: str = "/v2/order/place"
    modify_path: str = "/v2/order/modify"
    cancel_path: str = "/v2/order/cancel"
    orders_path: str = "/v2/order/retrieve-all"
    positions_path: str = "/v2/portfolio/short-term-positions"
    funds_path: str = "/v2/user/get-funds-and-margin"
    timeout_seconds: float = 15.0
    #: Set only by a deployment that has checked these paths, the request field
    #: names and the status vocabulary against the broker's documentation.
    verified_against_documentation: bool = False

    def url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"


#: Broker status -> platform status.
#:
#: Deliberately not exhaustive-by-guessing. Every entry here is a mapping
#: somebody has to confirm, and a status absent from the map raises rather than
#: being rounded to a neighbour: "complete" and "cancelled" are both terminal,
#: and treating one as the other loses a position or invents one.
DEFAULT_STATUS_MAP: Mapping[str, OrderStatus] = {
    "open": OrderStatus.ACKNOWLEDGED,
    "open pending": OrderStatus.NEW,
    "trigger pending": OrderStatus.ACKNOWLEDGED,
    "validation pending": OrderStatus.NEW,
    "put order req received": OrderStatus.NEW,
    "modify validation pending": OrderStatus.ACKNOWLEDGED,
    "modify pending": OrderStatus.ACKNOWLEDGED,
    "modified": OrderStatus.ACKNOWLEDGED,
    "cancel pending": OrderStatus.ACKNOWLEDGED,
    "complete": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}


class UnknownBrokerStatus(BrokerError):
    """A broker state the platform has no mapping for.

    Raised rather than resolved. An order whose status is not understood is a
    position of unknown size, and guessing at it is worse than stopping.
    """


class LiveMappingUnverified(BrokerError):
    """The adapter has not been confirmed against the broker's documentation."""


class UpstoxOrderTransport(Protocol):
    """The one place that touches the network.

    Separate from the market-data transport because orders need methods other
    than GET, and because a test that can place an order must be impossible.
    """

    async def request_json(
        self,
        method: str,
        url: str,
        token: str,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> tuple[int, Any]: ...


class HttpUpstoxOrderTransport:
    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self._timeout = timeout_seconds

    async def request_json(
        self,
        method: str,
        url: str,
        token: str,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> tuple[int, Any]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method,
                    url,
                    json=dict(json_body) if json_body is not None else None,
                    params=dict(params or {}),
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                )
        except httpx.HTTPError as exc:
            raise BrokerError(f"the broker could not be reached: {exc}") from exc

        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None


class AccessTokenSource(Protocol):
    """Where the adapter gets a credential.

    A protocol, so the adapter never imports the credential vault and a test can
    supply a token without a database. The vault's renewal happens behind this
    call, which is why no token ever appears in a request the adapter builds.
    """

    async def __call__(self) -> str: ...


class InstrumentKeys(Protocol):
    """The join between platform ids and the broker's instrument keys."""

    async def provider_key(self, instrument_id: uuid.UUID) -> str | None: ...

    async def by_provider_key(self, key: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class UpstoxOrderDefaults:
    """Request values the broker requires and the platform does not model.

    ``product`` and ``validity`` are broker vocabulary describing how a position
    is carried and how long an order lives. The platform has no equivalent
    concept, so rather than pick one silently they are configuration, stated
    once, and written into the provenance of every order that used them.
    """

    product: str = "D"
    disclosed_quantity: int = 0
    is_amo: bool = False


class UpstoxBroker(BrokerAdapter):
    """Places orders with Upstox.

    Without ``MODIFY`` in its declared capabilities until somebody has confirmed
    the modify contract: an amendment that silently becomes a no-op is worse
    than one the OMS refuses up front, and the OMS reads this set before it
    sends anything.
    """

    name = BROKER_NAME

    def __init__(
        self,
        transport: UpstoxOrderTransport,
        token_source: AccessTokenSource,
        instruments: InstrumentKeys,
        *,
        endpoints: UpstoxOrderEndpoints | None = None,
        defaults: UpstoxOrderDefaults | None = None,
        status_map: Mapping[str, OrderStatus] | None = None,
    ) -> None:
        self._transport = transport
        self._token = token_source
        self._instruments = instruments
        self._endpoints = endpoints or UpstoxOrderEndpoints()
        self._defaults = defaults or UpstoxOrderDefaults()
        self._status_map = dict(status_map or DEFAULT_STATUS_MAP)
        self.capabilities = frozenset(
            {
                BrokerCapability.MARKET_ORDERS,
                BrokerCapability.LIMIT_ORDERS,
                BrokerCapability.CANCEL,
                BrokerCapability.POSITIONS,
                BrokerCapability.ORDERS,
                BrokerCapability.ACCOUNT,
                BrokerCapability.IMMEDIATE_OR_CANCEL,
            }
        )

    @property
    def mapping_verified(self) -> bool:
        return self._endpoints.verified_against_documentation

    # ------------------------------------------------------------ placement
    async def place_order(self, request: OrderRequest) -> BrokerOrderUpdate:
        if not self.mapping_verified:
            raise LiveMappingUnverified(
                "this adapter's endpoints, request fields and status vocabulary have "
                "not been confirmed against the broker's published contract, so it "
                "will not send a live order. A wrong field name fails loudly; a wrong "
                "status mapping tells the platform an order filled when it did not, "
                "and the book is then wrong with nothing reporting a problem. Set "
                "UpstoxOrderEndpoints.verified_against_documentation once the mapping "
                "has been checked"
            )

        key = await self._instruments.provider_key(request.instrument_id)
        if key is None:
            raise BrokerRejected(
                f"instrument {request.instrument_id} has no {BROKER_NAME} key in the "
                "master, so there is nothing to send"
            )

        body = {
            "quantity": int(request.quantity),
            "product": self._defaults.product,
            "validity": _validity(request.time_in_force),
            "price": float(request.limit_price or 0),
            "instrument_token": key,
            "order_type": _order_type(request.order_type),
            "transaction_type": _transaction_type(request.side),
            "disclosed_quantity": self._defaults.disclosed_quantity,
            "trigger_price": 0,
            "is_amo": self._defaults.is_amo,
        }
        status, payload = await self._call("POST", self._endpoints.place_path, body)
        return self._update_from(status, payload, request=request)

    async def cancel_order(self, broker_order_id: str) -> BrokerOrderUpdate:
        status, payload = await self._call(
            "DELETE", self._endpoints.cancel_path, params={"order_id": broker_order_id}
        )
        return self._update_from(status, payload)

    async def modify_order(
        self,
        broker_order_id: str,
        quantity: Decimal | None = None,
        limit_price: Decimal | None = None,
    ) -> BrokerOrderUpdate:
        raise NotImplementedError(
            "order modification is not enabled for this adapter. The modify contract "
            "has not been confirmed, and an amendment that silently becomes a no-op "
            "leaves an order working at terms nobody chose. Cancel and replace"
        )

    async def get_orders(self) -> list[BrokerOrderUpdate]:
        status, payload = await self._call("GET", self._endpoints.orders_path)
        rows = _rows(payload)
        return [self._one_order(row) for row in rows]

    async def get_positions(self) -> list[BrokerPosition]:
        """The broker's own view, kept beside the platform's rather than merged.

        A reconciliation that overwrote one with the other would destroy the only
        evidence that they had ever disagreed, which is the entire point of
        asking the broker in the first place.
        """
        _status, payload = await self._call("GET", self._endpoints.positions_path)
        reported_at = datetime.now(UTC)
        positions: list[BrokerPosition] = []
        for row in _rows(payload):
            key = str(row.get("instrument_token") or "")
            instrument = await self._instruments.by_provider_key(key) if key else None
            positions.append(
                BrokerPosition(
                    instrument_key=key,
                    quantity=_decimal(row.get("quantity")) or Decimal(0),
                    average_price=_decimal(row.get("average_price")),
                    instrument_id=getattr(instrument, "id", None),
                    reported_at=reported_at,
                    raw=dict(row),
                )
            )
        return positions

    async def get_account(self) -> BrokerAccount:
        """Balances as the broker reports them, labelled as reports.

        Every figure keeps its ``reported_`` prefix and its timestamp. The
        platform computes no margin of its own and does not blend one into
        these: what is here is an observation, attributed, and callers can see
        that is what it is.
        """
        _status, payload = await self._call("GET", self._endpoints.funds_path)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        segment = _first_mapping(data)
        known = {"available_margin", "used_margin", "payin_amount"}
        unmapped = tuple(sorted(set(segment) - known)) if segment else ()
        return BrokerAccount(
            broker=BROKER_NAME,
            reported_at=datetime.now(UTC),
            reported_available_margin=_decimal((segment or {}).get("available_margin")),
            reported_used_margin=_decimal((segment or {}).get("used_margin")),
            reported_available_cash=_decimal((segment or {}).get("payin_amount")),
            unmapped_fields=unmapped,
            raw=dict(segment) if segment else {},
        )

    # ------------------------------------------------------------- internals
    async def _call(
        self,
        method: str,
        path: str,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> tuple[int, Any]:
        token = await self._token()
        status, payload = await self._transport.request_json(
            method, self._endpoints.url(path), token, json_body, params
        )
        if status >= 500:
            raise BrokerError(
                f"the broker returned {status} for {method} {path}; the outcome of "
                "this request is unknown and it must be reconciled rather than retried"
            )
        if status in (401, 403):
            raise BrokerError(
                f"the broker refused the credential ({status}). The connection needs "
                "reauthorising; no order was placed"
            )
        return status, payload

    def _update_from(
        self, status_code: int, payload: Any, request: OrderRequest | None = None
    ) -> BrokerOrderUpdate:
        data = payload.get("data") if isinstance(payload, Mapping) else None

        if status_code >= 400 or (isinstance(payload, Mapping) and payload.get("errors")):
            raise BrokerRejected(
                _error_detail(payload) or f"the broker refused the order ({status_code})",
                code=_error_code(payload),
            )

        if isinstance(data, Mapping) and "order_id" in data and "status" not in data:
            # A placement acknowledgement: an id and nothing else. Reported as
            # ACKNOWLEDGED rather than assumed to be working or filled — the
            # order's real state comes from the next poll, not from optimism.
            return BrokerOrderUpdate(
                status=OrderStatus.ACKNOWLEDGED,
                broker_order_id=str(data["order_id"]),
                unmapped_fields=tuple(sorted(set(data) - {"order_id"})),
                raw=dict(data),
            )

        if isinstance(data, Mapping):
            return self._one_order(data, request)
        raise BrokerError(
            "the broker's response carried no order payload this adapter recognises; "
            f"keys seen: {sorted(payload) if isinstance(payload, Mapping) else type(payload)}"
        )

    def _one_order(
        self, row: Mapping[str, Any], request: OrderRequest | None = None
    ) -> BrokerOrderUpdate:
        raw_status = str(row.get("status") or "").strip().lower()
        mapped = self._status_map.get(raw_status)
        if mapped is None:
            raise UnknownBrokerStatus(
                f"the broker reported a status this adapter has no mapping for: "
                f"{raw_status!r}. It is not resolved to the nearest plausible state, "
                "because an order in an unknown state is a position of unknown size. "
                f"Known states: {', '.join(sorted(self._status_map))}"
            )

        filled = _decimal(row.get("filled_quantity")) or Decimal(0)
        average = _decimal(row.get("average_price"))
        fills: tuple[BrokerFill, ...] = ()
        if filled > 0 and average is not None and average > 0:
            side = _side_from(row, request)
            fills = (
                BrokerFill(
                    quantity=filled * side.sign,
                    price=average,
                    filled_at=_timestamp(row) or datetime.now(UTC),
                    # Named for what it is. This is the broker's average across
                    # however many executions it aggregated, not a price on a
                    # single trade, and calling it a fill price would overstate
                    # what is known.
                    price_basis="BROKER_REPORTED_AVERAGE",
                    broker_trade_id=str(row.get("exchange_order_id") or "") or None,
                ),
            )

        if mapped is OrderStatus.FILLED and filled == 0:
            raise BrokerError(
                "the broker reports a completed order with nothing filled. That "
                "combination is not translated into a state: it means either the "
                "mapping or the payload is wrong, and both need looking at"
            )
        if (
            mapped is OrderStatus.FILLED
            and fills
            and filled < (_decimal(row.get("quantity")) or filled)
        ):
            mapped = OrderStatus.PARTIALLY_FILLED

        return BrokerOrderUpdate(
            status=mapped,
            broker_order_id=str(row.get("order_id") or "") or None,
            fills=fills,
            rejection_reason=(
                RejectionReason.BROKER_REJECTED if mapped is OrderStatus.REJECTED else None
            ),
            rejection_detail=str(row.get("status_message") or "") or None,
            unmapped_fields=tuple(sorted(set(row) - ORDER_FIELDS)),
            raw=dict(row),
        )


def _validity(tif: TimeInForce) -> str:
    return "IOC" if tif is TimeInForce.IMMEDIATE_OR_CANCEL else "DAY"


def _order_type(order_type: OrderType) -> str:
    return "LIMIT" if order_type is OrderType.LIMIT else "MARKET"


def _transaction_type(side: OrderSide) -> str:
    return "BUY" if side is OrderSide.BUY else "SELL"


def _side_from(row: Mapping[str, Any], request: OrderRequest | None) -> OrderSide:
    """The side of a fill, from the broker's own field where it has one.

    Falls back to the request only when the payload does not say. Getting this
    wrong inverts a position, so the broker's statement is preferred over ours
    even though ours is the instruction that created the order.
    """
    reported = str(row.get("transaction_type") or "").strip().upper()
    if reported in ("BUY", "SELL"):
        return OrderSide(reported)
    if request is not None:
        return request.side
    raise BrokerError(
        "the fill payload carries no transaction type and no request to read one "
        "from; a fill booked on the wrong side inverts a position"
    )


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    data = payload.get("data") if isinstance(payload, Mapping) else payload
    if isinstance(data, list):
        return [row for row in data if isinstance(row, Mapping)]
    if isinstance(data, Mapping):
        return [data]
    return []


def _first_mapping(data: Any) -> Mapping[str, Any] | None:
    if isinstance(data, Mapping):
        for value in data.values():
            if isinstance(value, Mapping):
                return value
        return data
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _timestamp(row: Mapping[str, Any]) -> datetime | None:
    for field_name in ("exchange_timestamp", "order_timestamp"):
        raw = row.get(field_name)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _error_detail(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, Mapping):
            return str(first.get("message") or first.get("errorCode") or first)
        return str(first)
    return str(payload.get("message")) if payload.get("message") else None


def _error_code(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
        code = errors[0].get("errorCode") or errors[0].get("error_code")
        return str(code) if code else None
    return None


__all__ = [
    "BROKER_NAME",
    "DEFAULT_STATUS_MAP",
    "HttpUpstoxOrderTransport",
    "LiveMappingUnverified",
    "UnknownBrokerStatus",
    "UpstoxBroker",
    "UpstoxOrderDefaults",
    "UpstoxOrderEndpoints",
    "UpstoxOrderTransport",
]
