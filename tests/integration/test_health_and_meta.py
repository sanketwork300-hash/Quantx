from __future__ import annotations


class TestHealth:
    async def test_liveness_needs_no_dependencies(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_readiness_reports_each_dependency(self, client):
        response = await client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert set(body["checks"]) == {"database", "cache", "object_store"}
        assert body["status"] == "ok"

    async def test_version_metadata(self, client):
        response = await client.get("/meta/version")
        body = response.json()
        assert body["environment"] == "test"
        assert body["code_commit"] == "test-commit"


class TestErrorContract:
    async def test_errors_carry_a_correlation_id(self, client):
        response = await client.get("/auth/me")
        assert response.status_code == 401
        body = response.json()
        assert body["code"] == "UNAUTHORIZED"
        assert body["correlation_id"]
        assert body["type"].startswith("https://qip.dev/errors/")

    async def test_correlation_id_echoes_the_request_header(self, client):
        response = await client.get("/health", headers={"X-Correlation-Id": "abc-123-correlation"})
        assert response.headers["X-Correlation-Id"] == "abc-123-correlation"

    async def test_request_validation_errors_are_structured(self, client):
        response = await client.post("/auth/register", json={"email": "not-an-email"})
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "REQUEST_VALIDATION_FAILED"
        assert body["errors"]


class TestProductLanguage:
    async def test_the_api_description_makes_no_advisory_claim(self, client):
        """A contract guarantee: this platform gives analysis, not advice."""
        spec = (await client.get("http://testserver/openapi.json")).json()
        description = spec["info"]["description"].lower()
        for forbidden in ("fair value", "guaranteed", "recommend", "buy signal"):
            assert forbidden not in description or "does not" in description
        assert "reference value" in description
        assert "does not produce trade" in description
