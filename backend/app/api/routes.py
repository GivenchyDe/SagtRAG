"""企微员工作为问答用户的只读自助接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentPrincipal
from app.db import get_db
from app.models import KnowledgeBase, User
from app.schemas import CurrentUserResponse

router = APIRouter(prefix="/api/v1")


@router.get("/me", response_model=CurrentUserResponse)
async def get_me(
    principal: CurrentPrincipal,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUserResponse:
    """返回当前已验证企微员工的实时身份与权限摘要。"""
    user = await session.get(User, principal.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is inactive")
    return CurrentUserResponse.from_principal(user, principal)


@router.get("/knowledge-bases")
async def list_accessible_knowledge_bases(
    principal: CurrentPrincipal,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, str]]:
    """返回租户唯一总知识库；员工访问不再依赖知识库成员授权。"""
    knowledge_base = await session.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.tenant_id == principal.tenant_id,
            KnowledgeBase.is_default.is_(True),
            KnowledgeBase.status == "active",
        )
    )
    if knowledge_base is None:
        return []
    return [{"id": str(knowledge_base.id), "name": knowledge_base.name}]
