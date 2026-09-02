"""Instrument master and resolution through the API."""

from __future__ import annotations

import uuid

import pytest

CALL = {
    "asset_class": "OPTION",
    "exchange": "NSE",
    "symbol": "NIFTY",
    "currency": "INR",
    "multiplier": "75",
    "tick_size": "0.05",
    "lot_size": "75",
    "expiry": "2026-09-24",
    "strike": "24000",
    "option_type": "CALL",
    "exercise_style": "EUROPEAN",
    "settlement_type": "CASH",
}
INDEX = {
    "asset_class": "INDEX",
    "exchange": "NSE",
    "symbol": "NIFTY",
    "currency": "INR",
}


async def create(client, header, payload):
    response = await client.post("/instruments", headers={"Authorization": header}, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def underlying(client, auth_header):
    return await create(client, auth_header, INDEX)


class TestCreation:
    async def test_creates_with_a_canonical_key_and_derived_id(self, client, auth_header):
        instrument = await create(client, auth_header, INDEX)
        assert instrument["canonical_key"] == "NSE:INDEX:NIFTY"
        assert uuid.UUID(instrument["id"])

    async def test_creation_is_idempotent(self, client, auth_header):
        first = await create(client, auth_header, INDEX)
        second = await create(client, auth_header, INDEX)
        assert first["id"] == second["id"]

        listing = await client.get(
            "/instruments?asset_class=INDEX", headers={"Authorization": auth_header}
        )
        assert len(listing.json()["items"]) == 1

    async def test_option_creation(self, client, auth_header, underlying):
        option = await create(client, auth_header, {**CALL, "underlying_id": underlying["id"]})
        assert option["canonical_key"] == "NSE:OPTION:NIFTY:2026-09-24:24000:C"
        assert option["underlying_id"] == underlying["id"]

    async def test_an_invalid_instrument_is_rejected_with_a_reason(self, client, auth_header):
        response = await client.post(
            "/instruments",
            headers={"Authorization": auth_header},
            json={**CALL, "strike": None},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_INSTRUMENT"

    async def test_a_zero_multiplier_is_rejected(self, client, auth_header, underlying):
        response = await client.post(
            "/instruments",
            headers={"Authorization": auth_header},
            json={**CALL, "underlying_id": underlying["id"], "multiplier": "0"},
        )
        assert response.status_code == 422


class TestRetrieval:
    async def test_get_by_id(self, client, auth_header, underlying):
        response = await client.get(
            f"/instruments/{underlying['id']}", headers={"Authorization": auth_header}
        )
        assert response.status_code == 200
        assert response.json()["canonical_key"] == "NSE:INDEX:NIFTY"

    async def test_unknown_id_is_not_found(self, client, auth_header):
        response = await client.get(
            f"/instruments/{uuid.uuid4()}", headers={"Authorization": auth_header}
        )
        assert response.status_code == 404

    async def test_filtering(self, client, auth_header, underlying):
        await create(client, auth_header, {**CALL, "underlying_id": underlying["id"]})
        await create(
            client,
            auth_header,
            {**CALL, "underlying_id": underlying["id"], "option_type": "PUT"},
        )
        response = await client.get(
            "/instruments?asset_class=OPTION&option_type=CALL",
            headers={"Authorization": auth_header},
        )
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["option_type"] == "CALL"

    async def test_decimals_are_serialised_as_strings(self, client, auth_header, underlying):
        option = await create(client, auth_header, {**CALL, "underlying_id": underlying["id"]})
        assert option["multiplier"] == "75"
        assert option["strike"] == "24000"
        assert isinstance(option["tick_size"], str)


class TestResolution:
    async def test_structured_request_resolves_exactly(self, client, auth_header, underlying):
        option = await create(client, auth_header, {**CALL, "underlying_id": underlying["id"]})
        response = await client.post(
            "/instruments/resolve",
            headers={"Authorization": auth_header},
            json={
                "requests": [
                    {
                        "symbol": "NIFTY",
                        "exchange": "NSE",
                        "asset_class": "OPTION",
                        "expiry": "2026-09-24",
                        "strike": "24000",
                        "option_type": "CALL",
                    }
                ]
            },
        )
        result = response.json()["results"][0]
        assert result["status"] == "RESOLVED"
        assert result["instrument_id"] == option["id"]
        assert result["confidence"] == 1.0

    async def test_unknown_symbol_is_unresolved_not_guessed(self, client, auth_header):
        response = await client.post(
            "/instruments/resolve",
            headers={"Authorization": auth_header},
            json={"requests": [{"symbol": "NOTLISTED", "exchange": "NSE"}]},
        )
        result = response.json()["results"][0]
        assert result["status"] == "UNRESOLVED"
        assert result["instrument_id"] is None
        assert result["reason"] == "NO_MATCH"

    async def test_multiple_candidates_are_ambiguous_with_the_candidates_returned(
        self, client, auth_header, underlying
    ):
        """The platform never picks a 'most likely' contract on the user's
        behalf: that is how a portfolio silently gets the wrong expiry."""
        await create(client, auth_header, {**CALL, "underlying_id": underlying["id"]})
        await create(
            client,
            auth_header,
            {**CALL, "underlying_id": underlying["id"], "expiry": "2026-10-29"},
        )
        response = await client.post(
            "/instruments/resolve",
            headers={"Authorization": auth_header},
            json={
                "requests": [
                    {
                        "symbol": "NIFTY",
                        "exchange": "NSE",
                        "asset_class": "OPTION",
                        "strike": "24000",
                        "option_type": "CALL",
                    }
                ]
            },
        )
        result = response.json()["results"][0]
        assert result["status"] == "AMBIGUOUS"
        assert result["instrument_id"] is None
        assert result["reason"] == "MULTIPLE_CANDIDATES"
        assert len(result["candidates"]) == 2

    async def test_alias_resolution(self, client, auth_header, underlying):
        option = await create(client, auth_header, {**CALL, "underlying_id": underlying["id"]})
        response = await client.post(
            f"/instruments/{option['id']}/aliases",
            headers={"Authorization": auth_header},
            json={"source": "broker-x", "alias_symbol": "NIFTY26SEP24000CE"},
        )
        assert response.status_code == 201

        resolved = await client.post(
            "/instruments/resolve",
            headers={"Authorization": auth_header},
            json={"requests": [{"symbol": "NIFTY26SEP24000CE", "source": "broker-x"}]},
        )
        result = resolved.json()["results"][0]
        assert result["status"] == "RESOLVED"
        assert result["method"] == "ALIAS"
        assert result["instrument_id"] == option["id"]

    async def test_batch_resolution_preserves_order(self, client, auth_header, underlying):
        response = await client.post(
            "/instruments/resolve",
            headers={"Authorization": auth_header},
            json={
                "requests": [
                    {"canonical_key": "NSE:INDEX:NIFTY"},
                    {"symbol": "NOTLISTED"},
                    {"instrument_id": underlying["id"]},
                ]
            },
        )
        statuses = [item["status"] for item in response.json()["results"]]
        assert statuses == ["RESOLVED", "UNRESOLVED", "RESOLVED"]
