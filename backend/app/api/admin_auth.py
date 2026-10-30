"""管理员账号初始化、密码登录与管理会话接口。"""

from datetime import datetime, timezone
from typing import Annotated

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.tokens import create_access_token
from app.auth.admin import AdminPasswordSetupPrincipal, CurrentAdmin
from app.config import get_settings
from app.db import get_db, set_tenant_rls_context
from app.models import AdminAuditEvent, AdminUser, Tenant
from app.schemas import AdminLoginRequest, AdminLoginResponse, AdminPasswordChangeRequest, AdminUserResponse

router = APIRouter(prefix="/api/v1/auth/admin", tags=["admin-auth"])
password_hasher = PasswordHasher(type=Type.ID)
DUMMY_PASSWORD_HASH = password_hasher.hash("sagtrag-invalid-login-timing-padding")


@router.get("/me", response_model=AdminUserResponse)
async def get_admin_profile(admin: AdminPasswordSetupPrincipal) -> AdminUserResponse:
    """从当前已验证管理员账号返回角色与首次改密状态。"""
    return AdminUserResponse(
        id=admin.id,
        username=admin.username,
        tenant_id=admin.tenant_id,
        role=admin.role,
        is_first_login=admin.must_change_password,
    )


async def initialize_first_admin(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """当前配置租户尚无后台账号时，创建强制首次改密的本机演示管理员。"""
    settings = get_settings()
    async with session_factory() as session:
        async with session.begin():
            await set_tenant_rls_context(session, tenant_id=settings.we_com_tenant_id)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant_id, 0))"),
                {"tenant_id": str(settings.we_com_tenant_id)},
            )
            # The advisory lock above serializes bootstrap; the runtime role only
            # needs SELECT on tenants, while FOR UPDATE would require UPDATE.
            tenant = await session.scalar(select(Tenant).where(Tenant.id == settings.we_com_tenant_id))
            if tenant is None:
                raise RuntimeError("Configured tenant must be bootstrapped before API startup")
            existing = await session.scalar(
                select(AdminUser.id)
                .where(AdminUser.tenant_id == tenant.id, AdminUser.deleted_at.is_(None))
                .limit(1)
            )
            if existing is not None:
                return
            admin = AdminUser(
                tenant_id=tenant.id,
                username="admin",
                password_hash=password_hasher.hash(settings.admin_bootstrap_password.get_secret_value()),
                role="tenant_admin",
                must_change_password=True,
            )
            session.add(admin)
            await session.flush()
            session.add(
                AdminAuditEvent(
                    tenant_id=tenant.id,
                    admin_user_id=admin.id,
                    action="admin.bootstrap_created",
                    details={"username": "admin", "is_first_login": True},
                )
            )


@router.post("/login", response_model=AdminLoginResponse)
async def admin_login(
    request: AdminLoginRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AdminLoginResponse:
    """校验管理员口令，记录成功或失败审计并签发短期管理员 JWT。"""
    settings = get_settings()
    tenant_id = settings.we_com_tenant_id
    await set_tenant_rls_context(session, tenant_id=tenant_id)
    admin = await session.scalar(
        select(AdminUser).where(
            AdminUser.tenant_id == tenant_id,
            AdminUser.username == request.username,
            AdminUser.deleted_at.is_(None),
        )
    )
    valid_password = False
    candidate_hash = admin.password_hash if admin is not None and admin.status == "active" else DUMMY_PASSWORD_HASH
    try:
        valid_password = password_hasher.verify(candidate_hash, request.password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        valid_password = False
    if not valid_password or admin is None or admin.status != "active":
        session.add(
            AdminAuditEvent(
                tenant_id=tenant_id,
                admin_user_id=None,
                action="admin.login_failed",
                details={"username": request.username[:128]},
            )
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    admin.last_login_at = datetime.now(timezone.utc)
    session.add(
        AdminAuditEvent(
            tenant_id=tenant_id,
            admin_user_id=admin.id,
            action="admin.login_succeeded",
            details={"username": admin.username},
        )
    )
    await session.commit()
    token = create_access_token(
        user_id=admin.id,
        tenant_id=admin.tenant_id,
        permission_version=1,
        token_type="admin",
    )
    return AdminLoginResponse(
        access_token=token,
        user=AdminUserResponse(
            id=admin.id,
            username=admin.username,
            tenant_id=admin.tenant_id,
            role=admin.role,
            is_first_login=admin.must_change_password,
        ),
    )


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_admin_password(
    request: AdminPasswordChangeRequest,
    admin: AdminPasswordSetupPrincipal,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """验证当前口令、更新 Argon2id 哈希并清除首次登录改密标记。"""
    try:
        password_hasher.verify(admin.password_hash, request.current_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect") from exc
    if request.current_password == request.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be different")
    admin.password_hash = password_hasher.hash(request.new_password)
    admin.must_change_password = False
    session.add(
        AdminAuditEvent(
            tenant_id=admin.tenant_id,
            admin_user_id=admin.id,
            action="admin.password_changed",
            details={"first_login": True},
        )
    )
    await session.commit()
