from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from api.dependencies.core import CacheDep, ObjectStoreDep, SessionDep, SettingsDep

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict:
    """Liveness. Touches no dependency, so it stays up while they are down."""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(session: SessionDep, cache: CacheDep, store: ObjectStoreDep) -> dict:
    """Readiness. Reports *which* dependency failed, not just that one did."""
    checks: dict[str, bool] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False
    try:
        await cache.set("qip:health", "1", ttl_seconds=5)
        checks["cache"] = await cache.get("qip:health") == "1"
    except Exception:
        checks["cache"] = False
    try:
        checks["object_store"] = await store.health()
    except Exception:
        checks["object_store"] = False

    return {"status": "ok" if all(checks.values()) else "degraded", "checks": checks}


@router.get("/meta/version")
async def version(settings: SettingsDep) -> dict:
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "code_commit": settings.code_commit,
        "environment": str(settings.env),
    }
