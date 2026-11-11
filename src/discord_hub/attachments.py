"""Immutable attachment objects in R2; only metadata enters Postgres."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import boto3

from .config import Settings


class ArchiveAttachmentStore:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.archive_r2_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.archive_r2_endpoint
            or f"https://{settings.archive_r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.archive_r2_access_key_id,
            aws_secret_access_key=settings.archive_r2_secret_access_key,
            region_name="auto",
        )

    async def save(self, attachment: Any, *, message_id: int, data: bytes) -> dict[str, Any]:
        digest = hashlib.sha256(data).hexdigest()
        key = f"archive/messages/{message_id}/{attachment.id}-{digest}"
        metadata = {
            "id": str(attachment.id),
            "filename": attachment.filename,
            "size": len(data),
            "content_type": attachment.content_type,
            "sha256": digest,
            "object_key": key,
        }
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=attachment.content_type or "application/octet-stream",
            Metadata={"sha256": digest, "message-id": str(message_id)},
        )
        return metadata

    async def read(self, object_key: str) -> tuple[bytes, str | None]:
        def get():
            obj = self.client.get_object(Bucket=self.bucket, Key=object_key)
            return obj["Body"].read(), obj.get("ContentType")

        return await asyncio.to_thread(get)
