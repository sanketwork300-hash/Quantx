"""S3-compatible object store (AWS S3, MinIO, R2).

``boto3`` is an optional dependency; the import is deferred so the local backend
works without it installed.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator

from infrastructure.storage.base import ObjectStore, StoredObject


class S3ObjectStore(ObjectStore):
    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        import boto3  # noqa: PLC0415 - optional dependency, imported on use

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    async def _run(self, fn, *args, **kwargs):
        # boto3 is synchronous; keep it off the event loop.
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> StoredObject:
        await self._run(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return StoredObject(
            key=key,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
        )

    async def get(self, key: str) -> bytes:
        response = await self._run(self._client.get_object, Bucket=self._bucket, Key=key)
        return response["Body"].read()

    async def get_range(self, key: str, length: int) -> bytes:
        response = await self._run(
            self._client.get_object,
            Bucket=self._bucket,
            Key=key,
            Range=f"bytes=0-{max(length - 1, 0)}",
        )
        return response["Body"].read()

    async def exists(self, key: str) -> bool:
        try:
            await self._run(self._client.head_object, Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    async def delete(self, key: str) -> None:
        await self._run(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def iter_keys(self, prefix: str) -> AsyncIterator[str]:
        token: str | None = None
        while True:
            kwargs = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = await self._run(self._client.list_objects_v2, **kwargs)
            for item in page.get("Contents", []):
                yield item["Key"]
            if not page.get("IsTruncated"):
                return
            token = page.get("NextContinuationToken")

    async def health(self) -> bool:
        try:
            await self._run(self._client.head_bucket, Bucket=self._bucket)
            return True
        except Exception:
            return False
