"""数据库权威的文档摄取 Worker，支持租约回收与失败重试。"""

import asyncio
import json
import logging
import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text, update
import aio_pika

from app.config import get_settings
from app.db import SessionFactory, engine, set_tenant_rls_context
from app.embeddings import embed_texts
from app.ingestion import CHUNKER_VERSION, DocumentInputError, extract_paragraphs, split_into_chunks, validate_document_bytes
from app.integrations import connected_object_store
from app.models import Document, DocumentRevision, IndexVersion, IngestionJob
from app.queueing import INGESTION_QUEUE_NAME

logger = logging.getLogger("sagtrag.ingestion")
ACTIVE_STATES = ("validating", "parsing", "chunking", "embedding", "indexing")


async def claim_job(worker_id: str, requested_job_id: str | None = None) -> tuple[str, str] | None:
    """以 SKIP LOCKED 原子领取新任务或租约已过期任务。"""
    tenant_id = get_settings().we_com_tenant_id
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_rls_context(session, tenant_id=tenant_id)
            await session.execute(
                update(IngestionJob)
                .where(
                    IngestionJob.tenant_id == tenant_id,
                    IngestionJob.attempt_count >= IngestionJob.max_attempts,
                    or_(
                        IngestionJob.status == "queued",
                        IngestionJob.status.in_(ACTIVE_STATES)
                        & (IngestionJob.lease_expires_at < func.now()),
                    ),
                )
                .values(
                    status="failed",
                    worker_id=None,
                    lease_expires_at=None,
                    error_code="attempt_limit_exceeded",
                    error_summary="任务多次中断，已停止自动重试",
                )
            )
            query = select(IngestionJob).where(
                    IngestionJob.tenant_id == tenant_id,
                    or_(
                        (IngestionJob.status == "queued")
                        & (IngestionJob.attempt_count < IngestionJob.max_attempts),
                        IngestionJob.status.in_(ACTIVE_STATES)
                        & (IngestionJob.lease_expires_at < func.now())
                        & (IngestionJob.attempt_count < IngestionJob.max_attempts),
                    ),
                )
            if requested_job_id is not None:
                query = query.where(IngestionJob.id == UUID(requested_job_id))
            job = await session.scalar(query.order_by(IngestionJob.created_at).with_for_update(skip_locked=True).limit(1))
            if job is None:
                return None
            job.status = "validating"
            job.worker_id = worker_id
            job.attempt_count += 1
            job.heartbeat_at = datetime.now(timezone.utc)
            job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            return str(job.id), str(job.tenant_id)


async def _set_stage(job_id: str, tenant_id: str, worker_id: str, stage: str, checkpoint: dict[str, object] | None = None) -> None:
    """更新任务阶段和心跳租约。"""
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_rls_context(session, tenant_id=UUID(tenant_id))
            job = await session.get(IngestionJob, UUID(job_id))
            if job is None or job.worker_id != worker_id:
                raise RuntimeError("ingestion_lease_lost")
            job.status = stage
            job.heartbeat_at = datetime.now(timezone.utc)
            job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            if checkpoint is not None:
                job.checkpoint = checkpoint


async def process_job(job_id: str, tenant_id: str, worker_id: str) -> None:
    """下载原件、解析分块、生成向量并原子激活新索引版本。"""
    tenant_uuid, job_uuid = UUID(tenant_id), UUID(job_id)
    async with SessionFactory() as session:
        await set_tenant_rls_context(session, tenant_id=tenant_uuid)
        job = await session.get(IngestionJob, job_uuid)
        if job is None:
            raise RuntimeError("ingestion_job_missing")
        if job.worker_id != worker_id:
            raise RuntimeError("ingestion_lease_lost")
        revision_id = job.revision_id
        document_id = job.document_id
        revision = await session.get(DocumentRevision, revision_id)
        document = await session.get(Document, document_id)
        if revision is None or document is None or revision.document_id != document.id:
            raise RuntimeError("ingestion_source_missing")
        object_key = revision.object_key
        filename = revision.filename
        media_type = revision.media_type
        max_bytes = get_settings().max_upload_bytes
        clearance = min(revision.clearance_level, document.clearance_level)
        await session.commit()

    await _set_stage(job_id, tenant_id, worker_id, "parsing")
    async with connected_object_store(get_settings()) as store:
        content, _stored_media = await store.get(object_key)
    validate_document_bytes(filename, media_type, content, max_bytes)
    paragraphs = extract_paragraphs(filename, content)
    await _set_stage(job_id, tenant_id, worker_id, "chunking")
    chunks = split_into_chunks(paragraphs, revision_id=revision_id)
    if not chunks or len(chunks) > get_settings().max_document_chunks:
        raise DocumentInputError("文档分块数量超出处理限制")
    await _set_stage(job_id, tenant_id, worker_id, "embedding", {"chunk_count": len(chunks), "chunker": CHUNKER_VERSION})
    async def heartbeat() -> None:
        """Embedding 分批调用期间续租，避免长任务被其他 Worker 重复领取。"""
        await _set_stage(job_id, tenant_id, worker_id, "embedding")

    vectors = await embed_texts([str(chunk["content"]) for chunk in chunks], on_batch=heartbeat)
    await _set_stage(job_id, tenant_id, worker_id, "indexing", {"chunk_count": len(chunks), "embedded_count": len(vectors)})

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_rls_context(session, tenant_id=tenant_uuid)
            job = await session.scalar(select(IngestionJob).where(IngestionJob.id == job_uuid).with_for_update())
            document = await session.scalar(select(Document).where(Document.id == document_id).with_for_update())
            if job is None or document is None or job.worker_id != worker_id:
                raise RuntimeError("ingestion_lease_lost")
            previous = await session.scalars(
                select(IndexVersion).where(IndexVersion.tenant_id == tenant_uuid, IndexVersion.document_id == document_id)
            )
            old_versions = previous.all()
            version_number = max((item.version_number for item in old_versions), default=0) + 1
            for item in old_versions:
                if item.status == "active":
                    item.status = "retired"
            index_version = IndexVersion(
                id=uuid4(), tenant_id=tenant_uuid, document_id=document_id, revision_id=revision_id,
                version_number=version_number, status="building", embedding_model=get_settings().embedding_model,
                embedding_dimension=1024, chunk_count=len(chunks), parser_version="sagtrag-parser-1",
                chunking_config={"version": CHUNKER_VERSION, "size": 1200, "overlap": 150},
            )
            session.add(index_version)
            await session.flush()
            statement = text("""INSERT INTO chunks
                (id, tenant_id, kb_id, document_id, index_version_id, chunk_key, ordinal, content,
                 clearance_level, search_vector, embedding, metadata_json, created_at)
                VALUES (:id, :tenant_id, :kb_id, :document_id, :index_version_id, :chunk_key, :ordinal,
                 :content, :clearance_level, to_tsvector('simple', sagtrag_search_text(:content)), CAST(:embedding AS vector),
                 CAST(:metadata_json AS json), now())""")
            values = []
            for chunk, vector in zip(chunks, vectors, strict=True):
                values.append({
                    "id": str(uuid4()), "tenant_id": tenant_id, "kb_id": str(document.kb_id),
                    "document_id": str(document.id), "index_version_id": str(index_version.id),
                    "chunk_key": chunk["chunk_key"], "ordinal": chunk["ordinal"], "content": chunk["content"],
                    "clearance_level": clearance, "embedding": "[" + ",".join(map(str, vector)) + "]",
                    "metadata_json": json.dumps({"revision_id": str(revision_id)}, separators=(",", ":")),
                })
            await session.execute(statement, values)
            index_version.status = "active"
            index_version.activated_at = datetime.now(timezone.utc)
            document.active_revision_id = revision_id
            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
            job.error_code = None
            job.error_summary = None
            job.lease_expires_at = None
            job.worker_id = None
            job.checkpoint = {"chunk_count": len(chunks), "index_version_id": str(index_version.id)}


async def _record_failure(job_id: str, tenant_id: str, worker_id: str, error: Exception) -> None:
    """记录不含正文、凭据或内部异常信息的错误并安排有限重试。"""
    frames = traceback.extract_tb(error.__traceback__)
    location = next((frame for frame in reversed(frames) if frame.filename == __file__), None)
    logger.error("ingestion failure type=%s worker_line=%s", type(error).__name__,
                 location.lineno if location else "external")
    code = str(error) if isinstance(error, (DocumentInputError, RuntimeError)) else "ingestion_internal_error"
    summary = {
        "embedding_provider_error": "Embedding 服务暂时不可用",
        "embedding_result_count_mismatch": "Embedding 返回数量不匹配",
        "embedding_dimension_or_value_invalid": "Embedding 维度或数值无效",
        "ingestion_internal_error": "文档处理失败，可稍后重试",
    }.get(code, str(error) if isinstance(error, DocumentInputError) else "文档处理失败，可稍后重试")[:500]
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_rls_context(session, tenant_id=UUID(tenant_id))
            job = await session.scalar(select(IngestionJob).where(IngestionJob.id == UUID(job_id)).with_for_update())
            if job is None or job.worker_id != worker_id:
                return
            job.error_code = re.sub(r"[^a-zA-Z0-9_-]", "_", code)[:80]
            job.error_summary = summary
            job.worker_id = None
            job.lease_expires_at = None
            job.status = "queued" if job.attempt_count < job.max_attempts else "failed"


async def run_worker() -> None:
    """持续轮询数据库任务表；进程启动后自动恢复队列和过期租约任务。"""
    worker_id = f"worker-{os.getpid()}-{uuid4().hex[:8]}"
    settings = get_settings()
    connection = None
    try:
        try:
            connection = await aio_pika.connect_robust(settings.rabbitmq_url.get_secret_value(), timeout=3)
            channel = await connection.channel()
            queue = await channel.declare_queue(INGESTION_QUEUE_NAME, durable=True)
        except Exception:
            logger.warning("ingestion notification queue unavailable; database recovery scan remains active")
            connection = None

        async def handle_notification(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            """消费唤醒通知；数据库领取操作仍负责并发和幂等控制。"""
            async with message.process(requeue=True):
                claimed = None
                try:
                    payload = json.loads(message.body)
                    if not isinstance(payload, dict) or not isinstance(payload.get("job_id"), str):
                        return
                    tenant_id = str(settings.we_com_tenant_id)
                    if payload.get("tenant_id") != tenant_id:
                        return
                    claimed = await claim_job(worker_id, str(payload["job_id"]))
                    if claimed is not None:
                        await process_job(*claimed, worker_id)
                except Exception as exc:
                    logger.error("ingestion notification failed")
                    if claimed is not None:
                        await _record_failure(claimed[0], claimed[1], worker_id, exc)

        if connection is not None:
            await queue.consume(handle_notification)
        while True:
            claimed = await claim_job(worker_id)
            if claimed is None:
                await asyncio.sleep(2)
                continue
            try:
                await process_job(*claimed, worker_id)
            except Exception as exc:
                logger.error("ingestion job failed job_id=%s", claimed[0])
                await _record_failure(claimed[0], claimed[1], worker_id, exc)
    finally:
        if connection is not None:
            await connection.close()


async def _run() -> None:
    """运行 Worker 并在退出时释放数据库连接池。"""
    try:
        from app.rag_worker import run_rag_worker

        await asyncio.gather(run_worker(), run_rag_worker())
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
