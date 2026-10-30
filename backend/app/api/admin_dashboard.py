"""首页聚合接口：文档与员工总数、最近文档和入库失败提醒。"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.documents import load_latest_metadata, serialize_document
from app.auth.admin import CurrentAdmin
from app.db import get_db
from app.models import Document, IngestionJob, User

router = APIRouter(prefix="/api/v1/admin", tags=["dashboard"])

RECENT_DOCUMENT_LIMIT = 5


async def count_failed_ingestion(session: AsyncSession, tenant_id) -> int:
    """统计当前有效文档中“最新一次摄取任务失败”的数量。"""
    latest_jobs = (
        select(
            IngestionJob.document_id.label("document_id"),
            IngestionJob.status.label("status"),
            func.row_number()
            .over(partition_by=IngestionJob.document_id, order_by=IngestionJob.created_at.desc())
            .label("position"),
        )
        .where(IngestionJob.tenant_id == tenant_id)
        .subquery()
    )
    failed = await session.scalar(
        select(func.count())
        .select_from(latest_jobs)
        .join(
            Document,
            and_(
                Document.tenant_id == tenant_id,
                Document.id == latest_jobs.c.document_id,
                Document.status == "active",
            ),
        )
        .where(latest_jobs.c.position == 1, latest_jobs.c.status == "failed")
    )
    return failed or 0


@router.get("/dashboard")
async def get_dashboard(
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    """返回首页所需的真实数据，不包含装饰性趋势或管理员数量。"""
    document_total = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.tenant_id == admin.tenant_id, Document.status == "active")
    )
    # 员工总数统计本地登记的全部员工，包含尚未绑定企微的账号，不含已删除员工。
    employee_total = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.tenant_id == admin.tenant_id, User.deleted_at.is_(None))
    )
    recent_documents = list(
        (
            await session.scalars(
                select(Document)
                .where(Document.tenant_id == admin.tenant_id, Document.status == "active")
                .order_by(Document.updated_at.desc(), Document.id)
                .limit(RECENT_DOCUMENT_LIMIT)
            )
        ).all()
    )
    revision_map, job_map = await load_latest_metadata(session, admin.tenant_id, recent_documents)
    recent = [
        {
            "id": item["id"],
            "title": item["title"],
            "clearance_level": item["clearance_level"],
            "ingestion_status": item["ingestion_status"],
            "updated_at": item["updated_at"],
        }
        for item in (
            serialize_document(document, revision_map.get(document.id), job_map.get(document.id))
            for document in recent_documents
        )
    ]
    return {
        "document_total": document_total or 0,
        "employee_total": employee_total or 0,
        "recent_documents": recent,
        "failed_ingestion_count": await count_failed_ingestion(session, admin.tenant_id),
    }
