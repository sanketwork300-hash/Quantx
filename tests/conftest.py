from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
DATA_DIR = TESTS_DIR / "data"


def _configure_environment(tmp_root: Path) -> None:
    """Point every piece of configuration at throwaway resources.

    Set before ``Settings`` is first constructed anywhere, so no test can
    accidentally reach a real database, cache or bucket.
    """
    os.environ.update(
        {
            "QIP_ENV": "test",
            "QIP_SECRET_KEY": "test-secret-key-not-for-production-use-only",
            "QIP_JOB_EXECUTION_MODE": "eager",
            "QIP_OBJECT_STORE_BACKEND": "local",
            "QIP_OBJECT_STORE_ROOT": str(tmp_root / "objectstore"),
            "QIP_LOG_FORMAT": "console",
            "QIP_LOG_LEVEL": "WARNING",
            "QIP_CODE_COMMIT": "test-commit",
        }
    )


@pytest.fixture(scope="session")
def session_tmp_root(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("qip")


@pytest.fixture
async def app_environment(tmp_path, session_tmp_root) -> AsyncIterator[dict]:
    """A fully isolated application environment: database, object store, cache."""
    _configure_environment(tmp_path)
    database_path = tmp_path / "qip.db"
    os.environ["QIP_DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path}"

    from infrastructure.cache.client import InMemoryCache, override_cache
    from infrastructure.database.base import Base
    from infrastructure.database.session import dispose_engine, override_sessionmaker
    from infrastructure.settings import get_settings, reset_settings_cache
    from infrastructure.storage.factory import override_object_store
    from infrastructure.storage.local import LocalObjectStore

    reset_settings_cache()
    await dispose_engine()
    override_sessionmaker(None)
    override_cache(InMemoryCache())
    override_object_store(LocalObjectStore(tmp_path / "objectstore"))

    settings = get_settings()

    # Importing the ORM modules is what registers them on Base.metadata.
    import domains.derivatives.orm  # noqa: F401
    import domains.execution.orm  # noqa: F401
    import domains.instruments.orm  # noqa: F401
    import domains.jobs.orm  # noqa: F401
    import domains.market_data.orm  # noqa: F401
    import domains.portfolio.orm  # noqa: F401
    import domains.reports.orm  # noqa: F401
    import domains.risk.orm  # noqa: F401
    import domains.scenarios.orm  # noqa: F401
    import domains.users.orm  # noqa: F401
    from infrastructure.database.session import get_engine

    engine = get_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield {"settings": settings, "tmp_path": tmp_path}

    await dispose_engine()
    override_sessionmaker(None)
    override_cache(None)
    override_object_store(None)
    reset_settings_cache()


@pytest.fixture
async def db_session(app_environment) -> AsyncIterator:
    from infrastructure.database.session import get_sessionmaker

    maker = get_sessionmaker()
    async with maker() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def client(app_environment) -> AsyncIterator:
    import httpx

    from apps.api.main import create_app

    app = create_app(app_environment["settings"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver/api/v1"
    ) as http_client:
        yield http_client


async def register_and_login(client, email: str | None = None) -> tuple[dict, str]:
    """Create a user and return ``(user, authorization_header_value)``."""
    email = email or f"user-{uuid.uuid4().hex[:12]}@example.com"
    password = "correct-horse-battery-staple"
    response = await client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    user = response.json()

    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return user, f"Bearer {response.json()['access_token']}"


@pytest.fixture
async def auth_header(client) -> str:
    _user, header = await register_and_login(client)
    return header


@pytest.fixture
def clean_chain_csv() -> bytes:
    return (DATA_DIR / "options_chain_clean.csv").read_bytes()


@pytest.fixture
def bad_chain_csv() -> bytes:
    return (DATA_DIR / "options_chain_bad_quotes.csv").read_bytes()
