"""管理员 Bearer 令牌校验及租户上下文依赖。"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import decode_access_token
from app.db import get_db, set_tenant_rls_context
from app.models import AdminUser

bearer_scheme = HTTPBearer(auto_error=False)
ADMIN_ROLES = frozenset({"tenant_admin", "document_manager", "auditor"})


async def get_current_admin(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUser:
    """验证管理员 Bearer JWT，从数据库核对账号和角色并设置 RLS 租户上下文。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    admin_id, tenant_id = decode_access_token(credentials.credentials, expected_type="admin")
    await set_tenant_rls_context(session, tenant_id=tenant_id, subject_id=admin_id)
    admin = await session.scalar(
        select(AdminUser).where(
            AdminUser.id == admin_id,
            AdminUser.tenant_id == tenant_id,
            AdminUser.deleted_at.is_(None),
        )
    )
    if admin is None or admin.status != "active" or admin.role not in ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Administrator account is inactive")
    if admin.must_change_password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Password change required")
    return admin


CurrentAdmin = Annotated[AdminUser, Depends(get_current_admin)]


async def get_admin_during_password_setup(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUser:
    """只在首次改密阶段允许访问账号资料和改密接口。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    admin_id, tenant_id = decode_access_token(credentials.credentials, expected_type="admin")
    await set_tenant_rls_context(session, tenant_id=tenant_id, subject_id=admin_id)
    admin = await session.scalar(
        select(AdminUser).where(
            AdminUser.id == admin_id,
            AdminUser.tenant_id == tenant_id,
            AdminUser.deleted_at.is_(None),
        )
    )
    if admin is None or admin.status != "active" or admin.role not in ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Administrator account is inactive")
    return admin


AdminPasswordSetupPrincipal = Annotated[AdminUser, Depends(get_admin_during_password_setup)]
