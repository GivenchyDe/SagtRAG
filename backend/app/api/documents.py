"""管理员文档上传、列表与软删除接口。

文档统一进入租户唯一的默认知识库，前端不再提交知识库 ID。
列表按文档聚合最新修订和最新摄取任务，确保排队中或失败的文档也能完整展示。
"""

import hashlib
import re
from pathlib import PurePath
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import CurrentAdmin
from app.config import get_settings
from app.db import get_db, set_tenant_rls_context
from app.ingestion import CHUNKER_VERSION, PARSER_VERSION, DocumentInputError, validate_document_bytes
from app.knowledge_bases import resolve_default_knowledge_base
from app.models import AdminAuditEvent, Document, DocumentRevision, IndexVersion, IngestionJob

router = APIRouter(prefix="/api/v1/admin", tags=["documents"])

PROCESSING_STATUSES = frozenset({"queued", "validating", "parsing", "chunking", "embedding", "indexing"})


def serialize_document(
    document: Document,
    revision: DocumentRevision | None,
    job: IngestionJob | None,
) -> dict[str, object]:
    """把文档、最新修订和最新任务投影为列表卡片所需的稳定结构。"""
    ingestion_status = job.status if job is not None else "queued"
    return {
        "id": str(document.id),
        "title": document.title,
        "filename": revision.filename if revision is not None else None,
        "clearance_level": document.clearance_level,
        "byte_size": revision.byte_size if revision is not None else None,
        "uploaded_at": document.created_at,
        "updated_at": document.updated_at,
        "ingestion_status": ingestion_status,
        "error_summary": job.error_summary if job is not None and ingestion_status == "failed" else None,
        "job_id": str(job.id) if job is not None else None,
    }


async def load_latest_metadata(
    session: AsyncSession,
    tenant_id: UUID,
    documents: list[Document],
) -> tuple[dict[UUID, DocumentRevision], dict[UUID, IngestionJob]]:
    """一次取回这批文档的最新修订与最新摄取任务，避免逐条查询。"""
    if not documents:
        return {}, {}
    document_ids = [document.id for document in documents]
    revisions = await session.scalars(
        select(DocumentRevision)
        .where(DocumentRevision.tenant_id == tenant_id, DocumentRevision.document_id.in_(document_ids))
        .order_by(DocumentRevision.revision_number.asc())
    )
    revision_map = {revision.document_id: revision for revision in revisions.all()}
    jobs = await session.scalars(
        select(IngestionJob)
        .where(IngestionJob.tenant_id == tenant_id, IngestionJob.document_id.in_(document_ids))
        .order_by(IngestionJob.created_at.asc())
    )
    job_map = {job.document_id: job for job in jobs.all()}
    return revision_map, job_map


async def find_duplicate_job(
    session: AsyncSession,
    tenant_id: UUID,
    kb_id: UUID,
    digest: str,
) -> IngestionJob | None:
    """查找同租户同知识库中仍有效的相同内容任务，供重复上传复用。"""
    return await session.scalar(
        select(IngestionJob)
        .join(
            Document,
            and_(
                Document.tenant_id == IngestionJob.tenant_id,
                Document.id == IngestionJob.document_id,
            ),
        )
        .join(
            DocumentRevision,
            and_(
                DocumentRevision.tenant_id == IngestionJob.tenant_id,
                DocumentRevision.id == IngestionJob.revision_id,
            ),
        )
        .where(
            IngestionJob.tenant_id == tenant_id,
            Document.kb_id == kb_id,
            Document.status == "active",
            DocumentRevision.sha256 == digest,
        )
        .order_by(IngestionJob.created_at.desc())
        .limit(1)
    )


@router.get("/documents")
async def list_documents(
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 12,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: Literal["uploaded_at", "clearance_level"] = "uploaded_at",
    order: Literal["asc", "desc"] = "desc",
    q: Annotated[str | None, Query(max_length=100)] = None,
    clearance_level: Annotated[int | None, Query(ge=0, le=3)] = None,
) -> dict[str, object]:
    """分页返回未删除文档，支持名称/文件名搜索、密级筛选与时间排序。"""
    order_column = Document.created_at if sort == "uploaded_at" else Document.clearance_level
    ordering = order_column.asc() if order == "asc" else order_column.desc()
    conditions = [Document.tenant_id == admin.tenant_id, Document.status == "active"]
    if clearance_level is not None:
        conditions.append(Document.clearance_level == clearance_level)
    keyword = (q or "").strip()
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                Document.title.ilike(pattern),
                exists(
                    select(1).where(
                        DocumentRevision.tenant_id == admin.tenant_id,
                        DocumentRevision.document_id == Document.id,
                        DocumentRevision.filename.ilike(pattern),
                    )
                ),
            )
        )
    total = await session.scalar(select(func.count()).select_from(Document).where(*conditions))
    documents = list(
        (
            await session.scalars(
                select(Document)
                .where(*conditions)
                .order_by(ordering, Document.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    revision_map, job_map = await load_latest_metadata(session, admin.tenant_id, documents)
    return {
        "items": [
            serialize_document(document, revision_map.get(document.id), job_map.get(document.id))
            for document in documents
        ],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "order": order,
        "q": keyword or None,
        "clearance_level": clearance_level,
    }


@router.post("/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    request: Request,
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
    file: Annotated[UploadFile, File()],
    clearance_level: Annotated[int, Form(ge=0, le=3)] = 0,
    title: Annotated[str | None, Form(max_length=500)] = None,
) -> dict[str, object]:
    """校验文件后写入 S3，并在默认知识库中原子创建文档修订与摄取任务。"""
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if not filename or len(filename) > 512 or re.search(r"[\x00-\x1f]", filename):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename")
    settings = get_settings()
    content = await file.read(settings.max_upload_bytes + 1)
    try:
        suffix = validate_document_bytes(filename, file.content_type or "", content, settings.max_upload_bytes)
    except DocumentInputError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    knowledge_base = await resolve_default_knowledge_base(session, admin.tenant_id)
    # 先取出标识：失败回滚后 ORM 对象会失效，后续补偿逻辑只依赖这个值。
    kb_id = knowledge_base.id
    digest = hashlib.sha256(content).hexdigest()
    duplicate = await find_duplicate_job(session, admin.tenant_id, kb_id, digest)
    if duplicate is not None:
        await session.commit()
        return {
            "document_id": str(duplicate.document_id),
            "job_id": str(duplicate.id),
            "status": duplicate.status,
            "duplicate": True,
        }

    document_id, revision_id, job_id = uuid4(), uuid4(), uuid4()
    object_key = (
        f"tenant/{admin.tenant_id}/kb/{kb_id}/documents/{document_id}/{revision_id}/source{suffix}"
    )
    # 软删除保留历史任务；再次上传同一文件时递增序号，避免与历史幂等键冲突。
    key_prefix = f"{kb_id}:{digest}:"
    previous_attempts = await session.scalar(
        select(func.count())
        .select_from(IngestionJob)
        .where(
            IngestionJob.tenant_id == admin.tenant_id,
            IngestionJob.idempotency_key.like(f"{key_prefix}%"),
        )
    )
    idempotency_key = f"{key_prefix}{(previous_attempts or 0) + 1}"

    object_store = request.app.state.object_store
    try:
        await object_store.put(object_key, content, file.content_type or "application/octet-stream")
        document = Document(
            id=document_id,
            tenant_id=admin.tenant_id,
            kb_id=kb_id,
            title=(title or PurePath(filename).stem)[:500],
            clearance_level=clearance_level,
            created_by_admin_id=admin.id,
        )
        session.add(document)
        await session.flush()
        session.add(
            DocumentRevision(
                id=revision_id,
                tenant_id=admin.tenant_id,
                document_id=document_id,
                revision_number=1,
                filename=filename,
                media_type=file.content_type or "application/octet-stream",
                byte_size=len(content),
                sha256=digest,
                object_key=object_key,
                parser_version=PARSER_VERSION,
                chunking_config={"version": CHUNKER_VERSION, "size": 1200, "overlap": 150},
                clearance_level=clearance_level,
                created_by_admin_id=admin.id,
            )
        )
        session.add(
            IngestionJob(
                id=job_id,
                tenant_id=admin.tenant_id,
                document_id=document_id,
                revision_id=revision_id,
                idempotency_key=idempotency_key,
                status="queued",
            )
        )
        session.add(
            AdminAuditEvent(
                tenant_id=admin.tenant_id,
                admin_user_id=admin.id,
                action="document.uploaded",
                details={
                    "document_id": str(document_id),
                    "kb_id": str(kb_id),
                    "title": document.title,
                    "sha256": digest,
                },
            )
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        try:
            await object_store.delete(object_key)
        except Exception:
            pass
        if isinstance(exc, IntegrityError):
            await set_tenant_rls_context(session, tenant_id=admin.tenant_id, subject_id=admin.id)
            duplicate = await find_duplicate_job(session, admin.tenant_id, kb_id, digest)
            if duplicate is not None:
                await session.commit()
                return {
                    "document_id": str(duplicate.document_id),
                    "job_id": str(duplicate.id),
                    "status": duplicate.status,
                    "duplicate": True,
                }
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Document could not be queued") from None
    publisher = getattr(request.app.state, "ingestion_publisher", None)
    if publisher is not None:
        try:
            await publisher.publish(tenant_id=str(admin.tenant_id), job_id=str(job_id))
        except Exception:
            # PostgreSQL 保留 queued 状态，Worker 的定时扫描会继续领取任务。
            pass
    return {"document_id": str(document_id), "job_id": str(job_id), "status": "queued", "duplicate": False}


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: UUID,
    admin: CurrentAdmin,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """软删除文档、停用其索引并审计；原件保留供保留策略清理。"""
    document = await session.scalar(
        select(Document)
        .where(Document.tenant_id == admin.tenant_id, Document.id == document_id)
        .with_for_update()
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if document.status == "deleted":
        return
    document.status = "deleted"
    versions = await session.scalars(
        select(IndexVersion)
        .where(IndexVersion.tenant_id == admin.tenant_id, IndexVersion.document_id == document_id)
        .with_for_update()
    )
    for version in versions:
        if version.status in {"active", "ready", "building"}:
            version.status = "retired"
    jobs = await session.scalars(
        select(IngestionJob)
        .where(
            IngestionJob.tenant_id == admin.tenant_id,
            IngestionJob.document_id == document_id,
            IngestionJob.status.in_(PROCESSING_STATUSES),
        )
        .with_for_update()
    )
    for job in jobs:
        job.status = "failed"
        job.error_code = "document_deleted"
        job.error_summary = "文档已删除，摄取任务已停止"
        job.worker_id = None
        job.lease_expires_at = None
    session.add(
        AdminAuditEvent(
            tenant_id=admin.tenant_id,
            admin_user_id=admin.id,
            action="document.deleted",
            details={"document_id": str(document_id), "title": document.title},
        )
    )
    await session.commit()
