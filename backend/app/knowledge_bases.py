"""租户唯一总知识库的解析与初始化。

管理后台不再让用户选择知识库，所有文档都进入租户唯一的默认知识库。
解析过程在租户级 advisory lock 内执行，避免并发首次上传创建出两个总知识库。
"""

from uuid import UUID

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, KnowledgeBase

DEFAULT_KNOWLEDGE_BASE_NAME = "总知识库"


async def resolve_default_knowledge_base(session: AsyncSession, tenant_id: UUID) -> KnowledgeBase:
    """返回租户唯一总知识库，不存在时在当前事务内初始化。

    ``pg_advisory_xact_lock`` 只在当前事务内生效，调用方提交或回滚后自动释放。
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"default-knowledge-base:{tenant_id}"},
    )
    knowledge_base = await session.scalar(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.is_default.is_(True),
        )
        .with_for_update()
    )
    if knowledge_base is not None:
        # 默认知识库被手工归档时恢复为启用，保证上传始终有落点。
        if knowledge_base.status != "active":
            knowledge_base.status = "active"
        return knowledge_base

    adopted = await session.scalar(
        select(KnowledgeBase)
        .join(
            Document,
            and_(
                Document.tenant_id == KnowledgeBase.tenant_id,
                Document.kb_id == KnowledgeBase.id,
                Document.status == "active",
            ),
            isouter=True,
        )
        .where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.status == "active",
            KnowledgeBase.is_default.is_(False),
        )
        .group_by(KnowledgeBase.id)
        .order_by(func.count(Document.id).desc(), KnowledgeBase.created_at.asc())
        .limit(1)
    )
    if adopted is not None:
        adopted.is_default = True
        return adopted

    knowledge_base = KnowledgeBase(
        tenant_id=tenant_id,
        name=DEFAULT_KNOWLEDGE_BASE_NAME,
        is_default=True,
    )
    session.add(knowledge_base)
    await session.flush()
    return knowledge_base
