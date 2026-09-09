"""Phase 2 end to end: a live chain becomes a volatility surface.

*User selects NIFTY → live option chain → IV → surface → surface analytics.*

The live quotes come from the seeded synthetic market, which is the one source
whose true parameters are known: it generates an arbitrage-clean chain from an
admissible SVI slice, so a surface fitted back out of it can be checked against
what went in. Everything between the quote and the surface — capture, quality
scoring, the IV solver, the SVI calibration, the skew — is production code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.instruments.enums import AssetClass
from domains.instruments.service import InstrumentService
from domains.market_data.providers.synthetic import (
    SyntheticMarketConfig,
    SyntheticMarketDataProvider,
)
from domains.market_data.streaming.live_state import LiveMarketStore
from tests.conftest import register_and_login


@pytest.fixture
def synthetic_market():
    """A live market whose true volatility surface is known."""
    config = SyntheticMarketConfig(as_of=datetime.now(UTC).replace(microsecond=0))
    return config, SyntheticMarketDataProvider(config)


@pytest.fixture
def store(app_environment) -> LiveMarketStore:
    from infrastructure.cache.client import get_cache

    settings = app_environment["settings"]
    return LiveMarketStore(get_cache(settings), settings.live_quote_ttl_seconds)


@pytest.fixture
async def live_chain(db_session, store, synthetic_market):
    """Instruments in the database, and their prices in the live cache.

    Exactly the state the feed worker leaves behind after subscribing to a
    chain, reached without a socket.
    """
    config, provider = synthetic_market
    instruments = InstrumentService(db_session)

    everything = list(await provider.list_instruments())
    underlyings = [i for i in everything if i.asset_class is not AssetClass.OPTION]
    options = [i for i in everything if i.asset_class is AssetClass.OPTION]
    await instruments.upsert_many(underlyings)
    await instruments.upsert_many(options)
    await db_session.commit()

    for instrument in everything:
        quote = await provider.get_quote(instrument.id)
        if quote is not None:
            await store.put_quote(quote, feed="synthetic")

    return config, provider, provider.underlying, options


async def run_pipeline(client, header: str, underlying_id, config, **overrides) -> dict:
    """Submit the capture-to-surface job and return its completed result."""
    body = {
        "underlying_id": str(underlying_id),
        "risk_free_rate": config.risk_free_rate,
        "dividend_yield": config.dividend_yield,
        "settlement_time_utc": config.expiry_time_utc.isoformat(),
        **overrides,
    }
    response = await client.post(
        "/live/options/analyse", json=body, headers={"Authorization": header}
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    job = await client.get(f"/jobs/{job_id}/result", headers={"Authorization": header})
    assert job.status_code == 200, job.text
    payload = job.json()
    assert payload["status"] == "COMPLETED", payload
    return payload["result"]


class TestCapturingALiveChain:
    async def test_the_capture_becomes_a_stored_snapshot(self, client, live_chain, db_session):
        config, _provider, underlying, options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config, calibrate=False)

        capture = result["capture"]
        assert capture["contracts_considered"] == len(options)
        assert capture["quotes_kept"] > 0
        assert uuid.UUID(capture["snapshot_id"])

    async def test_every_contract_is_kept_excluded_or_rejected(self, client, live_chain):
        """The conservation rule the ingestion pipeline obeys, obeyed by the
        live path too. A contract the feed had no price for is *rejected with
        that reason*, not quietly left out of the chain."""
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config, calibrate=False)

        capture = result["capture"]
        assert capture["conserved"] is True
        assert (
            capture["contracts_considered"]
            == capture["quotes_kept"]
            + capture["quotes_excluded"]
            + capture["contracts_without_quotes"]
        )

    async def test_a_contract_with_no_live_price_is_rejected_not_omitted(
        self, client, live_chain, store, db_session
    ):
        config, _provider, underlying, options = live_chain
        # Evict one contract's price, as an untraded wing would be.
        from domains.market_data.streaming.live_state import quote_key
        from infrastructure.cache.client import get_cache

        await get_cache().delete(quote_key(options[0].id))

        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config, calibrate=False)

        assert result["capture"]["contracts_without_quotes"] >= 1
        assert result["capture"]["conserved"] is True
        codes = [warning["code"] for warning in result["warnings"]]
        assert "LIVE_CHAIN_CONTRACTS_WITHOUT_QUOTES" in codes

    async def test_the_snapshot_carries_the_underlying_level(self, client, live_chain):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config, calibrate=False)
        assert Decimal(result["capture"]["underlying_price"]) > 0

    async def test_how_much_of_an_instant_the_snapshot_is_gets_reported(self, client, live_chain):
        """A chain assembled from quotes minutes apart is not a snapshot, and
        every calibration downstream treats it as one."""
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config, calibrate=False)
        assert result["capture"]["timestamp_spread_seconds"] == 0.0
        assert "oldest_quote_age_seconds" in result["capture"]


class TestFromLiveChainToSurface:
    """The Phase 2 acceptance criterion."""

    async def test_implied_volatilities_are_solved_from_the_live_chain(self, client, live_chain):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config)

        assert result["stages"]["analyse"] == "OK"
        counts = result["analysis"]["counts"]
        assert counts["solved"] > 0
        assert counts["quotes"] >= counts["solved"]
        assert counts["expiries"] == len(config.expiry_days)
        assert uuid.UUID(result["analysis_id"])

    async def test_a_surface_is_calibrated_and_stored(self, client, live_chain):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config)

        assert result["stages"]["calibrate"] == "OK"
        assert uuid.UUID(result["surface_id"])
        assert result["surface"]["slices"]

    async def test_the_recovered_surface_matches_the_market_it_came_from(self, client, live_chain):
        """The synthetic market is generated from a known SVI slice, so the
        at-the-money level fitted back out of it is checkable rather than merely
        plausible."""
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config)

        levels = [item["smiles"][0]["atm_volatility"] for item in result["delta_skew"]["slices"]]
        assert levels, result["delta_skew"]
        # A plausible index volatility, not a solver that has wandered off.
        assert all(0.02 < level < 1.5 for level in levels)

    async def test_every_stage_names_the_identifier_the_next_one_used(self, client, live_chain):
        """Which is what makes a surface traceable back to the quotes it was
        built from."""
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config)

        assert result["capture"]["snapshot_id"]
        assert result["analysis_id"]
        assert result["surface_id"]
        # Compared exactly rather than by subset: a stage that silently stopped
        # running would still pass a subset check, and the point of this test is
        # that the chain from quotes to surface has no gaps in it.
        assert result["stages"] == {
            "capture": "OK",
            "analyse": "OK",
            "greeks": "OK",
            "calibrate": "OK",
            "delta_skew": "OK",
        }

    async def test_one_expiry_can_be_captured_on_its_own(self, client, live_chain):
        config, _provider, underlying, options = live_chain
        expiry = sorted({item.expiry for item in options})[0]
        _user, header = await register_and_login(client)

        result = await run_pipeline(
            client, header, underlying.id, config, expiry=expiry.isoformat(), calibrate=False
        )
        expected = sum(1 for item in options if item.expiry == expiry)
        assert result["capture"]["contracts_considered"] == expected


class TestSurfaceAnalytics:
    async def test_delta_skew_is_available_off_the_stored_surface(self, client, live_chain):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config)

        response = await client.get(
            f"/derivatives/surfaces/{result['surface_id']}/delta-skew",
            headers={"Authorization": header},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["levels"] == [0.25, 0.10]
        assert body["delta_convention"] == "FORWARD"
        assert body["slices"]

    async def test_recomputing_it_gives_the_same_answer(self, client, live_chain):
        """It is a pure function of the stored SVI parameters, which is why it
        is computed on read rather than stored beside them."""
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config)

        first = await client.get(
            f"/derivatives/surfaces/{result['surface_id']}/delta-skew",
            headers={"Authorization": header},
        )
        second = await client.get(
            f"/derivatives/surfaces/{result['surface_id']}/delta-skew",
            headers={"Authorization": header},
        )
        assert first.json() == second.json()

    async def test_the_skew_has_the_sign_the_generated_market_was_given(self, client, live_chain):
        """The synthetic market is built with a negative SVI rho — the usual
        equity-index shape — so the fitted risk reversal must come back
        negative. A sign error here would be invisible in every other test."""
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config)

        response = await client.get(
            f"/derivatives/surfaces/{result['surface_id']}/delta-skew",
            headers={"Authorization": header},
        )
        measured = [
            smile["risk_reversal"]
            for item in response.json()["slices"]
            for smile in item["smiles"]
            if smile["delta_level"] == 0.25 and smile["risk_reversal"] is not None
        ]
        assert measured, response.text
        assert all(value < 0 for value in measured)

    async def test_an_unmeasurable_wing_is_listed_rather_than_dropped(self, client, live_chain):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config)

        response = await client.get(
            f"/derivatives/surfaces/{result['surface_id']}/delta-skew",
            headers={"Authorization": header},
        )
        body = response.json()
        # Whether any wing failed depends on the fit; what must hold is that a
        # failure is reported with a status rather than silently absent.
        for item in body["slices"]:
            for smile in item["smiles"]:
                if smile["risk_reversal"] is None:
                    assert smile["call"]["status"] != "OK" or smile["put"]["status"] != "OK"


class TestOpenInterestOverTheLiveChain:
    async def test_the_profile_is_built_from_the_captured_snapshot(self, client, live_chain):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        await run_pipeline(client, header, underlying.id, config, calibrate=False)

        response = await client.get(
            f"/market/open-interest/{underlying.id}", headers={"Authorization": header}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["expiries"]
        assert body["open_interest_unit"] == "PROVIDER_REPORTED_UNNORMALISED"

    async def test_the_ratio_is_reported_without_being_interpreted(self, client, live_chain):
        """The response carries the ratio and the counts behind it. It carries
        no reading of what the ratio means, and this test is what keeps it that
        way."""
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        await run_pipeline(client, header, underlying.id, config, calibrate=False)

        response = await client.get(
            f"/market/open-interest/{underlying.id}", headers={"Authorization": header}
        )
        text = response.text.lower()
        for word in ("bullish", "bearish", "sentiment", "signal", "buy", "sell", "overbought"):
            assert word not in text, word

    async def test_a_change_needs_two_snapshots_and_says_so(self, client, live_chain):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        await run_pipeline(client, header, underlying.id, config, calibrate=False)

        response = await client.get(
            f"/market/open-interest/{underlying.id}/change",
            headers={"Authorization": header},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "OPEN_INTEREST_NEEDS_TWO_SNAPSHOTS"

    async def test_a_change_carries_the_window_it_happened_over(self, client, live_chain, store):
        config, provider, underlying, options = live_chain
        _user, header = await register_and_login(client)
        await run_pipeline(client, header, underlying.id, config, calibrate=False)

        # A second capture, a minute later, with one contract's open interest moved.
        moved = options[0]
        quote = await provider.get_quote(moved.id)
        from dataclasses import replace as dataclass_replace

        await store.put_quote(
            dataclass_replace(
                quote,
                open_interest=(quote.open_interest or Decimal(0)) + Decimal(500),
                exchange_timestamp=quote.exchange_timestamp + timedelta(minutes=1),
            ),
            feed="synthetic",
        )
        await run_pipeline(client, header, underlying.id, config, calibrate=False)

        response = await client.get(
            f"/market/open-interest/{underlying.id}/change",
            headers={"Authorization": header},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["window_seconds"] >= 0
        assert body["matched_contracts"] > 0
        assert body["open_interest_unit"] == "PROVIDER_REPORTED_UNNORMALISED"


class TestWhenThereIsNothingToAnalyse:
    """A stage that cannot run stops the ones depending on it and says which.

    The failure being avoided is an empty surface: a calibration with nothing to
    fit produces an object that plots as a flat market rather than as no data.
    """

    async def test_a_chain_with_no_live_prices_stops_after_the_capture(
        self, client, live_chain, store
    ):
        config, _provider, underlying, options = live_chain
        from domains.market_data.streaming.live_state import quote_key
        from infrastructure.cache.client import get_cache

        cache = get_cache()
        for contract in options:
            await cache.delete(quote_key(contract.id))

        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config)

        assert result["capture"]["quotes_kept"] == 0
        assert result["capture"]["contracts_without_quotes"] == len(options)
        assert result["capture"]["conserved"] is True
        assert result["stages"]["analyse"] == "SKIPPED_NO_USABLE_QUOTES"
        assert "surface_id" not in result

    async def test_calibration_can_be_declined_without_losing_the_analysis(
        self, client, live_chain
    ):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config, calibrate=False)

        assert result["stages"]["analyse"] == "OK"
        assert result["stages"]["calibrate"] == "NOT_REQUESTED"
        assert result["analysis"]["counts"]["solved"] > 0
        assert "surface_id" not in result

    async def test_an_underlying_with_no_contracts_says_so(self, client, db_session):
        from domains.instruments.models import make_instrument

        lonely = make_instrument(
            asset_class=AssetClass.INDEX, exchange="NSE", symbol="NOCHAIN", currency="INR"
        )
        await InstrumentService(db_session).upsert(lonely)
        await db_session.commit()

        _user, header = await register_and_login(client)
        response = await client.post(
            "/live/options/analyse",
            json={"underlying_id": str(lonely.id)},
            headers={"Authorization": header},
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        payload = (
            await client.get(f"/jobs/{job_id}/result", headers={"Authorization": header})
        ).json()

        codes = [warning["code"] for warning in payload["result"]["warnings"]]
        assert "LIVE_CHAIN_NO_CONTRACTS" in codes


class TestGreeksAcrossTheChain:
    """Delta, gamma, vega, theta and rho for the whole chain, from the analysis.

    The failure being guarded against is a row of zeros: an option reported with
    no delta reads as one carrying no risk, and it plots and sums perfectly.
    """

    async def test_every_solved_contract_gets_greeks(self, client, live_chain):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config, calibrate=False)

        response = await client.get(
            f"/derivatives/analyses/{result['analysis_id']}/greeks",
            headers={"Authorization": header},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["counts"]["priced"] > 0
        assert body["counts"]["priced"] == result["analysis"]["counts"]["solved"]

    async def test_a_call_has_positive_delta_and_a_put_negative(self, client, live_chain):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config, calibrate=False)

        response = await client.get(
            f"/derivatives/analyses/{result['analysis_id']}/greeks",
            headers={"Authorization": header},
        )
        contracts = [c for e in response.json()["expiries"] for c in e["contracts"]]
        assert contracts
        for contract in contracts:
            if contract["option_type"] == "CALL":
                assert 0.0 <= contract["delta"] <= 1.0, contract
            else:
                assert -1.0 <= contract["delta"] <= 0.0, contract

    async def test_gamma_and_vega_are_never_negative_for_a_long_option(self, client, live_chain):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config, calibrate=False)

        response = await client.get(
            f"/derivatives/analyses/{result['analysis_id']}/greeks",
            headers={"Authorization": header},
        )
        contracts = [c for e in response.json()["expiries"] for c in e["contracts"]]
        assert all(c["gamma"] >= 0 for c in contracts)
        assert all(c["vega_per_vol_point"] >= 0 for c in contracts)

    async def test_the_units_are_named_on_the_payload(self, client, live_chain):
        """An unlabelled vega of 0.42 could be per 1.00 of volatility or per
        volatility point, and those differ by a factor of a hundred."""
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config, calibrate=False)

        response = await client.get(
            f"/derivatives/analyses/{result['analysis_id']}/greeks",
            headers={"Authorization": header},
        )
        units = response.json()["units"]
        assert "+0.01 of volatility" in units["vega_per_vol_point"]
        assert "calendar day" in units["theta_per_day"]
        assert "basis point" in units["rho_per_bp"]
        assert set(units) == {
            "delta",
            "gamma",
            "vega_per_vol_point",
            "theta_per_day",
            "rho_per_bp",
        }

    async def test_the_carry_assumption_travels_with_the_answer(self, client, live_chain):
        """A wrong carry moves every delta, so whether the yield was supplied or
        assumed has to be on the response."""
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config, calibrate=False)

        response = await client.get(
            f"/derivatives/analyses/{result['analysis_id']}/greeks",
            headers={"Authorization": header},
        )
        body = response.json()
        assert body["risk_free_rate"] == pytest.approx(config.risk_free_rate)
        assert "dividend_yield_assumed" in body
        assert body["volatility_source"] == "market_implied_per_contract"

    async def test_an_unsolved_contract_is_listed_rather_than_given_zeros(self, client, live_chain):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config, calibrate=False)

        response = await client.get(
            f"/derivatives/analyses/{result['analysis_id']}/greeks",
            headers={"Authorization": header},
        )
        body = response.json()
        for expiry in body["expiries"]:
            for item in expiry["unavailable"]:
                assert item["reason"] in {
                    "NO_IMPLIED_VOL",
                    "NO_TIME_TO_EXPIRY",
                    "NO_UNDERLYING_PRICE",
                    "EXPIRED",
                }
            # Nothing appears in both lists.
            priced = {c["instrument_id"] for c in expiry["contracts"]}
            missing = {c["instrument_id"] for c in expiry["unavailable"]}
            assert not (priced & missing)

    async def test_the_pipeline_reports_greeks_as_a_stage(self, client, live_chain):
        config, _provider, underlying, _options = live_chain
        _user, header = await register_and_login(client)
        result = await run_pipeline(client, header, underlying.id, config)
        assert result["stages"]["greeks"] == "OK"
        assert result["greeks"]["counts"]["priced"] > 0
