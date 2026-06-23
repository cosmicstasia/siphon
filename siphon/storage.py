"""Shared DB/S3 storage helpers used by all scrapers.

S3 layout convention: every run uploads its screenshots under a prefix of
the form ``{scraper_name}_{run_id}/`` (e.g. ``snapchat_2026-06-18_10-30-00/``),
so each run's images live in their own "folder" in the bucket. Scrapers
build this prefix once per run and pass it into ``upload_bytes``/``upload_file``.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import aioboto3
import motor.motor_asyncio
from beanie import Document, init_beanie

_client: motor.motor_asyncio.AsyncIOMotorClient | None = None
_s3_session: aioboto3.Session | None = None
_s3_bucket: str = ""
_s3_kwargs: dict = {}


async def init_db(document_models: list[type[Document]]) -> None:
    global _client, _s3_session, _s3_bucket, _s3_kwargs
    url = os.environ["MONGO_URL"]
    _client = motor.motor_asyncio.AsyncIOMotorClient(url)
    db = _client.get_default_database()
    await init_beanie(database=db, document_models=document_models)

    _s3_bucket = os.environ["S3_BUCKET"]
    _s3_kwargs = {"region_name": os.environ.get("AWS_REGION", "us-east-1")}
    if endpoint := os.environ.get("S3_ENDPOINT_URL"):
        _s3_kwargs["endpoint_url"] = endpoint
    _s3_session = aioboto3.Session()


async def upload_bytes(data: bytes, prefix: str) -> str:
    image_id = str(uuid.uuid4())
    async with _s3_session.client("s3", **_s3_kwargs) as s3:
        await s3.put_object(
            Bucket=_s3_bucket,
            Key=f"{prefix}/{image_id}.png",
            Body=data,
            ContentType="image/png",
        )
    return image_id


async def upload_file(path: Path, prefix: str) -> str:
    with open(path, "rb") as f:
        return await upload_bytes(f.read(), prefix)
