"""员工访问主体的构建与 RAG 候选生成的统一访问条件。

后台重构后不再按用户组或知识库成员授权做业务判定。员工能否访问内容只看：
有效企微绑定、账号启用、同租户，以及员工密级不低于文档密级。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TenantRole, User, UserStatus


@dataclass(frozen=True, slots=True)
class Principal:
    """请求主体及从 PostgreSQL 实时读取的授权属性。"""
    user_id: UUID
    tenant_id: UUID
    clearance_level: int
    permission_version: int
    tenant_role: TenantRole | None


async def load_principal(session: AsyncSession, user: User) -> Principal:
    """构建请求主体；员工密级直接使用个人配置，用户组不再提高有效密级。"""
    return Principal(
        user_id=user.id,
        tenant_id=user.tenant_id,
        clearance_level=user.clearance_level,
        permission_version=user.permission_version,
        tenant_role=user.tenant_role,
    )


def document_access_predicate(document_model: type, principal: Principal):
    """返回文档或分块检索必须使用的 SQL 访问控制条件。

    必须在生成候选结果和执行 RRF 之前，将此条件加入向量检索和全文检索查询。
    检索或生成完成后再过滤，无法防止越权信息泄露。密级从数据库记录读取，不能信任令牌数据。
    """
    active_user = exists(
        select(1).where(
            User.id == principal.user_id,
            User.tenant_id == principal.tenant_id,
            User.status == UserStatus.ACTIVE,
            User.wecom_userid.is_not(None),
            User.deleted_at.is_(None),
        )
    )
    user_clearance = (
        select(User.clearance_level)
        .where(
            User.id == principal.user_id,
            User.tenant_id == principal.tenant_id,
            User.status == UserStatus.ACTIVE,
            User.deleted_at.is_(None),
        )
        .scalar_subquery()
    )
    return and_(
        document_model.tenant_id == principal.tenant_id,
        document_model.clearance_level <= user_clearance,
        active_user,
    )
