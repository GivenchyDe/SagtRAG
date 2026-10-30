from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import decode_access_token
from app.db import get_db, set_tenant_rls_context
from app.models import User, UserStatus
from app.permissions import Principal, load_principal


async def get_current_principal(
    session: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """验证 Bearer 会话，并根据数据库中的实时记录重新构建权限。"""
    token = None
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization header")
        token = value
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    user_id, tenant_id = decode_access_token(token)
    await set_tenant_rls_context(session, tenant_id=tenant_id, subject_id=user_id)
    user = await session.scalar(
        select(User).where(User.id == user_id, User.tenant_id == tenant_id)
    )
    if user is None or user.status != UserStatus.ACTIVE or user.wecom_userid is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is inactive")

    # 角色、密级和用户组始终从 PostgreSQL 重新读取，令牌声明本身不授予访问权限。
    # 因此权限变更无需等待 JWT 到期即可生效。
    principal = await load_principal(session, user)
    await set_tenant_rls_context(
        session,
        tenant_id=tenant_id,
        subject_id=user_id,
        clearance_level=principal.clearance_level,
    )
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]

