"""租户管理员的问答记录、处理状态与检索诊断，只读不重放消息。"""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import CurrentAdmin
from app.config import get_settings
from app.db import get_db
from app.models import AdminAuditEvent, Document, User, WeComInboundMessage as Message

router = APIRouter(prefix="/api/v1/admin/qa-messages", tags=["admin-qa"])


def content_expired(message: Message) -> bool:
    return message.received_at < datetime.now(timezone.utc) - timedelta(days=get_settings().rag_message_retention_days)


def _elapsed_ms(start, end) -> int | None:
    """只按实际时间戳计算；缺任一端返回 None，不用 0 表示未知。"""
    if start is None or end is None:
        return None
    return max(0, round((end - start).total_seconds() * 1000))


def _trace_int(trace: dict, key: str) -> int | None:
    value = trace.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, round(value))


def timing_projection(message: Message, trace: dict) -> dict:
    """按不同统计范围拆分耗时；历史记录缺测保持 null。"""
    voice = trace.get("voice") if isinstance(trace.get("voice"), dict) else {}
    end_to_end = _elapsed_ms(message.received_at, message.completed_at)
    if end_to_end is None:
        end_to_end = _trace_int(trace, "end_to_end_ms")
    queue_wait = _trace_int(trace, "queue_wait_ms")
    if queue_wait is None:
        queue_wait = _elapsed_ms(message.received_at, message.first_claimed_at)
    route_reused = bool(trace.get("route_reused"))
    route_ms = None if route_reused else message.route_ms
    return {
        # 收到→最终终态：含排队、处理与发送；完成/失败/未知均记录，不含企微投递到回调。
        "end_to_end_ms": end_to_end,
        # 收到→第一次领取：只记录首次领取，重试不重置。
        "queue_wait_ms": queue_wait,
        # 当前/最近一次处理尝试：从开始处理到答案生成完成，含语音与分类。
        "processing_ms": _trace_int(trace, "processing_ms"),
        # 复用检查点时不把首次分类耗时当作本次阶段耗时；历史值另列。
        "route_ms": route_ms,
        "route_reused": route_reused,
        "historical_route_ms": message.route_ms if route_reused else None,
        "voice_ms": _trace_int(voice, "voice_ms"),
        "asr_ms": _trace_int(voice, "asr_ms"),
        # 检索含查询向量、Dense、FTS 与 RRF；下面另有子阶段。
        "embedding_ms": _trace_int(trace, "embedding_ms"),
        "dense_ms": _trace_int(trace, "dense_ms"),
        "fts_ms": _trace_int(trace, "fts_ms"),
        "rrf_ms": _trace_int(trace, "rrf_ms"),
        "retrieval_ms": _trace_int(trace, "retrieval_ms"),
        "rerank_ms": _trace_int(trace, "rerank_ms"),
        "generation_ms": _trace_int(trace, "generation_ms"),
        # 生成开始→首个 content；不是用户实际看到答案的时间。
        "generation_first_content_ms": _trace_int(trace, "generation_first_content_ms"),
        # answer_question 内部总耗时，不含排队、ASR、分类和发送。
        "rag_total_ms": _trace_int(trace, "rag_total_ms"),
        # 最近一次企微发送接口调用；重试历史见 send_attempts。
        "send_ms": _trace_int(trace, "send_ms"),
    }


def summary_item(message: Message, employee_name: str) -> dict:
    expired = content_expired(message)
    trace = {} if expired else (message.diagnostics or {})
    # 意图标签不含正文，过期后保留用于统计和筛选。
    return {"id": str(message.id), "employee_name": employee_name, "msg_type": message.msg_type,
            "question": None if expired else message.content[:160], "state": message.state,
            "send_state": message.send_state, "outcome": None if expired else (message.answer or {}).get("outcome"),
            "error_code": message.error_code, "received_at": message.received_at,
            "first_claimed_at": message.first_claimed_at,
            "completed_at": message.completed_at, "attempt_count": message.attempt_count,
            "send_attempt_count": message.send_attempt_count, "content_expired": expired,
            "progress_state": getattr(message, "progress_state", "none"),
            "progress_attempt_count": getattr(message, "progress_attempt_count", 0),
            "progress_sent_at": getattr(message, "progress_sent_at", None),
            "manual_recovery_required": (
                message.send_state == "unknown" and getattr(message, "answer", None) is not None),
            "intent": message.intent, "predicted_intent": message.predicted_intent,
            "intent_confidence": message.intent_confidence, "intent_source": message.intent_source,
            "route_ms": message.route_ms,
            "diagnostic_code": None if expired else trace.get("diagnostic_code"),
            "timing": timing_projection(message, trace)}


@router.get("")
async def list_messages(
    admin: CurrentAdmin, session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    state: Literal["received", "queued", "processing", "completed", "failed"] | None = None,
    send_state: Literal["pending", "sending", "retry", "sent", "unknown", "cancelled", "failed"] | None = None,
    msg_type: Literal["text", "voice"] | None = None,
    intent: Literal["chitchat", "knowledge_query", "out_of_scope"] | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> dict:
    conditions = [Message.tenant_id == admin.tenant_id]
    if state:
        conditions.append(Message.state == state)
    if send_state:
        conditions.append(Message.send_state == send_state)
    if msg_type:
        conditions.append(Message.msg_type == msg_type)
    if intent:
        conditions.append(Message.intent == intent)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        cutoff = datetime.now(timezone.utc) - timedelta(days=get_settings().rag_message_retention_days)
        conditions.append(or_(User.display_name.ilike(pattern),
                              func.replace(cast(Message.id, Text), "-", "").ilike(pattern),
                              (Message.received_at >= cutoff) & Message.content.ilike(pattern)))
    join = (User.id == Message.user_id) & (User.tenant_id == Message.tenant_id)
    total = await session.scalar(select(func.count()).select_from(Message).join(User, join).where(*conditions))
    rows = (await session.execute(select(Message, User.display_name).join(User, join).where(*conditions)
            .order_by(Message.received_at.desc(), Message.id).limit(limit).offset(offset))).all()
    stats = (await session.execute(select(
        func.count().label("total"),
        func.count().filter(Message.state.in_(("received", "queued"))).label("queued"),
        func.count().filter(Message.state == "processing").label("processing"),
        func.count().filter(Message.send_state == "sent").label("sent"),
        func.count().filter(Message.state == "failed").label("failed"),
    ).where(Message.tenant_id == admin.tenant_id))).mappings().one()
    return {"items": [summary_item(message, name) for message, name in rows], "total": total or 0,
            "limit": limit, "offset": offset, "stats": dict(stats)}


@router.get("/{message_id}")
async def message_detail(message_id: UUID, admin: CurrentAdmin, session: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    row = (await session.execute(select(Message, User.display_name).join(User,
        (User.id == Message.user_id) & (User.tenant_id == Message.tenant_id))
        .where(Message.id == message_id, Message.tenant_id == admin.tenant_id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="问答记录不存在。")
    message, name = row
    result = summary_item(message, name)
    expired = result["content_expired"]
    trace = {} if expired else (message.diagnostics or {})
    # 文档标题按当前租户读取，诊断本身只持久化分块/文档标识与分数，不复制全文。
    candidates = []
    doc_ids = [UUID(item["document_id"]) for item in trace.get("candidates", [])]
    titles = dict((await session.execute(select(Document.id, Document.title).where(
        Document.tenant_id == admin.tenant_id, Document.id.in_(doc_ids)))).all()) if doc_ids else {}
    for item in trace.get("candidates", []):
        key = item["chunk_id"]
        candidates.append({**item, "title": titles.get(UUID(item["document_id"]), "历史文档"),
            "dense_rank": trace.get("dense_order", []).index(key) + 1 if key in trace.get("dense_order", []) else None,
            "fts_rank": trace.get("fts_order", []).index(key) + 1 if key in trace.get("fts_order", []) else None,
            "rerank_score": trace.get("rerank_scores", {}).get(key),
            "selected": key in trace.get("selected_chunk_ids", []), "cited": key in trace.get("cited_chunk_ids", [])})
    result.update(question=None if expired else message.content, answer=None if expired else message.answer,
                  diagnostics={key: value for key, value in trace.items()
                               if key not in {"candidates", "dense_order", "fts_order", "rerank_scores",
                                              "selected_chunk_ids", "cited_chunk_ids", "attempts", "policy"}},
                  timing=timing_projection(message, trace),
                  attempts=trace.get("attempts", []),
                  send_attempts=trace.get("send_attempts", []),
                  policy=trace.get("policy"),
                  candidates=candidates)
    session.add(AdminAuditEvent(tenant_id=admin.tenant_id, admin_user_id=admin.id,
        action="qa.detail_viewed", details={"message_id": str(message_id)}))
    await session.commit()
    return result


@router.post("/{message_id}/recover")
async def recover_unknown(message_id: UUID, admin: CurrentAdmin,
                          session: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """人工确认后才重新排队发送既有答案；绝不自动重发 unknown。"""
    message = await session.scalar(select(Message).where(
        Message.id == message_id, Message.tenant_id == admin.tenant_id).with_for_update())
    if message is None:
        raise HTTPException(status_code=404, detail="问答记录不存在。")
    if message.send_state != "unknown" or message.answer is None:
        raise HTTPException(status_code=409, detail="仅未知送达且已有答案的记录可人工恢复。")
    if content_expired(message):
        raise HTTPException(status_code=409, detail="正文已超过保留期限，不能恢复发送。")
    if message.send_attempt_count >= 3:
        raise HTTPException(status_code=409, detail="发送尝试次数已达上限。")
    message.state, message.send_state = "queued", "pending"
    message.next_attempt_at = datetime.now(timezone.utc)
    message.worker_id, message.lease_expires_at, message.completed_at = None, None, None
    message.error_code = None
    session.add(AdminAuditEvent(
        tenant_id=admin.tenant_id, admin_user_id=admin.id,
        action="qa.manual_recovery_queued",
        details={"message_id": str(message_id), "send_state": "unknown"},
    ))
    await session.commit()
    return {"message_id": str(message_id), "state": "queued", "send_state": "pending",
            "automatic": False}
