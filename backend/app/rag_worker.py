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
from sqlalchemy import Text, cast, delete, exists, func, or_, select, text, update
from sqlalchemy.orm import aliased

from app import observability
from app.chitchat import answer_chitchat, answer_out_of_scope
from app.config import get_settings
from app.conversation import (DEFAULT_CONVERSATION_KEY, apply_resolution, load_snapshot,
                              resolve as resolve_conversation)
from app.db import SessionFactory, set_tenant_rls_context
from app.deadline import (Deadline, DeadlineExceeded, current_deadline,
                          reset_current_deadline, set_current_deadline)
from app.http_clients import close_client
from app.integrations import create_redis_client
from app.intent_router import IntentResult, build_policy_version, detect_intent
from app.models import (AuditEvent, Chunk, ConversationState, Document, User, UserStatus,
                        WeComInboundMessage)
from app.permissions import load_principal
from app.queueing import QUEUE_NAME
from app.rag import (PERMISSION_CHANGED, SERVICE_ERROR, RagAnswer, answer_question,
                     reset_developer_mode, reset_generation_checkpoint,
                     set_developer_mode, set_generation_checkpoint, strip_citations)
from app.retrieval import visible_chunks
from app.wecom_delivery import DeliveryError, WeComDelivery
from app.voice import VoiceError, VOICE_MESSAGES, transcribe_voice

logger = logging.getLogger("sagtrag.rag")
MAX_ATTEMPTS = 3
PROGRESS_TEXT = "收到，正在帮你查资料，请稍等一下。"


async def maybe_send_progress(
    message_id: UUID, worker_id: str, delivery: WeComDelivery, *, after_seconds: float,
) -> None:
    """等待阈值后最多发送一次进度消息；进度不占用最终答案/送达状态。"""
    await asyncio.sleep(max(0.0, after_seconds))
    async with SessionFactory() as session, session.begin():
        await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
        message = await session.scalar(select(WeComInboundMessage).where(
            WeComInboundMessage.id == message_id,
            WeComInboundMessage.worker_id == worker_id,
            WeComInboundMessage.state == "processing",
            WeComInboundMessage.answer.is_(None),
            WeComInboundMessage.send_state.in_(("pending", "retry")),
            WeComInboundMessage.progress_attempt_count == 0,
            WeComInboundMessage.progress_state == "none",
        ).with_for_update())
        if message is None:
            return
        message.progress_state = "sending"
        message.progress_attempt_count = 1
        recipient = message.wecom_userid
    started = time.perf_counter()
    state, code, msg_id = "sent", None, None
    try:
        msg_id = await delivery.send_text(recipient, PROGRESS_TEXT, duplicate_check=False)
    except DeliveryError as exc:
        state = "unknown" if exc.uncertain else "failed"
        code = exc.code
    except Exception as exc:
        state, code = "unknown", "progress_send_unknown"
        logger.warning("progress send failed type=%s", type(exc).__name__)
    async with SessionFactory() as session, session.begin():
        await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
        message = await session.scalar(select(WeComInboundMessage).where(
            WeComInboundMessage.id == message_id,
            WeComInboundMessage.worker_id == worker_id,
            WeComInboundMessage.progress_state == "sending",
        ).with_for_update())
        if message is None:
            return
        message.progress_state = state
        message.progress_msg_id = msg_id
        message.progress_error = code
        if state == "sent":
            message.progress_sent_at = now()
        diagnostics = dict(message.diagnostics or {})
        diagnostics["progress_state"] = state
        diagnostics["progress_error"] = code
        diagnostics["progress_attempt_count"] = message.progress_attempt_count
        diagnostics["progress_ms"] = observability.perf_ms(started)
        if message.progress_sent_at is not None:
            diagnostics["progress_sent_at"] = observability.iso_utc(message.progress_sent_at)
        message.diagnostics = diagnostics


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
    """领取排队或过期任务；同一员工最多一个处理中，跨Worker用数据库协调。"""
    async with SessionFactory() as session, session.begin():
        await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
        sibling = aliased(WeComInboundMessage)
        same_user_processing = exists(select(1).where(
            sibling.tenant_id == WeComInboundMessage.tenant_id,
            sibling.user_id == WeComInboundMessage.user_id,
            sibling.state == "processing",
            sibling.lease_expires_at >= func.now(),
        ))
        # 最近领取时间作为公平分数：有大量积压的员工排在刚被服务过的员工之后。
        fairness = select(func.max(sibling.last_claimed_at)).where(
            sibling.tenant_id == WeComInboundMessage.tenant_id,
            sibling.user_id == WeComInboundMessage.user_id,
        ).correlate(WeComInboundMessage).scalar_subquery()
        message = await session.scalar(
            select(WeComInboundMessage).where(
                WeComInboundMessage.tenant_id == get_settings().we_com_tenant_id,
                ~same_user_processing,
                or_(
                    (WeComInboundMessage.state.in_(("received", "queued")))
                    & (WeComInboundMessage.next_attempt_at <= func.now()),
                    (WeComInboundMessage.state == "processing")
                    & (WeComInboundMessage.lease_expires_at < func.now()),
                ),
            ).order_by(
                fairness.asc().nullsfirst(),
                WeComInboundMessage.received_at,
            ).with_for_update(skip_locked=True).limit(1)
        )
        if message is None:
            return None
        if message.send_state == "sending":
            message.state, message.send_state = "failed", "unknown"
            message.error_code = "send_interrupted_unknown"
            message.completed_at = now()
            message.worker_id, message.lease_expires_at = None, None
            diagnostics = dict(message.diagnostics or {})
            diagnostics["diagnostic_code"] = "send_interrupted_unknown"
            observability.record_completion_time(
                diagnostics, received_at=message.received_at, completed_at=message.completed_at)
            message.diagnostics = diagnostics
            audit(session, message, "rag.send_unknown")
            return None
        if message.progress_state == "sending":
            # 进度请求可能已发出，重领后不确认也不重发。
            message.progress_state = "unknown"
            message.progress_error = "progress_interrupted_unknown"
        # PostgreSQL advisory xact lock 只协调同一员工；进程内锁不能替代跨Worker协调。
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            locked = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"{message.tenant_id}:{message.user_id}"},
            )
            if not locked:
                return None
        if message.attempt_count >= MAX_ATTEMPTS and message.answer is None:
            message.answer = RagAnswer(SERVICE_ERROR, "service_error").payload()
        claimed_at = now()
        message.state = "processing"
        message.worker_id = worker_id
        message.attempt_count += 1
        message.lease_expires_at = claimed_at + timedelta(minutes=3)
        message.last_claimed_at = claimed_at
        # 首次领取时间只写一次；后续重试和发送重试都不能覆盖。
        if message.first_claimed_at is None:
            message.first_claimed_at = claimed_at
        diagnostics = dict(message.diagnostics or {})
        if "queue_wait_ms" not in diagnostics:
            queue_wait_ms = observability.elapsed_ms(message.received_at, claimed_at)
            if queue_wait_ms is not None:
                diagnostics["queue_wait_ms"] = queue_wait_ms
        # 只有实际需要生成时才追加处理尝试；复用答案的发送重试只记发送尝试。
        if message.answer is None:
            observability.append_attempt(
                diagnostics, number=message.attempt_count, kind="processing", started_at=claimed_at)
        message.diagnostics = diagnostics
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
    diagnostics = dict(message.diagnostics or {})
    diagnostics.update({"outcome": "identity_revoked", "cited_chunk_ids": [],
                        "diagnostic_code": observability.IDENTITY_REVOKED})
    observability.finish_open_attempt(
        diagnostics, outcome="identity_revoked", error_code=observability.IDENTITY_REVOKED, finished_at=now())
    message.worker_id, message.lease_expires_at, message.completed_at = None, None, now()
    observability.record_completion_time(
        diagnostics, received_at=message.received_at, completed_at=message.completed_at)
    message.diagnostics = diagnostics
    audit(session, message, "rag.cancelled")


def _record_intent_diagnostics(diagnostics: dict, result: IntentResult, *, reused: bool = False) -> None:
    diagnostics.update({
        "intent": result.intent,
        "predicted_intent": result.predicted_intent,
        "intent_confidence": result.confidence,
        "intent_source": result.source,
        "intent_rule_id": result.rule_id,
        "intent_model": result.model,
        "intent_reason": result.reason,
        "intent_error": result.error,
        "policy_version": result.policy_version,
    })
    # 缺测保持未知；复用检查点时不再写新的路由耗时，也不把缺失的旧值填成 0。
    if result.elapsed_ms is not None:
        diagnostics["route_ms"] = result.elapsed_ms
    elif not reused:
        diagnostics.pop("route_ms", None)
    if reused:
        diagnostics["route_reused"] = True


async def ensure_intent(message: WeComInboundMessage, diagnostics: dict, *, question: str | None = None) -> IntentResult:
    """路由一次并持久化；重试时复用检查点，不重复分类。"""
    settings = get_settings()
    route_text = question if question is not None else message.content
    if message.intent is not None:
        result = IntentResult(
            predicted_intent=message.predicted_intent,
            intent=message.intent,
            confidence=message.intent_confidence,
            source=message.intent_source or "checkpoint",
            model=message.intent_model,
            rule_id=message.intent_rule_id,
            reason=(message.diagnostics or {}).get("intent_reason"),
            elapsed_ms=message.route_ms,
            error=message.intent_error,
            policy_version=message.policy_version or build_policy_version(settings),
        )
        _record_intent_diagnostics(diagnostics, result, reused=True)
        return result

    result = await detect_intent(route_text, settings=settings)
    message.predicted_intent = result.predicted_intent
    message.intent = result.intent
    message.intent_confidence = result.confidence
    message.intent_source = result.source
    message.intent_model = result.model
    message.intent_rule_id = result.rule_id
    message.intent_error = result.error
    message.route_ms = result.elapsed_ms
    message.policy_version = result.policy_version
    _record_intent_diagnostics(diagnostics, result)
    # 检查点保存独立快照；后续 RAG 原地补充诊断时不能修改 ORM 的已提交 JSON。
    message.diagnostics = copy.deepcopy(diagnostics)
    return result


# 异常跨 Session 交接：外层 record_failure 可能拿到新的异常对象，不能只依赖异常属性。
_FAILURE_HANDOFF: dict[tuple[str, str], tuple[dict, datetime | None, int | None, int | None]] = {}


def _attach_failure_snapshot(
    exc: Exception, diagnostics: dict, *, message_id: UUID, worker_id: str,
    attempt_number: int | None, started_at: datetime | None, duration_ms: int | None,
) -> None:
    """把本次安全诊断快照交给外层 record_failure，避免会话回滚后丢失。"""
    try:
        snapshot = copy.deepcopy(diagnostics)
        setattr(exc, "s04_diagnostics", snapshot)
        setattr(exc, "s04_attempt_number", attempt_number)
        setattr(exc, "s04_attempt_started_at", started_at)
        setattr(exc, "s04_attempt_duration_ms", duration_ms)
        _FAILURE_HANDOFF[(str(message_id), worker_id)] = (
            snapshot, started_at, duration_ms, attempt_number)
        if len(_FAILURE_HANDOFF) > 64:
            _FAILURE_HANDOFF.pop(next(iter(_FAILURE_HANDOFF)))
    except Exception:
        # 交接失败不能掩盖原始异常；外层会退回数据库检查点。
        pass


async def process_message(message_id: UUID, worker_id: str, delivery: WeComDelivery) -> None:
    """为本次处理尝试建立统一期限；排队等待不计入该期限。"""
    settings = get_settings()
    deadline = Deadline(getattr(settings, "rag_processing_deadline_seconds", 240.0))
    token = set_current_deadline(deadline)
    progress = asyncio.create_task(maybe_send_progress(
        message_id, worker_id, delivery,
        after_seconds=float(getattr(settings, "rag_progress_after_seconds", 8.0))))
    try:
        await _process_message(message_id, worker_id, delivery)
    finally:
        progress.cancel()
        await asyncio.gather(progress, return_exceptions=True)
        reset_current_deadline(token)


async def _process_message(message_id: UUID, worker_id: str, delivery: WeComDelivery) -> None:
    """先保存生成结果，再独立发送；重试发送复用答案而不重复检索。"""
    async with SessionFactory() as session:
        await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
        message = await session.get(WeComInboundMessage, message_id)
        if message is None or message.worker_id != worker_id or message.state != "processing":
            return
        processing_started = time.perf_counter()
        attempt_started_at = now()
        diagnostics = copy.deepcopy(message.diagnostics or {})
        active_deadline = current_deadline()
        if active_deadline is not None:
            diagnostics["processing_deadline_seconds"] = active_deadline.total_seconds
        user = await current_user(session, message)
        if user is None:
            cancel(session, message)
            await session.commit()
            return
        if message.answer is not None and message.permission_version is None:
            # 达到处理重试上限后的固定错误回复没有检索内容，使用当前身份发送。
            message.permission_version = user.permission_version
        if message.answer is None:
            # 新处理尝试清空上一次的阶段字段；历史尝试的 timings 仍保留在 attempts。
            observability.clear_attempt_artifacts(diagnostics)
            if "policy" not in diagnostics:
                # 只保存允许字段的摘要和指纹，不写密钥、URL 或完整提示词。
                diagnostics["policy"] = observability.policy_snapshot(get_settings())
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
                            _attach_failure_snapshot(
                                exc, diagnostics, message_id=message_id, worker_id=worker_id, attempt_number=message.attempt_count,
                                started_at=attempt_started_at,
                                duration_ms=observability.perf_ms(processing_started))
                            raise
                        voice_failure = RagAnswer(VOICE_MESSAGES[exc.code], exc.code)
                        question = ""
                    except Exception as exc:
                        _attach_failure_snapshot(
                            exc, diagnostics, message_id=message_id, worker_id=worker_id, attempt_number=message.attempt_count,
                            started_at=attempt_started_at,
                            duration_ms=observability.perf_ms(processing_started))
                        raise
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
            elif message.msg_type == "voice" and isinstance(diagnostics.get("voice"), dict):
                # 复用已提交的转写：新尝试标记语音来源，不把首次耗时当作本次执行。
                voice = dict(diagnostics["voice"])
                voice["reused"] = True
                diagnostics["voice"] = voice
            # 转写检查点已提交；后续诊断使用独立对象，避免原地更新 JSON 被 ORM 漏写。
            diagnostics = copy.deepcopy(diagnostics)
            settings = get_settings()
            conversation_ttl = getattr(settings, "conversation_ttl_minutes", 60)
            conversation_snapshot = await load_snapshot(
                session, tenant_id=message.tenant_id, user_id=message.user_id,
                conversation_key=DEFAULT_CONVERSATION_KEY)
            resolution = resolve_conversation(
                message.content, conversation_snapshot, now=now(),
                ttl_minutes=conversation_ttl,
                clarify_message=getattr(settings, "clarify_message",
                                        "请补充完整的问题，或说明要查询的制度、流程或产品。"))
            diagnostics["conversation"] = {
                "source": resolution.source,
                "topic": resolution.topic,
                "pending_fields": list(resolution.pending_fields),
                "query_completed": resolution.query != message.content,
                "correction": resolution.correction,
                "state_version": resolution.state_version,
            }
            version = user.permission_version
            principal = await load_principal(session, user)
            intent_result: IntentResult | None = None
            if voice_failure is not None:
                answer = voice_failure
            else:
                if resolution.clarification is not None:
                    try:
                        clarification_policy = build_policy_version(settings)
                    except AttributeError:
                        clarification_policy = message.policy_version or "conversation"
                    intent_result = IntentResult(
                        predicted_intent="chitchat", intent="chitchat", confidence=1.0,
                        source="conversation", model=None, rule_id="conversation_clarify",
                        reason="pending_fields", elapsed_ms=0, error=None,
                        policy_version=clarification_policy)
                    _record_intent_diagnostics(diagnostics, intent_result)
                    answer = RagAnswer(
                        resolution.clarification, "chitchat",
                        diagnostic_code="conversation_clarify",
                        detail={"pending_fields": list(resolution.pending_fields)})
                else:
                    try:
                        intent_result = await ensure_intent(
                            message, diagnostics, question=resolution.query)
                    except Exception as exc:
                        _attach_failure_snapshot(
                            exc, diagnostics, message_id=message_id, worker_id=worker_id,
                            attempt_number=message.attempt_count,
                            started_at=attempt_started_at,
                            duration_ms=observability.perf_ms(processing_started))
                        raise
                    await session.commit()
                    await set_tenant_rls_context(session, tenant_id=settings.we_com_tenant_id)
                    policy = dict(diagnostics.get("policy") or {})
                    policy["policy_version"] = intent_result.policy_version
                    policy["intent_source"] = intent_result.source
                    diagnostics["policy"] = policy
                    try:
                        active_deadline = current_deadline()
                        remaining = active_deadline.remaining() if active_deadline else 240.0
                        async with asyncio.timeout(max(0.05, remaining)):
                            if intent_result.intent == "chitchat":
                                answer = await answer_chitchat(
                                    message.content, intent_result, settings=settings,
                                    diagnostics=diagnostics)
                            elif intent_result.intent == "out_of_scope":
                                answer = answer_out_of_scope(intent_result, settings)
                            else:
                                async def checkpoint(snapshot: dict) -> None:
                                    # 修复预算在每次生成前落库；崩溃重试不重置已消耗次数。
                                    message.diagnostics = copy.deepcopy(snapshot)
                                    await session.commit()
                                    await set_tenant_rls_context(
                                        session, tenant_id=settings.we_com_tenant_id)
                                token = set_generation_checkpoint(checkpoint)
                                developer_token = set_developer_mode(bool(principal.developer_mode))
                                try:
                                    answer = await answer_question(
                                        session, principal, resolution.query, diagnostics=diagnostics)
                                finally:
                                    reset_developer_mode(developer_token)
                                    reset_generation_checkpoint(token)
                    except Exception as exc:
                        _attach_failure_snapshot(
                            exc, diagnostics, message_id=message_id, worker_id=worker_id,
                            attempt_number=message.attempt_count,
                            started_at=attempt_started_at,
                            duration_ms=observability.perf_ms(processing_started))
                        raise
                    if intent_result.intent == "chitchat" and answer.outcome == "service_error":
                        diagnostics["chitchat_error"] = "chitchat_generation_error"
            conversation_persist = bool(resolution.should_persist) and (
                resolution.clarification is not None or (
                    intent_result is not None
                    and intent_result.intent == "knowledge_query"
                    and answer.outcome not in {
                        "service_error", "permission_changed", "identity_revoked", "answer_too_long",
                    }
                )
            )
            try:
                await session.commit()
                await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
                message = await session.scalar(select(WeComInboundMessage).where(
                    WeComInboundMessage.id == message_id,
                    WeComInboundMessage.worker_id == worker_id,
                    WeComInboundMessage.state == "processing",
                ).with_for_update())
                if message is None:
                    return
                processing_ms = observability.perf_ms(processing_started)
                diagnostics["processing_ms"] = processing_ms
                if active_deadline is not None:
                    diagnostics["deadline_remaining_ms"] = round(active_deadline.remaining() * 1000)
                diagnostics["diagnostic_code"] = answer.diagnostic_code or answer.outcome
                attempt_error = None
                if observability.is_failure_outcome(answer.outcome):
                    attempt_error = answer.diagnostic_code or answer.outcome
                observability.finish_open_attempt(
                    diagnostics, outcome=answer.outcome, error_code=attempt_error,
                    duration_ms=processing_ms, finished_at=now(),
                    timings=observability.attempt_timings(diagnostics))
                if conversation_persist:
                    applied = await apply_resolution(
                        session, message, resolution, now=now(),
                        ttl_minutes=conversation_ttl,
                        conversation_key=DEFAULT_CONVERSATION_KEY)
                    conversation_diagnostics = diagnostics.setdefault("conversation", {})
                    conversation_diagnostics["applied"] = applied
                    if not applied:
                        conversation_diagnostics["apply_skipped"] = "duplicate_or_stale"
                if message.progress_state == "sending":
                    # 进度请求在最终答案提交时仍未确认；保守标 unknown，绝不重发。
                    message.progress_state = "unknown"
                    message.progress_error = "progress_inflight_final"
                message.answer, message.permission_version = answer.payload(), version
                message.diagnostics = {**diagnostics, "outcome": answer.outcome}
                audit(session, message, "rag.generated")
                await session.commit()
            except Exception as exc:
                _attach_failure_snapshot(
                    exc, diagnostics, message_id=message_id, worker_id=worker_id,
                    attempt_number=message.attempt_count, started_at=attempt_started_at,
                    duration_ms=observability.perf_ms(processing_started))
                raise

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
            diagnostics = dict(message.diagnostics or {})
            diagnostics["diagnostic_code"] = "send_attempt_limit"
            observability.record_completion_time(
                diagnostics, received_at=message.received_at, completed_at=message.completed_at)
            message.diagnostics = diagnostics
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
        send_started_at = now()
        send_start = time.perf_counter()
        try:
            developer = bool(principal.developer_mode)
            if developer:
                # 开发者模式保留稳定编号，便于对照日志与重复发送排查。
                content = str(message.answer["text"]) + f"\n问答编号：{message.id.hex[:12]}"
            else:
                content = message.answer.get("plain_text") or strip_citations(str(message.answer["text"]))
            async with asyncio.timeout(45):
                message.send_msg_id = await delivery.send_text(
                    message.wecom_userid, str(content), duplicate_check=developer)
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
        send_finished_at = now()
        send_ms = observability.perf_ms(send_start)
        diagnostics = dict(message.diagnostics or {})
        observability.append_send_attempt(
            diagnostics, number=message.send_attempt_count, started_at=send_started_at,
            finished_at=send_finished_at, duration_ms=send_ms, state=message.send_state,
            error_code=message.error_code)
        diagnostics.update({
            "send_ms": send_ms, "send_error_code": message.error_code,
            "outcome": message.answer["outcome"],
            "cited_chunk_ids": [source["chunk_id"] for source in message.answer.get("sources", [])],
        })
        if message.answer["outcome"] == "permission_changed":
            diagnostics["diagnostic_code"] = observability.PERMISSION_CHANGED
        if message.state != "queued":
            message.completed_at = send_finished_at
            observability.record_completion_time(
                diagnostics, received_at=message.received_at, completed_at=message.completed_at)
        message.diagnostics = diagnostics
        audit(session, message, "rag.delivery")


async def record_failure(message_id: UUID, worker_id: str, exc: Exception) -> None:
    logger.error("RAG processing failed message_id=%s type=%s", message_id, type(exc).__name__)
    handoff = _FAILURE_HANDOFF.pop((str(message_id), worker_id), None)
    async with SessionFactory() as session, session.begin():
        await set_tenant_rls_context(session, tenant_id=get_settings().we_com_tenant_id)
        message = await session.scalar(select(WeComInboundMessage).where(
            WeComInboundMessage.id == message_id, WeComInboundMessage.worker_id == worker_id,
        ).with_for_update())
        if message is None:
            return
        code = exc.code if isinstance(exc, VoiceError) else "rag_processing_error"
        if isinstance(exc, DeadlineExceeded):
            code = observability.PROCESSING_DEADLINE_EXCEEDED
        snapshot = getattr(exc, "s04_diagnostics", None)
        attempt_number = getattr(exc, "s04_attempt_number", None)
        duration_ms = getattr(exc, "s04_attempt_duration_ms", None)
        attempt_started_at = getattr(exc, "s04_attempt_started_at", None)
        if handoff is not None:
            handoff_snapshot, handoff_started, handoff_duration, handoff_attempt = handoff
            if snapshot is None:
                snapshot = handoff_snapshot
                attempt_started_at = handoff_started
                duration_ms = handoff_duration
            if attempt_number is None:
                attempt_number = handoff_attempt
        if isinstance(snapshot, dict):
            if attempt_number is None:
                attempt_number = next(
                    (item.get("number") for item in reversed(snapshot.get("attempts") or [])
                     if isinstance(item, dict)), None)
            # 过期 Worker 只结束它领取的那次尝试，不能覆盖新尝试的诊断。
            if attempt_number is not None and attempt_number != message.attempt_count:
                return
            diagnostics = copy.deepcopy(snapshot)
        else:
            diagnostics = dict(message.diagnostics or {})
        message.error_code = code
        diagnostics["error_code"] = code
        diagnostics["diagnostic_code"] = code
        # 只结束当前尝试；首次领取、排队等待和历史尝试记录均保留。
        observability.finish_open_attempt(
            diagnostics, outcome="processing_exception", error_code=code, finished_at=now(),
            duration_ms=duration_ms, started_at=attempt_started_at,
            timings=observability.attempt_timings(diagnostics))
        if message.send_state == "sending":
            message.state, message.send_state, message.completed_at = "failed", "unknown", now()
            observability.record_completion_time(
                diagnostics, received_at=message.received_at, completed_at=message.completed_at)
        else:
            message.state = "queued"
            message.next_attempt_at = now() + timedelta(seconds=10)
            if message.attempt_count >= MAX_ATTEMPTS:
                message.answer = RagAnswer(SERVICE_ERROR, "service_error").payload()
        message.worker_id, message.lease_expires_at = None, None
        message.diagnostics = diagnostics
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
        # 会话状态已过期且超过消息保留期后删除，避免长期保存用户原话。
        await session.execute(delete(ConversationState).where(
            ConversationState.tenant_id == get_settings().we_com_tenant_id,
            ConversationState.expires_at.is_not(None),
            ConversationState.expires_at < now() - timedelta(days=get_settings().rag_message_retention_days),
        ))


async def _run_claimed(message_id: UUID, worker_id: str, delivery: WeComDelivery) -> None:
    renewal = asyncio.create_task(heartbeat(message_id, worker_id))
    try:
        await process_message(message_id, worker_id, delivery)
    except Exception as exc:
        await record_failure(message_id, worker_id, exc)
    finally:
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)


async def run_worker_loop(
    worker_id: str, delivery: WeComDelivery, *, concurrency: int,
    stop_event: asyncio.Event | None = None, wake: asyncio.Event | None = None,
    claim=claim_message, process=None,
) -> None:
    """有界并发循环：不同员工并行，同一员工由数据库领取互斥保证顺序。"""
    tasks: set[asyncio.Task] = set()
    stop = stop_event or asyncio.Event()
    process_one = process or _run_claimed
    last_purge = now() - timedelta(hours=2)
    try:
        while not stop.is_set():
            if now() - last_purge > timedelta(hours=1):
                await purge_message_content()
                last_purge = now()
            while len(tasks) < concurrency and not stop.is_set():
                message_id = await claim(worker_id)
                if message_id is None:
                    break
                task = asyncio.create_task(process_one(message_id, worker_id, delivery))
                tasks.add(task)
            if stop.is_set():
                break
            if tasks:
                done, _ = await asyncio.wait(tasks, timeout=0.5,
                                             return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    tasks.discard(task)
            else:
                if wake is not None:
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=2)
                    except TimeoutError:
                        pass
                    wake.clear()
                else:
                    await asyncio.sleep(0.1)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def run_rag_worker() -> None:
    """消费通知唤醒并扫描数据库，RabbitMQ 暂不可用时仍可恢复已入库任务。"""
    worker_id = f"rag-{os.getpid()}-{uuid4().hex[:8]}"
    redis = create_redis_client(get_settings())
    delivery = WeComDelivery(redis)
    connection = None
    wake = asyncio.Event()
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
        await run_worker_loop(
            worker_id, delivery,
            concurrency=max(1, getattr(get_settings(), "rag_worker_concurrency", 4)),
            wake=wake)
    finally:
        if connection is not None:
            await connection.close()
        await close_client()
        await redis.aclose()
