"""Phase 10 end to end: import an L2 dataset, gate it, measure it, model it.

Carries the Phase 10 acceptance criteria from docs/backlog.md over the wire.
The through-line is the gate: a capability the data cannot support has to be a
stated refusal at the boundary, not a number with a caveat, and there has to be
no way round it from the outside.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from tests.conftest import register_and_login
from tests.integration.test_derivatives import ingest_clean_chain

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SNAPSHOT_CSV = (DATA_DIR / "orderbook_snapshots.csv").read_bytes()
EVENT_CSV = (DATA_DIR / "orderbook_events.csv").read_bytes()
BOOK_PARQUET = (DATA_DIR / "orderbook.parquet").read_bytes()


async def upload(client, header, name: str, data: bytes, kind: str) -> str:
    response = await client.post(
        "/uploads",
        headers={"Authorization": header},
        files={"file": (name, data, "text/csv")},
        data={"kind": kind},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def run_job(client, header, job_id) -> dict:
    job = await client.get(f"/jobs/{job_id}", headers={"Authorization": header})
    assert job.json()["status"] == "COMPLETED", job.json()
    result = await client.get(f"/jobs/{job_id}/result", headers={"Authorization": header})
    return result.json()["result"]


async def atm_call(client, header) -> str:
    """The contract the L2 fixture was generated around.

    The book fixture is built on the at-the-money call of the near expiry in
    ``options_chain_clean.csv``, so the two fixtures describe one contract
    rather than two unrelated synthetic markets.
    """
    response = await client.get(
        "/instruments?asset_class=OPTION&option_type=CALL&limit=1000",
        headers={"Authorization": header},
    )
    assert response.status_code == 200, response.text
    options = response.json()["items"]
    near = min(item["expiry"] for item in options)
    at_the_money = [
        item for item in options if item["expiry"] == near and item["strike"].startswith("24000")
    ]
    assert at_the_money, "the fixture chain should contain a 24000 call"
    return at_the_money[0]["id"]


def without_events(data: bytes) -> bytes:
    """Only the depth half, for the snapshot-only refusal cases."""
    return data


def truncated_events(data: bytes, rows: int) -> bytes:
    """The first ``rows`` events, for the too-thin refusals."""
    text = data.decode("utf-8")
    reader = list(csv.reader(io.StringIO(text)))
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerows(reader[: rows + 1])
    return out.getvalue().encode("utf-8")


def coarsen_event_clock(data: bytes) -> bytes:
    """Round every timestamp to the second, so most consecutive events tie."""
    reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
    rows = list(reader)
    for row in rows:
        stamp = row["TIME"]
        if "." in stamp:
            row["TIME"] = stamp.split(".")[0] + "+00:00"
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def strip_sequence_numbers(data: bytes) -> bytes:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
    rows = list(reader)
    for row in rows:
        row["SEQNO"] = ""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


@pytest.fixture
async def instrument_id(client, auth_header, clean_chain_csv):
    await ingest_clean_chain(client, auth_header, clean_chain_csv)
    return await atm_call(client, auth_header)


async def import_dataset(
    client,
    header,
    instrument_id: str,
    snapshot_data: bytes | None = SNAPSHOT_CSV,
    event_data: bytes | None = EVENT_CSV,
    name: str = "NIFTY ATM call, 30 minutes",
) -> dict:
    payload: dict = {"instrument_id": instrument_id, "name": name}
    if snapshot_data is not None:
        payload["snapshot_upload_id"] = await upload(
            client, header, "book.csv", snapshot_data, "BOOK_SNAPSHOTS"
        )
    if event_data is not None:
        payload["event_upload_id"] = await upload(
            client, header, "events.csv", event_data, "BOOK_EVENTS"
        )

    accepted = await client.post(
        "/microstructure/datasets", headers={"Authorization": header}, json=payload
    )
    assert accepted.status_code == 202, accepted.text
    return await run_job(client, header, accepted.json()["job_id"])


@pytest.fixture
async def dataset(client, auth_header, instrument_id):
    body = await import_dataset(client, auth_header, instrument_id)
    return body["results"]["dataset_id"]


class TestImport:
    async def test_the_preview_shows_the_detected_columns_and_writes_nothing(
        self, client, auth_header, instrument_id
    ):
        snapshot_upload = await upload(
            client, auth_header, "book.csv", SNAPSHOT_CSV, "BOOK_SNAPSHOTS"
        )
        event_upload = await upload(client, auth_header, "events.csv", EVENT_CSV, "BOOK_EVENTS")
        response = await client.post(
            "/microstructure/datasets/preview",
            headers={"Authorization": auth_header},
            json={
                "instrument_id": instrument_id,
                "snapshot_upload_id": snapshot_upload,
                "event_upload_id": event_upload,
                "limit": 200,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()

        detected = body["detected_snapshot_columns"]
        assert detected["timestamp"] == "TIMESTAMP"
        assert detected["depth"] == 5
        assert detected["levels"]["bid"]["1"]["price"] == "BID_PX_1"
        assert detected["levels"]["ask"]["1"]["size"] == "ASK1QTY"
        assert body["detected_event_mapping"]["event_type"] == "ACTION"
        assert body["committable"] is True

        listed = await client.get(
            "/microstructure/datasets", headers={"Authorization": auth_header}
        )
        assert listed.json() == []

    async def test_nothing_is_dropped_without_a_reason(self, client, auth_header, dataset):
        response = await client.get(
            f"/microstructure/datasets/{dataset}", headers={"Authorization": auth_header}
        )
        rows = response.json()["results"]["rows"]
        for half in ("snapshots", "events"):
            counts = rows[half]
            assert counts["input"] == counts["kept"] + counts["rejected"]
            assert counts["rejected"] > 0
        assert sum(rows["rejection_counts"].values()) == (
            rows["snapshots"]["rejected"] + rows["events"]["rejected"]
        )

    async def test_every_rejected_row_names_its_row_number_and_reason(
        self, client, auth_header, dataset
    ):
        response = await client.get(
            f"/microstructure/datasets/{dataset}/rejections",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        rejections = body["snapshot_rejections"] + body["event_rejections"]
        assert len(rejections) == sum(body["counts"].values())
        for row in rejections:
            assert row["row_number"] >= 1
            assert row["reason"]
            assert row["message"]

    async def test_the_fixture_seeds_every_reason_the_importers_can_give(
        self, client, auth_header, dataset
    ):
        response = await client.get(
            f"/microstructure/datasets/{dataset}", headers={"Authorization": auth_header}
        )
        counts = response.json()["results"]["rows"]["rejection_counts"]
        assert {
            "MISSING_TIMESTAMP",
            "NEGATIVE_PRICE",
            "NEGATIVE_QUANTITY",
            "LEVELS_OUT_OF_ORDER",
            "PRICE_WITHOUT_QUANTITY",
            "NO_LEVELS",
            "DUPLICATE_OBSERVATION",
            "UNPARSEABLE_ROW",
            "MISSING_EVENT_TYPE",
            "UNRECOGNISED_EVENT_TYPE",
            "UNRECOGNISED_SIDE",
        } <= set(counts)

    async def test_a_canonical_parquet_upload_is_read_directly(
        self, client, auth_header, instrument_id
    ):
        """Parquet is what a capture pipeline writes and what the platform
        stores, so it is accepted as an upload rather than only as an export."""
        response = await client.post(
            "/uploads",
            headers={"Authorization": auth_header},
            files={"file": ("book.parquet", BOOK_PARQUET, "application/vnd.apache.parquet")},
            data={"kind": "BOOK_SNAPSHOTS"},
        )
        assert response.status_code == 201, response.text
        accepted = await client.post(
            "/microstructure/datasets",
            headers={"Authorization": auth_header},
            json={
                "instrument_id": instrument_id,
                "name": "parquet import",
                "snapshot_upload_id": response.json()["id"],
            },
        )
        assert accepted.status_code == 202, accepted.text
        body = await run_job(client, auth_header, accepted.json()["job_id"])
        assert body["status"] == "OK"

        detail = await client.get(
            f"/microstructure/datasets/{body['results']['dataset_id']}",
            headers={"Authorization": auth_header},
        )
        rows = detail.json()["results"]["rows"]["snapshots"]
        assert rows["kept"] == 361
        assert rows["rejected"] == 0

    async def test_a_truncated_parquet_file_is_refused_at_the_boundary(self, client, auth_header):
        response = await client.post(
            "/uploads",
            headers={"Authorization": auth_header},
            files={"file": ("book.parquet", BOOK_PARQUET[:2000], "application/octet-stream")},
            data={"kind": "BOOK_SNAPSHOTS"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "UPLOAD_PARQUET_TRUNCATED"

    async def test_a_snapshot_file_uploaded_as_an_event_tape_is_refused(
        self, client, auth_header, instrument_id
    ):
        """The two shapes are read by different parsers, so a mislabelled file
        is a refusal rather than a tape of unparseable rows."""
        upload_id = await upload(client, auth_header, "book.csv", SNAPSHOT_CSV, "BOOK_SNAPSHOTS")
        response = await client.post(
            "/microstructure/datasets",
            headers={"Authorization": auth_header},
            json={
                "instrument_id": instrument_id,
                "name": "wrong kind",
                "event_upload_id": upload_id,
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "WRONG_UPLOAD_KIND"

    async def test_book_kinds_are_routed_away_from_the_chain_ingest(self, client, auth_header):
        upload_id = await upload(client, auth_header, "book.csv", SNAPSHOT_CSV, "BOOK_SNAPSHOTS")
        response = await client.post(
            f"/uploads/{upload_id}/ingest",
            headers={"Authorization": auth_header},
            json={
                "kind": "BOOK_SNAPSHOTS",
                "underlying": {"symbol": "NIFTY", "exchange": "SYNTH", "currency": "INR"},
                "as_of_timestamp": "2026-09-24T09:20:00+00:00",
                "column_mapping": {},
            },
        )
        assert response.status_code == 400
        assert "microstructure" in response.json()["detail"]


class TestTheGate:
    async def test_a_full_dataset_is_granted_every_capability(self, client, auth_header, dataset):
        response = await client.get(
            f"/microstructure/datasets/{dataset}/capabilities",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200, response.text
        granted = {item["capability"] for item in response.json() if item["available"]}
        assert granted == {
            "TOP_OF_BOOK",
            "DEPTH_ANALYTICS",
            "EVENT_INTENSITY",
            "CANCELLATION_INTENSITY",
            "SELF_EXCITATION",
            "QUEUE_POSITION",
        }

    async def test_every_verdict_carries_a_reason_and_its_evidence(
        self, client, auth_header, instrument_id
    ):
        body = await import_dataset(
            client, auth_header, instrument_id, event_data=None, name="snapshots only"
        )
        response = await client.get(
            f"/microstructure/datasets/{body['results']['dataset_id']}/capabilities",
            headers={"Authorization": auth_header},
        )
        for item in response.json():
            assert item["message"]
            assert item["evidence"]
            assert (item["reason"] is None) == item["available"]

    async def test_a_snapshot_only_dataset_refuses_an_intensity_model(
        self, client, auth_header, instrument_id
    ):
        body = await import_dataset(
            client, auth_header, instrument_id, event_data=None, name="snapshots only"
        )
        dataset_id = body["results"]["dataset_id"]
        response = await client.post(
            f"/microstructure/datasets/{dataset_id}/intensity",
            headers={"Authorization": auth_header},
            json={},
        )
        assert response.status_code == 422
        problem = response.json()
        assert problem["code"] == "MICROSTRUCTURE_CAPABILITY_REFUSED"
        assert problem["reason"] == "NO_EVENTS"
        assert problem["capability"] == "EVENT_INTENSITY"
        assert "not the messages that caused them" in problem["detail"]

    async def test_an_event_only_dataset_refuses_book_analytics_and_a_queue(
        self, client, auth_header, instrument_id
    ):
        body = await import_dataset(
            client, auth_header, instrument_id, snapshot_data=None, name="events only"
        )
        dataset_id = body["results"]["dataset_id"]
        for path, payload, reason in (
            ("analyze", {}, "NO_SNAPSHOTS"),
            ("queue", {"side": "BID"}, "NO_SNAPSHOTS"),
        ):
            response = await client.post(
                f"/microstructure/datasets/{dataset_id}/{path}",
                headers={"Authorization": auth_header},
                json=payload,
            )
            assert response.status_code == 422, response.text
            assert response.json()["reason"] == reason

    async def test_a_tape_without_sequencing_refuses_a_queue_model(
        self, client, auth_header, instrument_id
    ):
        """A tape with a hole in it describes a different book, and nothing in
        a queue number would say so."""
        body = await import_dataset(
            client,
            auth_header,
            instrument_id,
            event_data=strip_sequence_numbers(EVENT_CSV),
            name="no sequencing",
        )
        dataset_id = body["results"]["dataset_id"]
        response = await client.post(
            f"/microstructure/datasets/{dataset_id}/queue",
            headers={"Authorization": auth_header},
            json={"side": "BID", "horizon_seconds": 60},
        )
        assert response.status_code == 422
        assert response.json()["reason"] == "NO_SEQUENCE_NUMBERS"

        # But the arrival rate does not need sequencing, and is still granted.
        intensity = await client.post(
            f"/microstructure/datasets/{dataset_id}/intensity",
            headers={"Authorization": auth_header},
            json={},
        )
        assert intensity.status_code == 202

    async def test_a_coarse_clock_refuses_a_self_exciting_model(
        self, client, auth_header, instrument_id
    ):
        """Fitting one to a second-resolution tape would estimate the recording
        resolution and report it as market behaviour."""
        body = await import_dataset(
            client,
            auth_header,
            instrument_id,
            event_data=coarsen_event_clock(EVENT_CSV),
            name="coarse clock",
        )
        dataset_id = body["results"]["dataset_id"]
        capabilities = await client.get(
            f"/microstructure/datasets/{dataset_id}/capabilities",
            headers={"Authorization": auth_header},
        )
        by_name = {item["capability"]: item for item in capabilities.json()}
        assert by_name["EVENT_INTENSITY"]["available"] is True
        excitation = by_name["SELF_EXCITATION"]
        assert excitation["available"] is False
        assert excitation["reason"] == "TIMESTAMP_RESOLUTION_TOO_COARSE"

        accepted = await client.post(
            f"/microstructure/datasets/{dataset_id}/intensity",
            headers={"Authorization": auth_header},
            json={},
        )
        result = await run_job(client, auth_header, accepted.json()["job_id"])
        assert result["results"]["adopted_model"] == "POISSON"

    async def test_there_is_no_parameter_that_overrides_a_refusal(
        self, client, auth_header, instrument_id
    ):
        """The gate is a property of the data, so nothing on the request can
        turn it off. Checked against the published schema, not by guessing."""
        schema = await client.get("http://testserver/openapi.json")
        assert schema.status_code == 200
        paths = schema.json()["paths"]
        microstructure = {path: spec for path, spec in paths.items() if "/microstructure/" in path}
        assert microstructure
        text = str(microstructure).lower()
        for escape_hatch in ("force", "override", "skip_gate", "ignore_availability"):
            assert escape_hatch not in text, escape_hatch


class TestBookAnalytics:
    @pytest.fixture
    async def report(self, client, auth_header, dataset):
        accepted = await client.post(
            f"/microstructure/datasets/{dataset}/analyze",
            headers={"Authorization": auth_header},
            json={"levels": 5, "weighted_decay": 0.5, "trade_sizes": [500.0, 1e9]},
        )
        assert accepted.status_code == 202, accepted.text
        body = await run_job(client, auth_header, accepted.json()["job_id"])
        response = await client.get(
            f"/microstructure/reports/{body['results']['report_id']}",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def test_every_measure_reports_what_it_was_computed_over(self, report):
        analysed = report["results"]["snapshots_analysed"]
        assert analysed == 361
        for measure in report["results"]["measures"]:
            assert measure["observations"] + measure["missing"] == analysed

    async def test_the_measures_are_the_ones_the_phase_promises(self, report):
        names = {measure["measure"] for measure in report["results"]["measures"]}
        assert {
            "spread",
            "relative_spread",
            "microprice",
            "imbalance",
            "weighted_imbalance",
            "bid_slope_value",
            "ask_slope_value",
            "bid_concentration",
            "ask_concentration",
        } <= names

    async def test_a_size_the_book_cannot_absorb_is_counted_not_extrapolated(self, report):
        costs = {item["quantity"]: item for item in report["results"]["trade_costs"]}
        assert costs[500.0]["snapshots_that_could_absorb_it"] == 361
        impossible = costs[1e9]
        assert impossible["snapshots_that_could_absorb_it"] == 0
        assert impossible["snapshots_that_could_not"] == 361
        assert impossible["median_slippage_bps"] is None

    async def test_the_result_carries_its_provenance(self, report):
        provenance = report["provenance"]
        assert provenance["model_versions"]["book_analytics"]
        assert provenance["model_versions"]["availability"]
        assert provenance["parameters"]["levels"] == 5
        assert provenance["parameters"]["weighted_decay"] == 0.5
        # "Which file was this?" is the first question a reproduction asks.
        assert len(provenance["dataset_versions"]["dataset"]) == 64

    async def test_the_series_spans_the_window_rather_than_its_opening(self, report):
        series = report["results"]["series"]
        assert series
        assert series[0]["timestamp"] == report["results"]["window"]["start"]
        assert series[-1]["timestamp"] == report["results"]["window"]["end"]


class TestIntensity:
    @pytest.fixture
    async def fitted(self, client, auth_header, dataset):
        accepted = await client.post(
            f"/microstructure/datasets/{dataset}/intensity",
            headers={"Authorization": auth_header},
            json={"train_fraction": 0.7},
        )
        assert accepted.status_code == 202, accepted.text
        return await run_job(client, auth_header, accepted.json()["job_id"])

    async def test_both_models_are_reported_with_the_verdict_between_them(self, fitted):
        results = fitted["results"]
        assert results["poisson"]["train"]["parameters"]["model"] == "POISSON"
        assert results["hawkes"]["train"]["parameters"]["model"] == "HAWKES_EXPONENTIAL"
        assert isinstance(results["hawkes_is_adopted"], bool)
        assert results["reason"]

    async def test_a_self_exciting_tape_adopts_the_self_exciting_model(self, fitted):
        """The fixture tape is generated from a Hawkes process, so the model
        that describes it should win on events it was not fitted on."""
        results = fitted["results"]
        assert results["hawkes_is_adopted"] is True
        assert results["adopted_model"] == "HAWKES_EXPONENTIAL"
        assert (
            results["predictive_test"]["statistic"] > results["predictive_test"]["critical_value"]
        )

    async def test_the_result_carries_the_digest_of_the_data_it_was_fitted_to(self, fitted):
        assert len(fitted["provenance"]["dataset_versions"]["dataset"]) == 64

    async def test_the_verdict_is_a_held_out_test_not_an_in_sample_fit(self, fitted):
        results = fitted["results"]
        assert results["predictive_test"]["test"].startswith("one-sided Diebold-Mariano")
        assert results["predictive_test"]["variance_estimator"].startswith("Newey-West")
        assert results["held_out_events"] > 0
        assert "not fitted on" in results["reason"]

    async def test_the_branching_ratio_is_below_one(self, fitted):
        ratio = fitted["results"]["hawkes"]["train"]["parameters"]["branching_ratio"]
        assert 0.0 < ratio < 1.0

    async def test_it_is_reproducible(self, client, auth_header, dataset):
        async def run() -> dict:
            accepted = await client.post(
                f"/microstructure/datasets/{dataset}/intensity",
                headers={"Authorization": auth_header},
                json={},
            )
            body = await run_job(client, auth_header, accepted.json()["job_id"])
            return body["results"]["hawkes"]["train"]["parameters"]

        assert await run() == await run()

    async def test_a_cancellation_scope_needs_labelled_cancellations(
        self, client, auth_header, dataset
    ):
        accepted = await client.post(
            f"/microstructure/datasets/{dataset}/intensity",
            headers={"Authorization": auth_header},
            json={"event_types": ["CANCEL"], "side": "BID"},
        )
        assert accepted.status_code == 202, accepted.text
        body = await run_job(client, auth_header, accepted.json()["job_id"])
        assert body["results"]["parameters"]["event_types"] == ["CANCEL"]
        assert body["results"]["parameters"]["side"] == "BID"
        assert body["results"]["events_selected"] > 0

    async def test_the_stored_row_cannot_claim_a_win_it_did_not_get(
        self, client, auth_header, dataset, fitted, db_session
    ):
        """The gate is in the schema as well as in the code.

        A row that says it adopted the self-exciting model without the held-out
        statistic to back it is not storable, so a future refactor, a bulk
        insert or a hand-written UPDATE cannot put one there.
        """
        import uuid as _uuid
        from datetime import UTC, datetime, timedelta

        from sqlalchemy.exc import IntegrityError

        from domains.microstructure.orm import IntensityModelORM

        me = await client.get("/auth/me", headers={"Authorization": auth_header})
        assert me.status_code == 200, me.text
        listed = await client.get(
            f"/microstructure/intensity?dataset_id={dataset}",
            headers={"Authorization": auth_header},
        )
        existing = listed.json()
        assert existing, "the fitted fixture should have stored a model"

        start = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)

        def fabricated(**overrides) -> IntensityModelORM:
            values = {
                "user_id": _uuid.UUID(me.json()["id"]),
                "dataset_id": _uuid.UUID(dataset),
                "instrument_id": _uuid.UUID(existing[0]["instrument_id"]),
                "scope": "ALL/BOTH/ANY",
                "event_types": ["ALL"],
                "events_selected": 100,
                "window_start": start,
                "window_end": start + timedelta(seconds=1800),
                "split_timestamp": start + timedelta(seconds=1200),
                "train_fraction": 0.7,
                "poisson_rate": 1.0,
                "poisson_train_log_likelihood": -1.0,
                "poisson_held_out_log_likelihood": -1.0,
                "hawkes_converged": True,
                "hawkes_branching_ratio": 0.5,
                "held_out_events": 50,
                "test_statistic": 5.0,
                "critical_value": 1.645,
                "hawkes_is_adopted": True,
                "adopted_model": "HAWKES_EXPONENTIAL",
                "adopted_rate": 1.0,
                "verdict_reason": "fabricated",
            }
            values.update(overrides)
            return IntensityModelORM(**values)

        # A statistic below the critical value cannot claim the model.
        db_session.add(fabricated(test_statistic=0.1))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

        # Neither can a fit that never converged.
        db_session.add(fabricated(hawkes_converged=False))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

        # And the adopted-model name cannot drift from the verdict flag.
        db_session.add(fabricated(adopted_model="POISSON"))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestQueueOutlook:
    @pytest.fixture
    async def outlook(self, client, auth_header, dataset):
        response = await client.post(
            f"/microstructure/datasets/{dataset}/queue",
            headers={"Authorization": auth_header},
            json={"side": "BID", "horizon_seconds": 300},
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def test_the_answer_is_a_bracket_and_there_is_no_single_number(self, outlook):
        """A probability exists only inside a labelled end of the bracket.

        The top level of the result carries the range and nothing that could be
        read as *the* answer, so a client rendering the obvious field renders
        the bracket.
        """
        results = outlook["results"]
        low, high = results["estimated_fill_probability_range"]
        assert 0.0 <= low <= high <= 1.0
        assert "estimated_fill_probability" not in results
        assert "fill_probability" not in results
        assert "expected_wait_seconds" not in results
        assert set(results["optimistic"]) & {"estimated_fill_probability"}
        assert set(results["pessimistic"]) & {"estimated_fill_probability"}

    async def test_the_two_ends_are_the_two_cancellation_assumptions(self, outlook):
        results = outlook["results"]
        assert results["optimistic"]["priority_assumption"] == "CANCELS_AHEAD"
        assert results["pessimistic"]["priority_assumption"] == "CANCELS_BEHIND"
        assert (
            results["optimistic"]["estimated_wait_seconds"]
            <= results["pessimistic"]["estimated_wait_seconds"]
        )

    async def test_every_assumption_travels_with_the_estimate(self, outlook):
        assumptions = " ".join(outlook["results"]["assumptions"]).lower()
        for phrase in (
            "first-in-first-out",
            "hidden and iceberg",
            "poisson counting process",
            "not a claim about where an exchange",
        ):
            assert phrase in assumptions

    async def test_it_is_warned_as_a_bracket_rather_than_presented_as_a_number(self, outlook):
        codes = {warning["code"] for warning in outlook["warnings"]}
        assert "QUEUE_ESTIMATE_IS_A_BRACKET" in codes

    async def test_a_price_the_book_never_showed_is_refused(self, client, auth_header, dataset):
        response = await client.post(
            f"/microstructure/datasets/{dataset}/queue",
            headers={"Authorization": auth_header},
            json={"side": "BID", "horizon_seconds": 60, "price": "1.00"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "FAILED"
        assert body["results"] is None
        assert "cannot be queued behind size the book was not showing" in str(body["warnings"])

    async def test_the_stored_estimate_is_listed_with_both_ends(
        self, client, auth_header, dataset, outlook
    ):
        response = await client.get(
            f"/microstructure/queue-estimates?dataset_id={dataset}",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        assert rows
        row = rows[0]
        assert row["optimistic_fill_probability"] >= row["pessimistic_fill_probability"]
        assert row["assumptions"]


class TestOwnership:
    async def test_another_users_dataset_is_a_404_never_a_403(self, client, auth_header, dataset):
        _other, other_header = await register_and_login(client)
        for path in (
            f"/microstructure/datasets/{dataset}",
            f"/microstructure/datasets/{dataset}/capabilities",
            f"/microstructure/datasets/{dataset}/rejections",
        ):
            response = await client.get(path, headers={"Authorization": other_header})
            assert response.status_code == 404, path

        for path, payload in (
            (f"/microstructure/datasets/{dataset}/analyze", {}),
            (f"/microstructure/datasets/{dataset}/intensity", {}),
            (f"/microstructure/datasets/{dataset}/queue", {"side": "BID"}),
        ):
            response = await client.post(
                path, headers={"Authorization": other_header}, json=payload
            )
            assert response.status_code == 404, path

    async def test_a_dataset_list_shows_only_your_own(self, client, auth_header, dataset):
        _other, other_header = await register_and_login(client)
        response = await client.get(
            "/microstructure/datasets", headers={"Authorization": other_header}
        )
        assert response.json() == []


class TestLanguage:
    async def test_no_microstructure_response_advises_or_promises(
        self, client, auth_header, dataset
    ):
        analyse = await client.post(
            f"/microstructure/datasets/{dataset}/analyze",
            headers={"Authorization": auth_header},
            json={},
        )
        report = await run_job(client, auth_header, analyse.json()["job_id"])
        queue = await client.post(
            f"/microstructure/datasets/{dataset}/queue",
            headers={"Authorization": auth_header},
            json={"side": "ASK", "horizon_seconds": 120},
        )
        intensity = await client.post(
            f"/microstructure/datasets/{dataset}/intensity",
            headers={"Authorization": auth_header},
            json={},
        )
        fitted = await run_job(client, auth_header, intensity.json()["job_id"])

        payloads = [
            str(report),
            queue.text,
            str(fitted),
            (
                await client.get(
                    f"/microstructure/datasets/{dataset}",
                    headers={"Authorization": auth_header},
                )
            ).text,
        ]
        forbidden = (
            "fair value",
            "underpriced",
            "overpriced",
            "arbitrage opportunity",
            "will fill",
            "guaranteed fill",
            "you should",
            "buy signal",
            "sell signal",
            "recommendation",
            "optimal placement",
        )
        for payload in payloads:
            lowered = payload.lower()
            for phrase in forbidden:
                assert phrase not in lowered, phrase

    async def test_a_queue_position_never_claims_to_be_the_exchange_queue(
        self, client, auth_header, dataset
    ):
        response = await client.post(
            f"/microstructure/datasets/{dataset}/queue",
            headers={"Authorization": auth_header},
            json={"side": "BID", "horizon_seconds": 60},
        )
        interpretation = response.json()["results"]["interpretation"].lower()
        assert "not a claim about where any exchange has placed an order" in interpretation
        assert "public data does not carry queue priority" in interpretation
