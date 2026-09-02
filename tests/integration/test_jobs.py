"""Asynchronous job lifecycle through the API."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import register_and_login

AS_OF = "2026-09-24T09:20:00Z"
MAPPING = {
    "strike": "STRIKE_PRICE",
    "option_type": "CE_PE",
    "expiry": "EXPIRY_DT",
    "bid_price": "BID",
    "ask_price": "ASK",
}


async def submit(client, header, data: bytes):
    upload = await client.post(
        "/uploads", headers={"Authorization": header}, files={"file": ("c.csv", data, "text/csv")}
    )
    response = await client.post(
        f"/uploads/{upload.json()['id']}/ingest",
        headers={"Authorization": header},
        json={
            "underlying": {"symbol": "NIFTY", "exchange": "SYNTH"},
            "as_of_timestamp": AS_OF,
            "column_mapping": MAPPING,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


class TestJobLifecycle:
    async def test_ingestion_returns_a_job_rather_than_blocking(
        self, client, auth_header, clean_chain_csv
    ):
        job_id = await submit(client, auth_header, clean_chain_csv)
        assert uuid.UUID(job_id)

    async def test_job_reaches_a_terminal_state_with_a_result(
        self, client, auth_header, clean_chain_csv
    ):
        job_id = await submit(client, auth_header, clean_chain_csv)
        job = (await client.get(f"/jobs/{job_id}", headers={"Authorization": auth_header})).json()
        assert job["status"] == "COMPLETED"
        assert job["progress"] == 1.0
        assert job["started_at"] is not None
        assert job["completed_at"] is not None
        assert job["error"] is None

        result = await client.get(f"/jobs/{job_id}/result", headers={"Authorization": auth_header})
        assert result.status_code == 200
        assert result.json()["result"]["results"]["counts"]["kept"] > 0

    async def test_jobs_are_listed_for_their_owner(self, client, auth_header, clean_chain_csv):
        await submit(client, auth_header, clean_chain_csv)
        response = await client.get("/jobs", headers={"Authorization": auth_header})
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["job_type"] == "INGEST_OPTION_CHAIN"

    async def test_a_completed_job_cannot_be_cancelled(self, client, auth_header, clean_chain_csv):
        job_id = await submit(client, auth_header, clean_chain_csv)
        response = await client.post(
            f"/jobs/{job_id}/cancel", headers={"Authorization": auth_header}
        )
        assert response.status_code == 409
        assert response.json()["code"] == "JOB_ALREADY_TERMINAL"

    async def test_unknown_job_is_not_found(self, client, auth_header):
        response = await client.get(f"/jobs/{uuid.uuid4()}", headers={"Authorization": auth_header})
        assert response.status_code == 404

    async def test_jobs_require_authentication(self, client):
        assert (await client.get("/jobs")).status_code == 401


class TestJobFailure:
    async def test_a_failed_job_records_a_structured_error(
        self, client, auth_header, clean_chain_csv, monkeypatch
    ):
        """A handler exception must leave a FAILED job with an error payload,
        not a lost job stuck in RUNNING."""
        import domains.jobs.handlers as handlers

        async def exploding_handler(_session, _job):
            raise RuntimeError("synthetic handler failure")

        monkeypatch.setitem(
            handlers._REGISTRY,
            __import__("domains.jobs.models", fromlist=["JobType"]).JobType.INGEST_OPTION_CHAIN,
            exploding_handler,
        )

        upload = await client.post(
            "/uploads",
            headers={"Authorization": auth_header},
            files={"file": ("c.csv", clean_chain_csv, "text/csv")},
        )
        with pytest.raises(RuntimeError, match="synthetic handler failure"):
            await client.post(
                f"/uploads/{upload.json()['id']}/ingest",
                headers={"Authorization": auth_header},
                json={
                    "underlying": {"symbol": "NIFTY", "exchange": "SYNTH"},
                    "as_of_timestamp": AS_OF,
                    "column_mapping": MAPPING,
                },
            )

        listing = await client.get("/jobs", headers={"Authorization": auth_header})
        job = listing.json()["items"][0]
        assert job["status"] == "FAILED"
        assert job["error"]["type"] == "RuntimeError"
        assert "synthetic handler failure" in job["error"]["message"]


class TestJobIsolation:
    async def test_job_listings_are_scoped_to_the_owner(self, client, clean_chain_csv):
        _alice, alice_header = await register_and_login(client, "j-alice@example.com")
        _bob, bob_header = await register_and_login(client, "j-bob@example.com")

        await submit(client, alice_header, clean_chain_csv)
        response = await client.get("/jobs", headers={"Authorization": bob_header})
        assert response.json()["items"] == []


class TestSubmissionContract:
    async def test_the_202_reports_the_state_at_submission(
        self, client, auth_header, clean_chain_csv
    ):
        """202 means accepted, not finished. Job state is authoritative only at
        GET /jobs/{id}."""
        upload = await client.post(
            "/uploads",
            headers={"Authorization": auth_header},
            files={"file": ("c.csv", clean_chain_csv, "text/csv")},
        )
        response = await client.post(
            f"/uploads/{upload.json()['id']}/ingest",
            headers={"Authorization": auth_header},
            json={
                "underlying": {"symbol": "NIFTY", "exchange": "SYNTH"},
                "as_of_timestamp": AS_OF,
                "column_mapping": MAPPING,
            },
        )
        assert response.status_code == 202
        assert response.json()["status"] == "QUEUED"

        job_id = response.json()["job_id"]
        authoritative = await client.get(f"/jobs/{job_id}", headers={"Authorization": auth_header})
        assert authoritative.json()["status"] == "COMPLETED"
