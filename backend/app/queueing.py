"""用于发布企业微信入站任务通知的持久化 RabbitMQ 发布器。"""

import asyncio
import json

import aio_pika
from aio_pika.abc import AbstractRobustChannel, AbstractRobustConnection

from app.config import get_settings

QUEUE_NAME = "sagtrag.wecom.inbound"
INGESTION_QUEUE_NAME = "sagtrag.documents.ingestion"


class InboundPublisher:
    """发布持久化任务引用；消费者处理时重新读取当前授权信息。"""

    def __init__(self) -> None:
        """创建尚未连接的 RabbitMQ 入站发布器。"""
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractRobustChannel | None = None

    async def connect(self) -> None:
        """建立可自动恢复的连接，并声明持久化入站队列。"""
        url = get_settings().rabbitmq_url.get_secret_value()
        self._connection = await aio_pika.connect_robust(url, timeout=3)
        self._channel = await self._connection.channel(publisher_confirms=True)
        await self._channel.declare_queue(QUEUE_NAME, durable=True)

    async def publish(self, *, tenant_id: str, inbound_id: str) -> None:
        """仅发布数据库标识，不在队列消息中携带权限快照或消息正文。"""
        if self._channel is None or self._channel.is_closed:
            await self.connect()
        body = json.dumps({"tenant_id": tenant_id, "inbound_id": inbound_id}, separators=(",", ":")).encode()
        await asyncio.wait_for(
            self._channel.default_exchange.publish(
                aio_pika.Message(
                    body=body,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    message_id=inbound_id,
                ),
                routing_key=QUEUE_NAME,
                mandatory=True,
            ),
            timeout=get_settings().rabbitmq_publish_timeout_seconds,
        )

    async def close(self) -> None:
        """应用关闭时关闭共享连接。"""
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()


class IngestionPublisher:
    """发布数据库摄取任务的唤醒通知；任务状态仍以 PostgreSQL 为准。"""

    def __init__(self) -> None:
        """创建尚未连接的文档摄取通知发布器。"""
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractRobustChannel | None = None

    async def connect(self) -> None:
        """连接 RabbitMQ 并声明持久摄取通知队列。"""
        self._connection = await aio_pika.connect_robust(get_settings().rabbitmq_url.get_secret_value(), timeout=3)
        self._channel = await self._connection.channel(publisher_confirms=True)
        await self._channel.declare_queue(INGESTION_QUEUE_NAME, durable=True)

    async def publish(self, *, tenant_id: str, job_id: str) -> None:
        """发布租户和任务标识，不携带文件正文。"""
        if self._channel is None or self._channel.is_closed:
            await self.connect()
        body = json.dumps({"tenant_id": tenant_id, "job_id": job_id}, separators=(",", ":")).encode()
        await asyncio.wait_for(
            self._channel.default_exchange.publish(
                aio_pika.Message(body=body, content_type="application/json", delivery_mode=aio_pika.DeliveryMode.PERSISTENT, message_id=job_id),
                routing_key=INGESTION_QUEUE_NAME,
                mandatory=True,
            ),
            timeout=get_settings().rabbitmq_publish_timeout_seconds,
        )

    async def close(self) -> None:
        """关闭 RabbitMQ 连接。"""
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()

