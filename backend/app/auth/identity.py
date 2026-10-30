from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import set_tenant_rls_context
from app.models import User, UserStatus
from app.permissions import Principal, load_principal


async def resolve_verified_wecom_user(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    wecom_userid: str,
) -> tuple[User, Principal]:
    """将已验证的企业微信身份解析为已预先绑定且处于启用状态的本地用户。

    调用方必须先校验企业微信签名、解密载荷并验证 CorpID/AgentID。
    本函数不会把客户端提供的 userid 当作身份凭证。
    """
    await set_tenant_rls_context(session, tenant_id=tenant_id)
    user = await session.scalar(
        select(User).where(
            User.tenant_id == tenant_id,
            User.wecom_userid == wecom_userid,
            User.status == UserStatus.ACTIVE,
            User.deleted_at.is_(None),
        )
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="WeCom identity is not bound to an active SagtRAG user",
        )

    await set_tenant_rls_context(session, tenant_id=tenant_id, subject_id=user.id)
    principal = await load_principal(session, user)
    await set_tenant_rls_context(
        session,
        tenant_id=tenant_id,
        subject_id=user.id,
        clearance_level=principal.clearance_level,
    )
    return user, principal

