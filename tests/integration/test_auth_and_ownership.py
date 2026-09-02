"""Authentication, and the ownership rule that a UUID confers nothing."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import register_and_login


class TestRegistration:
    async def test_register_and_login(self, client):
        response = await client.post(
            "/auth/register",
            json={"email": "Trader@Example.COM", "password": "correct-horse-battery"},
        )
        assert response.status_code == 201
        assert response.json()["email"] == "trader@example.com"

        response = await client.post(
            "/auth/login",
            json={"email": "trader@example.com", "password": "correct-horse-battery"},
        )
        assert response.status_code == 200
        assert response.json()["token_type"] == "bearer"

    async def test_duplicate_email_is_a_conflict(self, client):
        payload = {"email": "dup@example.com", "password": "correct-horse-battery"}
        assert (await client.post("/auth/register", json=payload)).status_code == 201
        response = await client.post("/auth/register", json=payload)
        assert response.status_code == 409
        assert response.json()["code"] == "EMAIL_ALREADY_REGISTERED"

    async def test_weak_passwords_are_refused(self, client):
        response = await client.post(
            "/auth/register", json={"email": "weak@example.com", "password": "short"}
        )
        assert response.status_code == 422


class TestLoginFailures:
    async def test_unknown_and_wrong_password_are_indistinguishable(self, client):
        await client.post(
            "/auth/register",
            json={"email": "known@example.com", "password": "correct-horse-battery"},
        )
        wrong = await client.post(
            "/auth/login", json={"email": "known@example.com", "password": "nope-nope-nope"}
        )
        unknown = await client.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": "correct-horse-battery"},
        )
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json()["detail"] == unknown.json()["detail"]
        assert wrong.json()["code"] == unknown.json()["code"]

    async def test_failed_logins_are_audited(self, client, db_session):
        from sqlalchemy import select

        from domains.users.orm import AuditLogORM

        await client.post(
            "/auth/login",
            json={"email": "ghost@example.com", "password": "correct-horse-battery"},
        )
        rows = (await db_session.execute(select(AuditLogORM))).scalars().all()
        assert any(row.action == "LOGIN_FAILED" for row in rows)


class TestTokenHandling:
    async def test_me_requires_a_token(self, client):
        assert (await client.get("/auth/me")).status_code == 401

    async def test_me_returns_the_authenticated_user(self, client):
        user, header = await register_and_login(client)
        response = await client.get("/auth/me", headers={"Authorization": header})
        assert response.status_code == 200
        assert response.json()["id"] == user["id"]

    @pytest.mark.parametrize(
        "header", ["", "Bearer ", "Basic abc", "Bearer not-a-jwt", "token abc"]
    )
    async def test_malformed_authorization_headers_are_rejected(self, client, header):
        response = await client.get("/auth/me", headers={"Authorization": header})
        assert response.status_code == 401

    async def test_a_token_for_a_deleted_user_is_rejected(self, client, db_session):
        from sqlalchemy import delete

        from domains.users.orm import UserORM

        user, header = await register_and_login(client)
        await db_session.execute(delete(UserORM).where(UserORM.id == uuid.UUID(user["id"])))
        await db_session.commit()
        response = await client.get("/auth/me", headers={"Authorization": header})
        assert response.status_code == 401


class TestOwnership:
    async def test_another_users_upload_is_not_found_rather_than_forbidden(
        self, client, clean_chain_csv
    ):
        """403 would confirm the id exists, which makes ids enumerable."""
        _alice, alice_header = await register_and_login(client, "alice@example.com")
        _bob, bob_header = await register_and_login(client, "bob@example.com")

        response = await client.post(
            "/uploads",
            headers={"Authorization": alice_header},
            files={"file": ("chain.csv", clean_chain_csv, "text/csv")},
        )
        assert response.status_code == 201
        upload_id = response.json()["id"]

        for method, path in (
            ("get", f"/uploads/{upload_id}"),
            ("post", f"/uploads/{upload_id}/preview"),
        ):
            call = getattr(client, method)
            kwargs = {"headers": {"Authorization": bob_header}}
            if method == "post":
                kwargs["json"] = {}
            response = await call(path, **kwargs)
            assert response.status_code == 404, path
            assert response.json()["code"] == "RESOURCE_NOT_FOUND"

    async def test_upload_listings_are_scoped_to_the_owner(self, client, clean_chain_csv):
        _alice, alice_header = await register_and_login(client, "alice2@example.com")
        _bob, bob_header = await register_and_login(client, "bob2@example.com")

        await client.post(
            "/uploads",
            headers={"Authorization": alice_header},
            files={"file": ("chain.csv", clean_chain_csv, "text/csv")},
        )
        response = await client.get("/uploads", headers={"Authorization": bob_header})
        assert response.status_code == 200
        assert response.json() == []

    async def test_another_users_job_is_not_found(self, client, clean_chain_csv):
        _alice, alice_header = await register_and_login(client, "alice3@example.com")
        _bob, bob_header = await register_and_login(client, "bob3@example.com")

        upload = await client.post(
            "/uploads",
            headers={"Authorization": alice_header},
            files={"file": ("chain.csv", clean_chain_csv, "text/csv")},
        )
        ingest = await client.post(
            f"/uploads/{upload.json()['id']}/ingest",
            headers={"Authorization": alice_header},
            json={
                "underlying": {"symbol": "NIFTY", "exchange": "SYNTH"},
                "as_of_timestamp": "2026-09-24T09:20:00Z",
                "column_mapping": {
                    "strike": "STRIKE_PRICE",
                    "option_type": "CE_PE",
                    "expiry": "EXPIRY_DT",
                },
            },
        )
        assert ingest.status_code == 202
        job_id = ingest.json()["job_id"]

        response = await client.get(f"/jobs/{job_id}", headers={"Authorization": bob_header})
        assert response.status_code == 404
