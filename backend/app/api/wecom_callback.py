import asyncio
import logging
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.identity import resolve_verified_wecom_user
from app.auth.wecom_crypto import decrypt_message, get_encrypted_element, parse_message, verify_signature
from app.config import get_settings
from app.db import get_db, set_tenant_rls_context
from app.models import WeComInboundMessage

router = APIRouter(prefix="/api/v1/wecom", tags=["WeCom callback"])


@router.get("/callback", include_in_schema=False)
async def verify_wecom_callback(
    msg_signature: Annotated[str, Query(min_length=40, max_length=40)],
    timestamp: Annotated[str, Query(min_length=1, max_length=32)],
    nonce: Annotated[str, Query(min_length=1, max_length=128)],
    echostr: Annotated[str, Query(min_length=1, max_length=4096)],
) -> Response:
    """签名验证通过后，完成企业微信 URL 验证挑战。"""
    verify_signature(signature=msg_signature, timestamp=timestamp, nonce=nonce, encrypted=echostr)
    return Response(content=decrypt_message(echostr), media_type="text/plain; charset=utf-8")


@router.post("/callback", include_in_schema=False)
async def receive_wecom_callback(
    request: Request,
    msg_signature: Annotated[str, Query(min_length=40, max_length=40)],
    timestamp: Annotated[str, Query(min_length=1, max_length=32)],
    nonce: Annotated[str, Query(min_length=1, max_length=128)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """验证身份并持久化幂等任务，快速 ACK；问答和主动回复由 Worker 负责。"""
    settings = get_settings()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.wecom_max_callback_body_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="WeCom callback body too large")
    encrypted = get_encrypted_element(bytes(body))
    verify_signature(signature=msg_signature, timestamp=timestamp, nonce=nonce, encrypted=encrypted)
    fields = parse_message(decrypt_message(encrypted))
    msg_type = fields.get("MsgType", "").lower()
    if msg_type not in {"text", "voice"}:
        return Response(content="success", media_type="text/plain")

    wecom_userid = fields["FromUserName"]
    message_id = fields["MsgId"]
    if len(message_id) > 128 or len(wecom_userid) > 128:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid WeCom message identifiers")
    content = fields.get("Content", "") if msg_type == "text" else ""
    if len(content) > settings.wecom_max_text_chars:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="WeCom message too long")
    media_id = fields.get("MediaId") if msg_type == "voice" else None
    if msg_type == "voice" and not media_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="WeCom voice message has no MediaId")

    tenant_id = settings.we_com_tenant_id
    try:
        user, _principal = await resolve_verified_wecom_user(
            session, tenant_id=tenant_id, wecom_userid=wecom_userid
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            # 发送者身份有效但未绑定本地用户，属于永久性授权失败。
            # 返回确认，避免企业微信对同一条未授权消息持续重试。
            return Response(content="success", media_type="text/plain")
        raise
    # 身份解析器在读取实时权限时开启了只读事务，此处先提交结束该事务。
    await session.commit()
    # 冲突时不重复插入，使企业微信重试继续使用原有持久化记录。
    async with session.begin():
        await set_tenant_rls_context(session, tenant_id=tenant_id, subject_id=user.id)
        new_id = await session.scalar(
            insert(WeComInboundMessage)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                user_id=user.id,
                wecom_userid=wecom_userid,
                wecom_msg_id=message_id,
                msg_type=msg_type,
                content=content,
                media_id=media_id,
                state="queued",
            )
            .on_conflict_do_nothing(constraint="uq_wecom_inbound_msg")
            .returning(WeComInboundMessage.id)
        )
        inbound = await session.scalar(
            select(WeComInboundMessage)
            .where(WeComInboundMessage.tenant_id == tenant_id, WeComInboundMessage.wecom_msg_id == message_id)
            .with_for_update()
        )
        if inbound is None or inbound.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="WeCom message id collision")

    if new_id is not None:
        try:
            await asyncio.wait_for(
                request.app.state.inbound_publisher.publish(tenant_id=str(tenant_id), inbound_id=str(new_id)),
                timeout=settings.rabbitmq_publish_timeout_seconds,
            )
        except Exception:
            # 数据库提交后即为可恢复任务，通知失败不让企微重复投递。
            logging.getLogger("sagtrag.rag").warning("RAG notification unavailable; database scan will recover")
    return Response(content="success", media_type="text/plain")

