"""数据库权威的企微问答 Worker：租约恢复、答案检查点、主动发送与保留策略。"""

import asyncio
import copy
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import aio_pika
from sqlalchemy import Text, cast, func, or_, select, update

from app.config import get_settings
from app.db import SessionFactory, set_tenant_rls_context
from app.integrations import create_redis_client
from app.models import AuditEvent, Chunk, Document, User, UserStatus, WeComInboundMessage
from app.permissions import load_principal
from app.queueing import QUEUE_NAME
from app.rag import PERMISSION_CHANGED, SERVICE_ERROR, RagAnswer, answer_question
from app.retrieval import visible_chunks
from app.wecom_delivery import DeliveryError, WeComDelivery
from app.voice import VoiceError, VOICE_MESSAGES, transcribe_voice

logger = logging.getLogger("sagtrag.rag")
MAX_ATTEMPTS = 3


def now() -> datetime:
    return datetime.now(timezone.utc)


def audit(session, message: WeComInboundMessage, action: str) -> None:
    """审计只保存状态和标识，不复制问题、答案、引用正文。"""
    session.add(AuditEvent(
        tenant_id=message.tenant_id, actor_id=message.user_id, action=action,
        resource_type="wecom_message", resource_id=str(message.id),
        details={"state": message.state, "send_state": message.send_state,
                 "error_code": message.error_code, "attempt_count": message.attempt_count,
                 "send_attempt_count": message.send_attempt_count},
    ))


async def claim_message(worker_id: str) -> UUID | None:
    """领取排队或过期任务；发送中断只能标记未知，避免重复回复。"""
    async with SessionFactory() as session, session.begin():
        await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
        message = await session.scalar(
            select(WeComInboundMessage).where(
                WeComInboundMessage.tenant_id == get_settings().we_com_tenant_id,
                or_(
                    (WeComInboundMessage.state.in_(("received", "queued")))
                    & (WeComInboundMessage.next_attempt_at <= func.now()),
                    (WeComInboundMessage.state == "processing")
                    & (WeComInboundMessage.lease_expires_at < func.now()),
                ),
            ).order_by(WeComInboundMessage.received_at).with_for_update(skip_locked=True).limit(1)
        )
        if message is None:
            return None
        if message.send_state == "sending":
            message.state, message.send_state = "failed", "unknown"
            message.error_code = "send_interrupted_unknown"
            message.completed_at = now()
            message.worker_id, message.lease_expires_at = None, None
            audit(session, message, "rag.send_unknown")
            return None
        if message.attempt_count >= MAX_ATTEMPTS and message.answer is None:
            message.answer = RagAnswer(SERVICE_ERROR, "service_error").payload()
        message.state = "processing"
        message.worker_id = worker_id
        message.attempt_count += 1
        message.lease_expires_at = now() + timedelta(minutes=3)
        return message.id


async def heartbeat(message_id: UUID, worker_id: str) -> None:
    """模型请求期间续租，更新只作用于当前领取者。"""
    while True:
        await asyncio.sleep(30)
        async with SessionFactory() as session, session.begin():
            await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
            await session.execute(update(WeComInboundMessage).where(
                WeComInboundMessage.id == message_id, WeComInboundMessage.worker_id == worker_id,
                WeComInboundMessage.state == "processing",
            ).values(lease_expires_at=now() + timedelta(minutes=3)))


async def current_user(session, message: WeComInboundMessage, *, lock: bool = False) -> User | None:
    """检查原发送者仍绑定到原本员工，禁止解绑后被另一个员工继承旧任务。"""
    query = select(User).where(
        User.id == message.user_id, User.tenant_id == message.tenant_id,
        User.wecom_userid == message.wecom_userid, User.status == UserStatus.ACTIVE,
        User.deleted_at.is_(None),
    )
    return await session.scalar((query.with_for_update() if lock else query).execution_options(populate_existing=True))


def cancel(session, message: WeComInboundMessage) -> None:
    message.state, message.send_state = "completed", "cancelled"
    message.error_code = "identity_revoked"
    message.answer = None
    message.diagnostics = {**(message.diagnostics or {}), "outcome": "identity_revoked", "cited_chunk_ids": []}
    message.worker_id, message.lease_expires_at, message.completed_at = None, None, now()
    audit(session, message, "rag.cancelled")


async def process_message(message_id: UUID, worker_id: str, delivery: WeComDelivery) -> None:
    """先保存生成结果，再独立发送；重试发送复用答案而不重复检索。"""
    async with SessionFactory() as session:
        await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
        message = await session.get(WeComInboundMessage, message_id)
        if message is None or message.worker_id != worker_id or message.state != "processing":
            return
        user = await current_user(session, message)
        if user is None:
            cancel(session, message)
            await session.commit()
            return
        if message.answer is not None and message.permission_version is None:
            # 达到处理重试上限后的固定错误回复没有检索内容，使用当前身份发送。
            message.permission_version = user.permission_version
        if message.answer is None:
            diagnostics = dict(message.diagnostics or {})
            voice_failure = None
            if message.msg_type == "voice" and not message.content:
                saved_voice = diagnostics.get("voice", {})
                if saved_voice.get("state") == "failed" and saved_voice.get("retryable") is False:
                    code = saved_voice["error_code"]
                    voice_failure = RagAnswer(VOICE_MESSAGES[code], code)
                    question = ""
                else:
                    try:
                        question = await transcribe_voice(delivery, message.media_id, diagnostics)
                    except VoiceError as exc:
                        diagnostics.setdefault("voice", {}).update(state="failed", error_code=exc.code, retryable=exc.retryable)
                        message.diagnostics = diagnostics
                        await session.commit()
                        if exc.retryable:
                            raise
                        voice_failure = RagAnswer(VOICE_MESSAGES[exc.code], exc.code)
                        question = ""
                await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
                # 转写后再次核对身份，并在 RAG 前保存检查点，重试不重新下载/转写。
                user = await current_user(session, message)
                if user is None:
                    cancel(session, message)
                    await session.commit()
                    return
                message.content = question
                message.media_id = None
                message.diagnostics = diagnostics
                await session.commit()
                await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
            # 转写检查点已提交；后续诊断使用独立对象，避免原地更新 JSON 被 ORM 漏写。
            diagnostics = copy.deepcopy(diagnostics)
            version = user.permission_version
            principal = await load_principal(session, user)
            if voice_failure is not None:
                answer = voice_failure
            else:
                async with asyncio.timeout(240):
                    answer = await answer_question(session, principal, message.content, diagnostics=diagnostics)
            await session.commit()
            await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
            message = await session.scalar(select(WeComInboundMessage).where(
                WeComInboundMessage.id == message_id,
                WeComInboundMessage.worker_id == worker_id,
                WeComInboundMessage.state == "processing",
            ).with_for_update())
            if message is None:
                return
            message.answer, message.permission_version = answer.payload(), version
            message.diagnostics = {**diagnostics, "outcome": answer.outcome}
            audit(session, message, "rag.generated")
        await session.commit()

    # 单独持久化发送检查点；身份与来源的最终复核集中在实际发送事务内。
    async with SessionFactory() as session, session.begin():
        await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
        message = await session.scalar(select(WeComInboundMessage).where(
            WeComInboundMessage.id == message_id, WeComInboundMessage.worker_id == worker_id,
            WeComInboundMessage.state == "processing",
        ).with_for_update())
        if message is None:
            return
        if message.send_attempt_count >= MAX_ATTEMPTS:
            message.state, message.send_state = "failed", "failed"
            message.error_code = "send_attempt_limit"
            message.worker_id, message.lease_expires_at, message.completed_at = None, None, now()
            audit(session, message, "rag.failed")
            return
        # 独立提交 sending 检查点，随后重新加锁；崩溃不会自动重发已可能送达的消息。
        message.send_state = "sending"
        message.send_attempt_count += 1
        await session.commit()

    await deliver_answer(message_id, worker_id, delivery)


async def deliver_answer(message_id: UUID, worker_id: str, delivery: WeComDelivery) -> None:
    """发送期间锁定员工和引用文档，使解绑、密级变更和删文档串行生效。"""
    async with SessionFactory() as session, session.begin():
        await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
        message = await session.scalar(select(WeComInboundMessage).where(
            WeComInboundMessage.id == message_id, WeComInboundMessage.worker_id == worker_id,
            WeComInboundMessage.state == "processing", WeComInboundMessage.send_state == "sending",
        ).with_for_update())
        if message is None:
            return
        user = await current_user(session, message, lock=True)
        if user is None:
            cancel(session, message)
            return
        principal = await load_principal(session, user)
        sources = message.answer.get("sources", [])
        ids = {UUID(source["chunk_id"]) for source in sources}
        if sources:
            await session.execute(select(Document.id).where(
                Document.id.in_({UUID(source["document_id"]) for source in sources})
            ).order_by(Document.id).with_for_update())
        allowed = set((await session.execute(visible_chunks(principal).where(Chunk.id.in_(ids)))).scalars()) if ids else set()
        if allowed != ids or user.permission_version != message.permission_version:
            message.answer = RagAnswer(PERMISSION_CHANGED, "permission_changed").payload()
        try:
            send_start = time.perf_counter()
            async with asyncio.timeout(45):
                # 每条问题使用稳定编号，重试可去重，不同问题的相同答案不会被企微误去重。
                content = str(message.answer["text"]) + f"\n问答编号：{message.id.hex[:12]}"
                message.send_msg_id = await delivery.send_text(message.wecom_userid, content)
            message.state, message.send_state, message.error_code = "completed", "sent", None
        except DeliveryError as exc:
            message.error_code = exc.code
            if exc.uncertain:
                message.state, message.send_state = "failed", "unknown"
            elif exc.retryable and message.send_attempt_count < MAX_ATTEMPTS:
                message.state, message.send_state = "queued", "retry"
                message.next_attempt_at = now() + timedelta(seconds=5 * 2 ** message.send_attempt_count)
            else:
                message.state, message.send_state = "failed", "failed"
        except Exception as exc:
            # sending 检查点之后的未知异常不自动重发，异常正文可能包含 token URL。
            logger.error("RAG delivery failed type=%s", type(exc).__name__)
            message.state, message.send_state, message.error_code = "failed", "unknown", "delivery_unknown"
        message.worker_id, message.lease_expires_at = None, None
        message.diagnostics = {**(message.diagnostics or {}), "send_ms": round((time.perf_counter() - send_start) * 1000),
                               "send_error_code": message.error_code, "outcome": message.answer["outcome"],
                               "cited_chunk_ids": [source["chunk_id"] for source in message.answer.get("sources", [])]}
        if message.state != "queued":
            message.completed_at = now()
        audit(session, message, "rag.delivery")


async def record_failure(message_id: UUID, worker_id: str, exc: Exception) -> None:
    logger.error("RAG processing failed message_id=%s type=%s", message_id, type(exc).__name__)
    async with SessionFactory() as session, session.begin():
        await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
        message = await session.scalar(select(WeComInboundMessage).where(
            WeComInboundMessage.id == message_id, WeComInboundMessage.worker_id == worker_id,
        ).with_for_update())
        if message is None:
            return
        message.error_code = exc.code if isinstance(exc, VoiceError) else "rag_processing_error"
        if message.send_state == "sending":
            message.state, message.send_state, message.completed_at = "failed", "unknown", now()
        else:
            message.state = "queued"
            message.next_attempt_at = now() + timedelta(seconds=10)
            if message.attempt_count >= MAX_ATTEMPTS:
                message.answer = RagAnswer(SERVICE_ERROR, "service_error").payload()
        message.worker_id, message.lease_expires_at = None, None
        audit(session, message, "rag.failed")


async def purge_message_content() -> None:
    """到期清除问题、答案及媒体标识，保留 MsgId 幂等墓碑与无正文审计。"""
    async with SessionFactory() as session, session.begin():
        await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
        await session.execute(update(WeComInboundMessage).where(
            WeComInboundMessage.tenant_id == get_settings().we_com_tenant_id,
            WeComInboundMessage.state.in_(("completed", "failed")),
            WeComInboundMessage.received_at < now() - timedelta(days=get_settings().rag_message_retention_days),
            or_(WeComInboundMessage.content != "", WeComInboundMessage.answer.is_not(None),
                WeComInboundMessage.media_id.is_not(None), cast(WeComInboundMessage.diagnostics, Text) != "{}"),
        ).values(content="", answer=None, media_id=None, diagnostics={}))


async def run_rag_worker() -> None:
    """消费通知唤醒并扫描数据库，RabbitMQ 暂不可用时仍可恢复已入库任务。"""
    worker_id = f"rag-{os.getpid()}-{uuid4().hex[:8]}"
    redis = create_redis_client(get_settings())
    delivery = WeComDelivery(redis)
    connection = None
    wake = asyncio.Event()
    last_purge = now() - timedelta(hours=2)
    try:
        try:
            connection = await aio_pika.connect_robust(get_settings().rabbitmq_url.get_secret_value(), timeout=3)
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=1)
            queue = await channel.declare_queue(QUEUE_NAME, durable=True)

            async def notified(message: aio_pika.abc.AbstractIncomingMessage) -> None:
                async with message.process():
                    payload = json.loads(message.body)
                    if payload.get("tenant_id") == str(get_settings().we_com_tenant_id):
                        wake.set()

            await queue.consume(notified)
        except Exception:
            logger.warning("RAG queue unavailable; database recovery scan remains active")
        while True:
            if now() - last_purge > timedelta(hours=1):
                await purge_message_content()
                last_purge = now()
            message_id = await claim_message(worker_id)
            if message_id is None:
                try:
                    await asyncio.wait_for(wake.wait(), timeout=2)
                except TimeoutError:
                    pass
                wake.clear()
                continue
            renewal = asyncio.create_task(heartbeat(message_id, worker_id))
            try:
                await process_message(message_id, worker_id, delivery)
            except Exception as exc:
                await record_failure(message_id, worker_id, exc)
            finally:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
    finally:
        if connection is not None:
            await connection.close()
        await redis.aclose()
