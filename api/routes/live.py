"""Live market data.

The read side of the feed. Everything here answers from the live store, which
the stream worker writes; no route opens a connection to a provider, because a
request that fetched a price would give a different answer from the feed's and
neither would say which was right.

The one thing every response carries is age. A live price with no visible age
gets treated as current whatever it actually is, and that is the failure mode
this whole surface exists to avoid.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Query, status

from api.dependencies.core import (
    CurrentUser,
    InstrumentServiceDep,
    JobServiceDep,
    LiveMarketServiceDep,
    SessionDep,
    SettingsDep,
)
from api.errors import NotFound, UnprocessableEntity
from api.schemas.live import (
    FeedHealthOut,
    LiveQuoteOut,
    LiveQuotesOut,
    LiveStateOut,
    LiveStatusOut,
    SubscriptionOut,
    SubscriptionRequest,
)
from api.schemas.market import QualityOut
from api.schemas.options import LiveChainAnalysisRequest
from api.schemas.uploads import JobAcceptedOut
from domains.jobs.dispatcher import submit_job
from domains.jobs.models import JobStatus, JobType
from domains.market_data.live import LiveQuoteView
from domains.market_data.providers.factory import ProviderKind
from domains.users.models import AuditAction
from domains.users.service import UserService

router = APIRouter(prefix="/live", tags=["live-market-data"])


def _quote_out(view: LiveQuoteView) -> LiveQuoteOut:
    quote = view.live.quote
    quality = view.live.quality
    return LiveQuoteOut(
        instrument_id=quote.instrument_id,
        symbol=view.instrument.symbol,
        exchange=view.instrument.exchange,
        asset_class=str(view.instrument.asset_class),
        exchange_timestamp=quote.exchange_timestamp,
        receive_timestamp=quote.receive_timestamp,
        age_seconds=view.age_seconds,
        source=quote.source,
        feed=view.live.feed,
        bid_price=quote.bid_price,
        bid_size=quote.bid_size,
        ask_price=quote.ask_price,
        ask_size=quote.ask_size,
        last_price=quote.last_price,
        volume=quote.volume,
        open_interest=quote.open_interest,
        # Derived on read from the stored observations, and null when there is
        # no genuine two-sided market rather than filled from the last print.
        mid_price=quote.mid_price,
        quality=(
            QualityOut(
                stale_score=quality.stale_score,
                spread_score=quality.spread_score,
                liquidity_score=quality.liquidity_score,
                consistency_score=quality.consistency_score,
                completeness_score=quality.completeness_score,
                overall_score=quality.overall_score,
                flags=[flag.to_dict() for flag in quality.flags],
            )
            if quality is not None
            else None
        ),
    )


@router.get("/status", response_model=LiveStatusOut)
async def live_status(
    _user: CurrentUser, live: LiveMarketServiceDep, settings: SettingsDep
) -> LiveStatusOut:
    """What the platform is configured to do, and what the feed is doing."""
    health = await live.feed_health(settings.market_data_provider)
    transport = settings.market_stream_transport

    unavailable: str | None = None
    if settings.market_data_provider == ProviderKind.SYNTHETIC:
        unavailable = (
            "QIP_MARKET_DATA_PROVIDER is 'synthetic': prices come from a generated market "
            "and describe nothing real."
        )
    elif health is None:
        unavailable = (
            "No feed worker has reported in. Start the market stream worker "
            "(`python -m apps.stream.main`) or the prices below will not update."
        )

    return LiveStatusOut(
        provider=settings.market_data_provider,
        transport=transport,
        delivers_every_update=transport == "websocket",
        poll_interval_seconds=(
            settings.market_stream_poll_interval_seconds if transport == "polling" else None
        ),
        health=FeedHealthOut(**health.to_dict()) if health is not None else None,
        unavailable_reason=unavailable,
    )


@router.get("/quotes", response_model=LiveQuotesOut)
async def live_quotes(
    _user: CurrentUser,
    live: LiveMarketServiceDep,
    instrument_ids: list[uuid.UUID] = Query(min_length=1, max_length=500),
) -> LiveQuotesOut:
    views, missing = await live.live_quotes(instrument_ids)
    return LiveQuotesOut(
        items=[_quote_out(view) for view in views],
        unavailable=missing,
        as_of=datetime.now(UTC),
    )


@router.get("/quotes/{instrument_id}", response_model=LiveQuoteOut)
async def live_quote(
    instrument_id: uuid.UUID, _user: CurrentUser, live: LiveMarketServiceDep
) -> LiveQuoteOut:
    view = await live.live_quote(instrument_id)
    if view is None:
        raise NotFound(
            "Live quote",
            "No live price is held for this instrument. Subscribe to it and check that the "
            "feed worker is running.",
        )
    return _quote_out(view)


@router.post("/subscriptions", response_model=SubscriptionOut)
async def subscribe(
    payload: SubscriptionRequest,
    _user: CurrentUser,
    live: LiveMarketServiceDep,
    instruments: InstrumentServiceDep,
    settings: SettingsDep,
) -> SubscriptionOut:
    """Register interest in instruments so the feed worker starts them.

    Interest expires unless renewed, so a closed browser tab stops costing a
    subscription without anyone having to remember to cancel it.
    """
    for instrument_id in payload.instrument_ids:
        if await instruments.get(instrument_id) is None:
            raise NotFound("Instrument")

    feed = settings.market_data_provider
    subscribed = await live.register_interest(feed, payload.instrument_ids)
    return SubscriptionOut(
        feed=feed,
        instrument_ids=sorted(subscribed, key=str),
        ttl_seconds=settings.live_subscription_ttl_seconds,
    )


@router.delete("/subscriptions", response_model=SubscriptionOut)
async def unsubscribe(
    payload: SubscriptionRequest,
    _user: CurrentUser,
    live: LiveMarketServiceDep,
    settings: SettingsDep,
) -> SubscriptionOut:
    feed = settings.market_data_provider
    remaining = await live.drop_interest(feed, payload.instrument_ids)
    return SubscriptionOut(
        feed=feed,
        instrument_ids=sorted(remaining, key=str),
        ttl_seconds=settings.live_subscription_ttl_seconds,
    )


@router.get("/state", response_model=LiveStateOut)
async def live_state(
    _user: CurrentUser,
    live: LiveMarketServiceDep,
    instrument_ids: list[uuid.UUID] = Query(min_length=1, max_length=500),
    include_quotes: bool = False,
    as_of: datetime | None = None,
) -> LiveStateOut:
    """A content-addressed snapshot assembled from live prices.

    This is the join between the live feed and everything else: downstream
    engines take one of these, exactly as they do for a chain loaded from a file.

    ``as_of`` is the decision time, and it is part of the snapshot's identity.
    Left out it defaults to now, which means every call returns a new
    ``state_id`` even if nothing moved — correct, because two different moments
    are two different snapshots. Pass it to get an id a later call can reproduce,
    and to refuse any quote stamped after it.
    """
    if as_of is not None and as_of.tzinfo is None:
        raise UnprocessableEntity(
            "AS_OF_NOT_TIMEZONE_AWARE",
            "as_of must carry a UTC offset; a naive timestamp does not name a moment.",
        )
    state, unavailable = await live.live_market_state(instrument_ids, as_of)
    payload = state.to_dict(include_quotes=include_quotes)
    return LiveStateOut(
        state_id=state.state_id,
        as_of_timestamp=state.as_of,
        quote_count=len(state.quotes),
        sources=list(state.sources),
        unavailable=unavailable,
        quotes=payload.get("quotes", {}) if include_quotes else {},
    )


@router.post(
    "/options/analyse",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def analyse_live_options(
    payload: LiveChainAnalysisRequest,
    user: CurrentUser,
    jobs: JobServiceDep,
    instruments: InstrumentServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Capture the live chain and take it through to a fitted surface.

    Four stages behind one action: capture into a stored snapshot, solve implied
    volatilities, fit SVI, read the delta-quoted skew off the fit. They run as a
    job because an SVI calibration across a dozen expiries is seconds of
    numerical work, and against one captured moment because a surface assembled
    from prices minutes apart is a surface of a market that never existed.

    Poll ``GET /jobs/{id}``. The result carries the snapshot, analysis and
    surface identifiers, so every number traces back to the quotes it came from.
    """
    underlying = await instruments.get(payload.underlying_id)
    if underlying is None:
        raise NotFound("Instrument")

    job = await jobs.create(
        user.id,
        JobType.ANALYSE_LIVE_CHAIN,
        {
            "underlying_id": str(payload.underlying_id),
            "expiry": payload.expiry.isoformat() if payload.expiry else None,
            "risk_free_rate": payload.risk_free_rate,
            "dividend_yield": payload.dividend_yield,
            "settlement_time_utc": (
                payload.settlement_time_utc.isoformat() if payload.settlement_time_utc else None
            ),
            "calibrate": payload.calibrate,
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.ANALYSE_LIVE_CHAIN),
    )
    await session.commit()
    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


@router.post(
    "/instruments/refresh",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_instrument_master(
    user: CurrentUser,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
    url: str | None = None,
) -> JobAcceptedOut:
    """Reload the provider's instrument file.

    Submitted as a job: the published files are large, and a request that waited
    for one would be a request that timed out.
    """
    if settings.market_data_provider != ProviderKind.UPSTOX:
        raise UnprocessableEntity(
            "INSTRUMENT_MASTER_NOT_APPLICABLE",
            f"the configured market data provider is {settings.market_data_provider!r}, "
            "which does not publish an instrument master",
        )

    job = await jobs.create(
        user.id,
        JobType.LOAD_INSTRUMENT_MASTER,
        {
            "url": url or settings.upstox_instruments_url,
            "segments": list(settings.upstox_segments),
            "underlyings": list(settings.upstox_underlyings),
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.LOAD_INSTRUMENT_MASTER),
    )
    # Durable before dispatch: a worker that picked it up first would not find it.
    await session.commit()
    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))
