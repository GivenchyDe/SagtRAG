from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

# 租户和主体上下文仅在当前事务内有效，避免连接池复用连接时将某个请求的 RLS 身份带到下一个请求。
settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """提供请求级数据库会话；请求处理失败时回滚当前事务。"""
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def set_tenant_rls_context(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    subject_id: UUID | None = None,
    clearance_level: int | None = None,
) -> None:
    """设置事务级 PostgreSQL RLS 上下文；事务由调用方管理。

    ``set_config(..., true)`` 将配置限定在当前事务内。每次提交后或切换已认证主体时，
    调用方都必须重新设置这些值。
    """
    await session.execute(
        text(
            "SELECT set_config('app.tenant_id', :tenant_id, true), "
            "set_config('app.subject_id', :subject_id, true), "
            "set_config('app.clearance_level', :clearance_level, true)"
        ),
        {
            "tenant_id": str(tenant_id),
            "subject_id": str(subject_id) if subject_id else "",
            "clearance_level": str(clearance_level) if clearance_level is not None else "0",
        },
    )

