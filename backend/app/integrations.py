"""Redis 与 S3 兼容对象存储的运行时连接。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aioboto3
from botocore.exceptions import ClientError
from redis.asyncio import Redis

from app.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    """创建连接本机 Compose Redis 的异步客户端。"""
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password.get_secret_value(),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
        health_check_interval=30,
    )


class S3ObjectStore:
    """封装 SeaweedFS S3 API，并限定访问配置好的 Demo 桶。"""

    def __init__(self, settings: Settings) -> None:
        """保存连接配置并初始化延迟创建的 S3 客户端上下文。"""
        self._settings = settings
        self._session = aioboto3.Session()
        self._client_context: Any = None
        self._client: Any = None

    async def __aenter__(self) -> "S3ObjectStore":
        """打开异步 S3 客户端并返回对象存储适配器。"""
        self._client_context = self._session.client(
            "s3",
            endpoint_url=str(self._settings.s3_endpoint_url),
            region_name=self._settings.s3_region,
            aws_access_key_id=self._settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=self._settings.s3_secret_key.get_secret_value(),
        )
        self._client = await self._client_context.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """关闭客户端并清除连接状态。"""
        if self._client_context is not None:
            await self._client_context.__aexit__(exc_type, exc, traceback)
        self._client = None
        self._client_context = None

    async def ensure_bucket(self) -> None:
        """验证配置的桶；仅当桶不存在时创建。"""
        client = self._require_client()
        try:
            await client.head_bucket(Bucket=self._settings.s3_bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            await client.create_bucket(Bucket=self._settings.s3_bucket)

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        """写入对象；调用方负责使用不可猜测且不含敏感信息的对象键。"""
        await self._require_client().put_object(
            Bucket=self._settings.s3_bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )

    async def get(self, key: str) -> tuple[bytes, str]:
        """读取对象正文和其 MIME 类型。"""
        response = await self._require_client().get_object(Bucket=self._settings.s3_bucket, Key=key)
        async with response["Body"] as stream:
            body = await stream.read()
        return body, response.get("ContentType", "application/octet-stream")

    async def delete(self, key: str) -> None:
        """删除指定对象。"""
        await self._require_client().delete_object(Bucket=self._settings.s3_bucket, Key=key)

    def _require_client(self) -> Any:
        """返回已连接的 S3 客户端，否则报告生命周期错误。"""
        if self._client is None:
            raise RuntimeError("S3 object store is not connected")
        return self._client


@asynccontextmanager
async def connected_object_store(settings: Settings) -> AsyncIterator[S3ObjectStore]:
    """连接对象存储、确保桶可用，并在退出时释放客户端。"""
    async with S3ObjectStore(settings) as object_store:
        await object_store.ensure_bucket()
        yield object_store
