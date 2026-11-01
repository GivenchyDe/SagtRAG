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


def summary_item(message: Message, employee_name: str) -> dict:
    expired = content_expired(message)
    # 意图标签不含正文，过期后保留用于统计和筛选。
    return {"id": str(message.id), "employee_name": employee_name, "msg_type": message.msg_type,
            "question": None if expired else message.content[:160], "state": message.state,
            "send_state": message.send_state, "outcome": None if expired else (message.answer or {}).get("outcome"),
            "error_code": message.error_code, "received_at": message.received_at,
            "completed_at": message.completed_at, "attempt_count": message.attempt_count,
            "send_attempt_count": message.send_attempt_count, "content_expired": expired,
            "intent": message.intent, "predicted_intent": message.predicted_intent,
            "intent_confidence": message.intent_confidence, "intent_source": message.intent_source,
            "route_ms": message.route_ms}


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
                               if key not in {"candidates", "dense_order", "fts_order", "rerank_scores", "selected_chunk_ids", "cited_chunk_ids"}},
                  candidates=candidates)
    session.add(AdminAuditEvent(tenant_id=admin.tenant_id, admin_user_id=admin.id,
        action="qa.detail_viewed", details={"message_id": str(message_id)}))
    await session.commit()
    return result
