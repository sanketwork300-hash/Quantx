from __future__ import annotations

from infrastructure.settings import ObjectStoreBackend, Settings, get_settings
from infrastructure.storage.base import ObjectStore

_store: ObjectStore | None = None


def get_object_store(settings: Settings | None = None) -> ObjectStore:
    global _store
    if _store is None:
        settings = settings or get_settings()
        if settings.object_store_backend is ObjectStoreBackend.S3:
            from infrastructure.storage.s3 import S3ObjectStore

            _store = S3ObjectStore(
                bucket=settings.s3_bucket,
                endpoint_url=settings.s3_endpoint_url,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                region=settings.s3_region,
            )
        else:
            from infrastructure.storage.local import LocalObjectStore

            _store = LocalObjectStore(settings.object_store_root)
    return _store


def override_object_store(store: ObjectStore | None) -> None:
    global _store
    _store = store
