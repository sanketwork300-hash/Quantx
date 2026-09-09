"""Phase 3 end to end: a historical dataset, validated, queryable, usable.

*Historical dataset → validated → queryable → usable by the research engine.*

The four verbs are four things a caller can see: a registry row that says what
arrived and what survived, a findings list that says what was wrong with it, a
query that reads the partitions back, and a quarantine that refuses to serve a
dataset validation would not stand behind.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from domains.instruments.enums import AssetClass
from domains.instruments.models import make_instrument
from domains.instruments.service import InstrumentService
from tests.conftest import register_and_login

START = datetime(2026, 3, 2, tzinfo=UTC)


def csv_bars(count: int, symbol: str = "NIFTY", start: float = 100.0, **overrides) -> bytes:
    """A plain historical OHLCV file, the shape a vendor or a broker exports."""
    lines = ["timestamp,symbol,open,high,low,close,volume"]
    for day in range(count):
        price = overrides.get("prices", {}).get(day, start + day)
        moment = (START + timedelta(days=day)).isoformat()
        lines.append(
            f"{moment},{symbol},{price:.2f},{price + 1:.2f},{price - 1:.2f},"
            f"{price:.2f},{1000 + day}"
        )
    return "\n".join(lines).encode()


def parquet_bars(count: int, instrument_id: uuid.UUID) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table(
        {
            "timestamp": pa.array(
                [START + timedelta(days=day) for day in range(count)],
                pa.timestamp("us", tz="UTC"),
            ),
            "open": pa.array([100.0 + day for day in range(count)], pa.float64()),
            "high": pa.array([101.0 + day for day in range(count)], pa.float64()),
            "low": pa.array([99.0 + day for day in range(count)], pa.float64()),
            "close": pa.array([100.0 + day for day in range(count)], pa.float64()),
            "volume": pa.array([1000.0] * count, pa.float64()),
        }
    )
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


@pytest.fixture
async def instrument(db_session):
    """One instrument, so a symbol column has something to resolve against."""
    row = make_instrument(
        asset_class=AssetClass.INDEX, exchange="NSE", symbol="NIFTY", currency="INR"
    )
    await InstrumentService(db_session).upsert(row)
    await db_session.commit()
    return row


async def upload(client, header, data: bytes, filename="bars.csv", content_type="text/csv"):
    response = await client.post(
        "/uploads",
        headers={"Authorization": header},
        files={"file": (filename, data, content_type)},
        data={"kind": "BARS"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def ingest(client, header, upload_id, **overrides) -> dict:
    payload = {
        "upload_id": upload_id,
        "name": "NIFTY daily",
        "exchange": "NSE",
        "corporate_action_treatment": "UNADJUSTED",
        **overrides,
    }
    response = await client.post(
        "/warehouse/datasets", headers={"Authorization": header}, json=payload
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    result = await client.get(f"/jobs/{job_id}/result", headers={"Authorization": header})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "COMPLETED", body
    return body["result"]


class TestRegisteringAHistoricalDataset:
    async def test_a_csv_becomes_a_registered_dataset(self, client, instrument):
        _user, header = await register_and_login(client)
        upload_id = await upload(client, header, csv_bars(10))
        result = await ingest(client, header, upload_id)

        assert result["results"]["status"] == "AVAILABLE"
        assert result["results"]["rows_written"] == 10
        assert result["results"]["partitions"] == 10
        assert result["results"]["instruments"] == 1

    async def test_a_parquet_file_reads_the_same_way(self, client, instrument):
        """One reader and one set of conventions, so the two formats cannot
        disagree about what a column means."""
        _user, header = await register_and_login(client)
        upload_id = await upload(
            client,
            header,
            parquet_bars(5, instrument.id),
            filename="bars.parquet",
            content_type="application/vnd.apache.parquet",
        )
        result = await ingest(client, header, upload_id, instrument_id=str(instrument.id))
        assert result["results"]["rows_written"] == 5

    async def test_every_row_in_the_file_is_accounted_for(self, client, instrument):
        _user, header = await register_and_login(client)
        upload_id = await upload(client, header, csv_bars(10))
        result = await ingest(client, header, upload_id)

        read = result["read"]
        assert read["conserved"] is True
        assert result["results"]["conserved"] is True
        assert result["results"]["rows_in"] == read["rows_read"]

    async def test_the_dataset_is_listed_with_its_quality(self, client, instrument):
        _user, header = await register_and_login(client)
        upload_id = await upload(client, header, csv_bars(10))
        await ingest(client, header, upload_id)

        listed = await client.get("/warehouse/datasets", headers={"Authorization": header})
        assert listed.status_code == 200, listed.text
        item = listed.json()[0]
        assert item["name"] == "NIFTY daily"
        assert item["quality"]["overall_score"] > 0
        assert item["corporate_action_treatment"] == "UNADJUSTED"

    async def test_a_symbol_that_resolves_to_nothing_is_reported_not_guessed(
        self, client, instrument
    ):
        """A bar filed under the wrong instrument is a price series that looks
        perfectly reasonable and is somebody else's."""
        _user, header = await register_and_login(client)
        upload_id = await upload(client, header, csv_bars(5, symbol="NOSUCHTHING"))
        result = await ingest(client, header, upload_id)

        read = result["read"]
        assert read["rows_read"] == 0
        assert read["unresolved_rows"] == 5
        assert read["conserved"] is True
        assert "no instrument on NSE matches" in read["unresolved"][0]["reason"]

    async def test_a_file_missing_a_required_column_fails_as_a_read(self, client, instrument):
        """Rather than registering an empty dataset, which would look exactly
        like a file that genuinely had no rows."""
        _user, header = await register_and_login(client)
        upload_id = await upload(client, header, b"timestamp,symbol,open\n2026-01-01,NIFTY,1\n")

        response = await client.post(
            "/warehouse/datasets",
            headers={"Authorization": header},
            json={"upload_id": upload_id, "name": "broken", "exchange": "NSE"},
        )
        assert response.status_code == 202
        job = await client.get(
            f"/jobs/{response.json()['job_id']}", headers={"Authorization": header}
        )
        assert job.json()["status"] == "FAILED"


class TestValidationTravelsWithTheDataset:
    async def test_the_findings_are_retrievable_in_full(self, client, instrument):
        """The row carries counts and a capped sample; this is every finding,
        which is what makes 'every rejected row reports its reason' true for
        every row rather than for the first twenty-five."""
        _user, header = await register_and_login(client)
        broken = csv_bars(5).decode().splitlines()
        broken.append("2026-03-08T00:00:00+00:00,NIFTY,10,9,11,10,100")  # high below low
        upload_id = await upload(client, header, "\n".join(broken).encode())
        result = await ingest(client, header, upload_id)

        dataset_id = result["results"]["dataset_id"]
        findings = await client.get(
            f"/warehouse/datasets/{dataset_id}/findings", headers={"Authorization": header}
        )
        assert findings.status_code == 200, findings.text
        codes = {item["code"] for item in findings.json()["rejected"]}
        assert "BAR_RANGE_INCONSISTENT" in codes

    async def test_an_undeclared_corporate_action_treatment_is_warned_about(
        self, client, instrument
    ):
        _user, header = await register_and_login(client)
        upload_id = await upload(client, header, csv_bars(5))
        result = await ingest(client, header, upload_id, corporate_action_treatment="UNKNOWN")

        codes = [warning["code"] for warning in result["warnings"]]
        assert "WAREHOUSE_CORPORATE_ACTION_TREATMENT_UNKNOWN" in codes

    async def test_a_split_like_jump_quarantines_the_dataset(self, client, instrument):
        """The platform holds no corporate-action feed. It cannot adjust the
        series, so it refuses to serve it rather than letting a -80% return
        reach a backtest."""
        _user, header = await register_and_login(client)
        prices = {day: 100.0 + day * 0.25 for day in range(50)}
        for day in range(50, 60):
            prices[day] = (100.0 + 49 * 0.25) / 5
        upload_id = await upload(client, header, csv_bars(60, prices=prices))
        result = await ingest(client, header, upload_id)

        assert result["results"]["status"] == "QUARANTINED"
        codes = [warning["code"] for warning in result["warnings"]]
        assert "WAREHOUSE_VALIDATION_FOUND_ERRORS" in codes

    async def test_a_quarantined_dataset_is_not_served(self, client, instrument):
        _user, header = await register_and_login(client)
        prices = {day: 100.0 + day * 0.25 for day in range(50)}
        for day in range(50, 60):
            prices[day] = (100.0 + 49 * 0.25) / 5
        upload_id = await upload(client, header, csv_bars(60, prices=prices))
        result = await ingest(client, header, upload_id)

        response = await client.get(
            "/warehouse/query",
            params={"dataset_id": result["results"]["dataset_id"]},
            headers={"Authorization": header},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "DATASET_QUARANTINED"

    async def test_the_partitions_are_still_written_and_listed(self, client, instrument):
        """Quarantine refuses to *serve* the data; it does not destroy it. The
        findings and the files are both there to be looked at."""
        _user, header = await register_and_login(client)
        prices = {day: 100.0 + day * 0.25 for day in range(50)}
        for day in range(50, 60):
            prices[day] = (100.0 + 49 * 0.25) / 5
        upload_id = await upload(client, header, csv_bars(60, prices=prices))
        result = await ingest(client, header, upload_id)

        partitions = await client.get(
            f"/warehouse/datasets/{result['results']['dataset_id']}/partitions",
            headers={"Authorization": header},
        )
        assert partitions.json()["total_rows"] == 60


class TestQueryingTheWarehouse:
    async def test_the_rows_come_back(self, client, instrument):
        _user, header = await register_and_login(client)
        upload_id = await upload(client, header, csv_bars(10))
        await ingest(client, header, upload_id)

        response = await client.get(
            "/warehouse/query", params={"exchange": "NSE"}, headers={"Authorization": header}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["row_count"] == 10
        assert body["read_path"] == "DIRECT"

    async def test_a_date_range_reads_only_the_days_it_needs(self, client, instrument):
        """The whole point of the partition layout, visible on the response."""
        _user, header = await register_and_login(client)
        upload_id = await upload(client, header, csv_bars(10))
        await ingest(client, header, upload_id)

        response = await client.get(
            "/warehouse/query",
            params={
                "start": (START + timedelta(days=2)).isoformat(),
                "end": (START + timedelta(days=4)).isoformat(),
            },
            headers={"Authorization": header},
        )
        body = response.json()
        assert body["row_count"] == 3
        assert body["partitions_read"] == 3

    async def test_prices_come_back_exact_rather_than_through_a_float(self, client, instrument):
        _user, header = await register_and_login(client)
        upload_id = await upload(client, header, csv_bars(3))
        await ingest(client, header, upload_id)

        response = await client.get(
            "/warehouse/query",
            params={"columns": ["close"], "limit": 1},
            headers={"Authorization": header},
        )
        assert response.json()["rows"][0]["close"] == "100.000000000000"

    async def test_a_truncated_answer_says_so(self, client, instrument):
        _user, header = await register_and_login(client)
        upload_id = await upload(client, header, csv_bars(10))
        await ingest(client, header, upload_id)

        response = await client.get(
            "/warehouse/query", params={"limit": 4}, headers={"Authorization": header}
        )
        assert response.json()["row_count"] == 4
        assert response.json()["truncated"] is True

    async def test_a_naive_timestamp_is_refused(self, client, instrument):
        _user, header = await register_and_login(client)
        response = await client.get(
            "/warehouse/query",
            params={"start": "2026-03-02T00:00:00"},
            headers={"Authorization": header},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "TIMESTAMP_NOT_TIMEZONE_AWARE"

    async def test_the_warehouse_is_readable_by_a_research_caller(self, client, instrument):
        """The acceptance criterion's last step: a research engine asks for a
        window of one instrument's history and gets a usable series back."""
        _user, header = await register_and_login(client)
        upload_id = await upload(client, header, csv_bars(30))
        await ingest(client, header, upload_id)

        response = await client.get(
            "/warehouse/query",
            params={
                "instrument_ids": [str(instrument.id)],
                "columns": ["exchange_timestamp", "close", "volume", "flags"],
                "start": START.isoformat(),
                "end": (START + timedelta(days=29)).isoformat(),
            },
            headers={"Authorization": header},
        )
        body = response.json()
        assert body["row_count"] == 30
        closes = [float(row["close"]) for row in body["rows"]]
        assert closes == sorted(closes), "a research caller needs the series in time order"


class TestOwnership:
    async def test_another_account_cannot_see_the_dataset(self, client, instrument):
        _first, first_header = await register_and_login(client)
        upload_id = await upload(client, first_header, csv_bars(3))
        result = await ingest(client, first_header, upload_id)

        _second, second_header = await register_and_login(client)
        response = await client.get(
            f"/warehouse/datasets/{result['results']['dataset_id']}",
            headers={"Authorization": second_header},
        )
        assert response.status_code == 404

    async def test_every_route_requires_a_signed_in_caller(self, client):
        for path in ("/warehouse/datasets", "/warehouse/query"):
            assert (await client.get(path)).status_code == 401


class TestTimestampsAreNotAssumed:
    async def test_a_file_with_no_offset_is_refused_rather_than_read_as_utc(
        self, client, instrument
    ):
        """A year of NSE bars stamped in local time and read as UTC is a year of
        bars shifted by five and a half hours, and nothing downstream would say
        so. The shared CSV parser defaults a naive timestamp to UTC, which is
        right for an option chain whose as-of moment is supplied separately and
        wrong here — so the reader hands the validator the naive value it
        actually had.
        """
        _user, header = await register_and_login(client)
        naive = "\n".join(
            [
                "timestamp,symbol,open,high,low,close,volume",
                "2026-03-02 09:15:00,NIFTY,100,101,99,100,1000",
                "2026-03-03 09:15:00,NIFTY,101,102,100,101,1000",
            ]
        ).encode()
        upload_id = await upload(client, header, naive)
        result = await ingest(client, header, upload_id)

        assert result["results"]["rows_written"] == 0
        assert result["results"]["rows_rejected"] == 2
        assert (
            result["results"]["validation"]["counts_by_code"]["TIMESTAMP_NOT_TIMEZONE_AWARE"] == 2
        )

    async def test_an_offset_is_honoured_rather_than_overwritten(self, client, instrument):
        """A file stamped in the venue's own zone partitions on the UTC day the
        moment actually falls in."""
        _user, header = await register_and_login(client)
        ist = "\n".join(
            [
                "timestamp,symbol,open,high,low,close,volume",
                "2026-03-03T02:00:00+05:30,NIFTY,100,101,99,100,1000",
            ]
        ).encode()
        upload_id = await upload(client, header, ist)
        result = await ingest(client, header, upload_id)

        assert result["results"]["rows_written"] == 1
        partitions = await client.get(
            f"/warehouse/datasets/{result['results']['dataset_id']}/partitions",
            headers={"Authorization": header},
        )
        # 02:00 IST on the 3rd is 20:30 UTC on the 2nd.
        assert partitions.json()["items"][0]["day"] == "2026-03-02"
