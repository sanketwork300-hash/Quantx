"""Working the resting book.

A paper account has no exchange behind it, so nothing happens to a resting
limit order unless something asks. This job is that something: it offers the
current market to every open order on every paper account that has one.

It calls exactly the method the ``/work`` endpoint calls, so a manual poke and
the scheduled sweep cannot disagree about what a resting order does. That is not
tidiness — two paths through a matching rule is how a paper account starts
filling orders in a way no test covers.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from domains.execution.oms.models import OrderVenue
from domains.execution.oms.repository import TradingRepository
from domains.execution.oms.service import OrderManagementService
from domains.instruments.service import InstrumentService
from domains.jobs.handlers import register
from domains.jobs.models import Job, JobType
from domains.market_data.live import LiveMarketDataService
from domains.market_data.streaming.live_state import LiveMarketStore
from infrastructure.cache.client import get_cache
from infrastructure.settings import get_settings


async def work_resting_orders(session: AsyncSession, job: Job) -> dict:
    """Offer the market to every resting paper order this user holds.

    Scoped to the job's own user, like every other job here. A sweep that
    crossed users would be one bug away from filling somebody else's order.
    """
    settings = get_settings()
    live = LiveMarketDataService(
        InstrumentService(session),
        LiveMarketStore(get_cache(settings), settings.live_quote_ttl_seconds),
        source=settings.market_data_provider,
        subscription_ttl_seconds=settings.live_subscription_ttl_seconds,
    )
    trading = OrderManagementService(session, InstrumentService(session), live, settings)
    repository = TradingRepository(session)

    accounts = await repository.accounts_with_open_orders(venue=str(OrderVenue.PAPER))
    considered = 0
    changed: list[dict] = []
    skipped: list[str] = []
    for account_id in accounts:
        if await repository.get_account(account_id, job.user_id) is None:
            # Another user's account. Counted, so the totals still add up.
            skipped.append(str(account_id))
            continue
        considered += 1
        for order in await trading.work_open_orders(job.user_id, account_id):
            changed.append(
                {
                    "order_id": str(order.id),
                    "account_id": str(account_id),
                    "status": str(order.status),
                    "filled_quantity": format(order.filled_quantity, "f"),
                }
            )

    return {
        "accounts_with_resting_orders": len(accounts),
        "accounts_worked": considered,
        "accounts_belonging_to_other_users": len(skipped),
        "orders_changed": len(changed),
        "changes": changed,
    }


def register_handlers() -> None:
    register(JobType.WORK_RESTING_ORDERS, work_resting_orders)
