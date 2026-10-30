"""员工资料与管理员账号管理接口。

员工没有“删除账号”语义：删除动作等于解除企微绑定，保留资料与历史记录。
管理员采用软删除，用户名在未删除账号范围内唯一，旧令牌因数据库校验立即失效。
"""

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid4

from argon2 import PasswordHasher, Type
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Text, and_, cast, func, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import CurrentAdmin
from app.db import get_db
from app.models import AdminAuditEvent, AdminUser, Document, User, UserStatus
from app.schemas import (
    CreateAdminUserRequest,
    CreateEmployeeRequest,
    UpdateAdminUserRequest,
    UpdateEmployeeRequest,
    WeComBindingRequest,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin-management"])
password_hasher = PasswordHasher(type=Type.ID)

ADMIN_MANAGEMENT_ACTIONS = (
    "admin.created",
    "admin.deleted",
    "admin.status_updated",
    "admin.password_changed",
    "admin.bootstrap_created",
)

# 与前端展示一致的中文操作名，供日志搜索按“操作”关键字匹配内部动作码。
AUDIT_ACTION_LABELS = {
    "admin.login_succeeded": "登录成功",
    "admin.login_failed": "登录失败",
    "admin.created": "创建管理员",
    "admin.deleted": "删除管理员",
    "admin.status_updated": "启用/停用管理员",
    "admin.password_changed": "修改密码",
    "admin.bootstrap_created": "初始化管理员",
    "document.uploaded": "上传文档",
    "document.deleted": "删除文档",
    "user.created": "新增员工",
    "user.updated": "编辑员工",
    "user.wecom_bound": "绑定企微",
    "user.wecom_unbound": "解除企微绑定",
    "user.deleted": "删除员工",
    "qa.detail_viewed": "查看问答详情",
    "knowledge_base.created": "创建知识库",
}


async def _lock_tenant(session: AsyncSession, tenant_id: UUID) -> None:
    """串行化同一租户的管理员增删，避免并发操作移除最后一名活跃管理员。"""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant_id, 0))"),
        {"tenant_id": str(tenant_id)},
    )


async def _count_other_active_admins(session: AsyncSession, tenant_id: UUID, admin_user_id: UUID) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(AdminUser)
        .where(
            AdminUser.tenant_id == tenant_id,
            AdminUser.deleted_at.is_(None),
            AdminUser.status == "active",
            AdminUser.id != admin_user_id,
        )
    )
    return count or 0


def _serialize_employee(user: User) -> dict[str, object]:
    return {
        "id": str(user.id),
        "display_name": user.display_name,
        "wecom_userid": user.wecom_userid,
        "clearance_level": user.clearance_level,
        "status": user.status.value,
        "is_bound": user.wecom_userid is not None,
        "created_at": user.created_at,
    }


@router.get("/users")
async def list_users(
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(max_length=100)] = None,
    clearance_level: Annotated[int | None, Query(ge=0, le=3)] = None,
    order: Literal["asc", "desc"] = "desc",
) -> dict[str, object]:
    """分页列出租户内本地员工，支持姓名/企微 userid 搜索、密级筛选与添加时间排序。"""
    conditions = [User.tenant_id == admin.tenant_id, User.deleted_at.is_(None)]
    if clearance_level is not None:
        conditions.append(User.clearance_level == clearance_level)
    keyword = (q or "").strip()
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(User.display_name.ilike(pattern), User.wecom_userid.ilike(pattern)))
    total = await session.scalar(select(func.count()).select_from(User).where(*conditions))
    ordering = User.created_at.asc() if order == "asc" else User.created_at.desc()
    rows = await session.scalars(
        select(User)
        .where(*conditions)
        .order_by(ordering, User.id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [_serialize_employee(user) for user in rows.all()],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
        "q": keyword or None,
        "clearance_level": clearance_level,
        "order": order,
    }


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_employee(
    body: CreateEmployeeRequest,
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    """创建本地员工；本地标识由服务端生成，可选同时绑定企微 userid。"""
    user = User(
        tenant_id=admin.tenant_id,
        external_id=f"local-{uuid4().hex}",
        display_name=body.display_name,
        clearance_level=body.clearance_level,
        wecom_userid=body.wecom_userid or None,
        status=UserStatus.ACTIVE,
    )
    try:
        session.add(user)
        await session.flush()
        session.add(
            AdminAuditEvent(
                tenant_id=admin.tenant_id,
                admin_user_id=admin.id,
                action="user.created",
                details={"user_id": str(user.id), "display_name": user.display_name, "clearance_level": body.clearance_level},
            )
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="WeCom userid is already bound") from exc
    return _serialize_employee(user)


@router.patch("/users/{user_id}")
async def update_employee(
    user_id: UUID,
    body: UpdateEmployeeRequest,
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    """编辑员工姓名、密级和可选的企微 userid；密级变化时递增权限版本。"""
    user = await session.scalar(
        select(User)
        .where(User.tenant_id == admin.tenant_id, User.id == user_id, User.deleted_at.is_(None))
        .with_for_update()
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    changed_clearance = user.clearance_level != body.clearance_level
    changed_binding = body.wecom_userid is not None and (body.wecom_userid or None) != user.wecom_userid
    user.display_name = body.display_name
    user.clearance_level = body.clearance_level
    if body.wecom_userid is not None:
        user.wecom_userid = body.wecom_userid or None
    if changed_clearance or changed_binding:
        user.permission_version += 1
    session.add(
        AdminAuditEvent(
            tenant_id=admin.tenant_id,
            admin_user_id=admin.id,
            action="user.updated",
            details={
                "user_id": str(user.id),
                "display_name": user.display_name,
                "clearance_level": body.clearance_level,
                "binding_changed": changed_binding,
            },
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="WeCom userid is already bound") from exc
    return _serialize_employee(user)


@router.put("/users/{user_id}/binding", status_code=status.HTTP_204_NO_CONTENT)
async def bind_wecom_user(
    user_id: UUID,
    body: WeComBindingRequest,
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """绑定员工的企微 userid；该标识本身不授予任何 ACL 权限。"""
    user = await session.scalar(
        select(User)
        .where(User.tenant_id == admin.tenant_id, User.id == user_id, User.deleted_at.is_(None))
        .with_for_update()
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.wecom_userid = body.wecom_userid
    user.permission_version += 1
    session.add(
        AdminAuditEvent(
            tenant_id=admin.tenant_id,
            admin_user_id=admin.id,
            action="user.wecom_bound",
            details={"user_id": str(user.id), "display_name": user.display_name},
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="WeCom userid is already bound") from exc


@router.delete("/users/{user_id}/binding", status_code=status.HTTP_204_NO_CONTENT)
async def unbind_wecom_user(
    user_id: UUID,
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """解除员工企微绑定并递增权限版本；员工资料保留，删除员工请使用删除接口。"""
    user = await session.scalar(
        select(User)
        .where(User.tenant_id == admin.tenant_id, User.id == user_id, User.deleted_at.is_(None))
        .with_for_update()
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.wecom_userid is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="WeCom binding is already removed")
    user.wecom_userid = None
    user.permission_version += 1
    session.add(
        AdminAuditEvent(
            tenant_id=admin.tenant_id,
            admin_user_id=admin.id,
            action="user.wecom_unbound",
            details={"user_id": str(user.id), "display_name": user.display_name},
        )
    )
    await session.commit()


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_employee(
    user_id: UUID,
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """软删除员工：列表移除、企微身份立即失效，用户行与历史审计保留。"""
    user = await session.scalar(
        select(User)
        .where(User.tenant_id == admin.tenant_id, User.id == user_id, User.deleted_at.is_(None))
        .with_for_update()
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.deleted_at = datetime.now(timezone.utc)
    user.wecom_userid = None
    user.status = UserStatus.DISABLED
    user.permission_version += 1
    session.add(
        AdminAuditEvent(
            tenant_id=admin.tenant_id,
            admin_user_id=admin.id,
            action="user.deleted",
            details={"user_id": str(user.id), "display_name": user.display_name},
        )
    )
    await session.commit()


@router.get("/admin-users")
async def list_admin_users(
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=128)] = None,
    status: Literal["active", "disabled"] | None = None,
    order: Literal["asc", "desc"] = "desc",
) -> list[dict[str, object]]:
    """列出未删除的后台账号，支持账号名搜索、状态筛选与创建时间排序。"""
    conditions = [AdminUser.tenant_id == admin.tenant_id, AdminUser.deleted_at.is_(None)]
    keyword = (q or "").strip()
    if keyword:
        conditions.append(AdminUser.username.ilike(f"%{keyword}%"))
    if status is not None:
        conditions.append(AdminUser.status == status)
    ordering = AdminUser.created_at.asc() if order == "asc" else AdminUser.created_at.desc()
    rows = await session.scalars(
        select(AdminUser)
        .where(*conditions)
        .order_by(ordering, AdminUser.id)
    )
    return [
        {
            "id": str(item.id),
            "username": item.username,
            "status": item.status,
            "must_change_password": item.must_change_password,
            "created_at": item.created_at,
            "is_current": item.id == admin.id,
        }
        for item in rows.all()
    ]


@router.post("/admin-users", status_code=status.HTTP_201_CREATED)
async def create_admin_user(
    body: CreateAdminUserRequest,
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """创建统一最高权限的后台账号，仅保存 Argon2id 哈希并要求首次改密。"""
    new_admin = AdminUser(
        tenant_id=admin.tenant_id,
        username=body.username,
        password_hash=password_hasher.hash(body.initial_password),
        role="tenant_admin",
        must_change_password=True,
    )
    try:
        session.add(new_admin)
        await session.flush()
        session.add(
            AdminAuditEvent(
                tenant_id=admin.tenant_id,
                admin_user_id=admin.id,
                action="admin.created",
                details={"admin_user_id": str(new_admin.id), "username": new_admin.username},
            )
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Administrator username already exists") from exc
    return {"id": str(new_admin.id), "username": new_admin.username}


@router.patch("/admin-users/{admin_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def update_admin_user(
    admin_user_id: UUID,
    body: UpdateAdminUserRequest,
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """启用或停用后台账号；禁止停用当前账号和最后一个活跃管理员。"""
    await _lock_tenant(session, admin.tenant_id)
    target = await session.scalar(
        select(AdminUser)
        .where(
            AdminUser.tenant_id == admin.tenant_id,
            AdminUser.id == admin_user_id,
            AdminUser.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Administrator not found")
    if body.status == "disabled":
        if target.id == admin.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cannot disable the current administrator")
        if target.status == "active" and await _count_other_active_admins(session, admin.tenant_id, target.id) == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="At least one active administrator is required",
            )
    if target.status != body.status:
        target.status = body.status
        session.add(
            AdminAuditEvent(
                tenant_id=admin.tenant_id,
                admin_user_id=admin.id,
                action="admin.status_updated",
                details={"admin_user_id": str(target.id), "username": target.username, "status": body.status},
            )
        )
    await session.commit()


@router.delete("/admin-users/{admin_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin_user(
    admin_user_id: UUID,
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """软删除后台账号：列表隐藏、禁止登录、旧令牌失效、审计保留。"""
    await _lock_tenant(session, admin.tenant_id)
    target = await session.scalar(
        select(AdminUser)
        .where(
            AdminUser.tenant_id == admin.tenant_id,
            AdminUser.id == admin_user_id,
            AdminUser.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Administrator not found")
    if target.id == admin.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cannot delete the current administrator")
    if target.status == "active" and await _count_other_active_admins(session, admin.tenant_id, target.id) == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="At least one active administrator is required",
        )
    target.status = "disabled"
    target.deleted_at = datetime.now(timezone.utc)
    session.add(
        AdminAuditEvent(
            tenant_id=admin.tenant_id,
            admin_user_id=admin.id,
            action="admin.deleted",
            details={"admin_user_id": str(target.id), "username": target.username},
        )
    )
    await session.commit()


@router.get("/audit-events")
async def list_admin_audit_events(
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    category: Literal["login", "admin", "user", "document", "qa"] | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> dict[str, object]:
    """分页返回租户范围的管理操作审计记录，可按类别或关键字筛选。

    关键字同时匹配中文操作名、操作人账号、对象信息（事件详情）与内部动作码。
    """
    conditions = [AdminAuditEvent.tenant_id == admin.tenant_id]
    if category == "login":
        conditions.append(AdminAuditEvent.action.startswith("admin.login"))
    elif category == "admin":
        conditions.append(AdminAuditEvent.action.in_(ADMIN_MANAGEMENT_ACTIONS))
    elif category == "user":
        conditions.append(AdminAuditEvent.action.startswith("user."))
    elif category == "document":
        conditions.append(AdminAuditEvent.action.startswith("document."))
    elif category == "qa":
        conditions.append(AdminAuditEvent.action.startswith("qa."))

    keyword = (q or "").strip()
    if keyword:
        pattern = f"%{keyword}%"
        matched_actions = [code for code, label in AUDIT_ACTION_LABELS.items() if keyword in label]
        details_json = cast(AdminAuditEvent.details, JSONB)
        matches = [
            AdminAuditEvent.action.ilike(pattern),
            AdminUser.username.ilike(pattern),
        ]
        # 详情里的账号名、姓名、标题等直接匹配（转 JSONB 后中文不再被转义）。
        for key in ("username", "display_name", "title", "filename"):
            matches.append(details_json.op("->>")(key).ilike(pattern))
        # 旧记录可能只存了对象 ID，用当前员工/文档/管理员名称反查对应 ID。
        matched_user_ids = select(cast(User.id, Text)).where(
            User.tenant_id == admin.tenant_id, User.display_name.ilike(pattern)
        )
        matched_document_ids = select(cast(Document.id, Text)).where(
            Document.tenant_id == admin.tenant_id, Document.title.ilike(pattern)
        )
        matched_admin_ids = select(cast(AdminUser.id, Text)).where(
            AdminUser.tenant_id == admin.tenant_id, AdminUser.username.ilike(pattern)
        )
        matches.extend(
            [
                details_json.op("->>")("user_id").in_(matched_user_ids),
                details_json.op("->>")("document_id").in_(matched_document_ids),
                details_json.op("->>")("admin_user_id").in_(matched_admin_ids),
            ]
        )
        if matched_actions:
            matches.append(AdminAuditEvent.action.in_(matched_actions))
        conditions.append(or_(*matches))

    base_query = select(AdminAuditEvent).outerjoin(
        AdminUser,
        and_(
            AdminUser.tenant_id == AdminAuditEvent.tenant_id,
            AdminUser.id == AdminAuditEvent.admin_user_id,
        ),
    )
    total = await session.scalar(
        select(func.count()).select_from(base_query.where(*conditions).subquery())
    )
    rows = await session.scalars(
        base_query
        .where(*conditions)
        .order_by(AdminAuditEvent.occurred_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "total": total or 0,
        "limit": limit,
        "offset": offset,
        "category": category,
        "q": keyword or None,
        "items": [
            {
                "id": str(item.id),
                "admin_user_id": str(item.admin_user_id) if item.admin_user_id else None,
                "action": item.action,
                "details": item.details,
                "occurred_at": item.occurred_at,
            }
            for item in rows.all()
        ],
    }
