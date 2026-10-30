"""带实时员工权限的 Dense、FTS 和 RRF 候选检索。"""

from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import set_tenant_rls_context
from app.embeddings import embed_texts
from app.models import Chunk, Document, IndexVersion, KnowledgeBase
from app.permissions import Principal, document_access_predicate


@dataclass(frozen=True)
class Candidate:
    """只包含已授权分块，供重排与引用白名单复用。"""

    id: UUID
    document_id: UUID
    index_version_id: UUID
    title: str
    content: str
    ordinal: int
    score: float = 0.0


def visible_chunks(principal: Principal):
    """所有检索和发送前复核共用同一 SQL 可见范围。"""
    return (
        select(Chunk.id, Chunk.document_id, Chunk.index_version_id, Document.title, Chunk.content, Chunk.ordinal)
        .join(Document, (Document.id == Chunk.document_id) & (Document.tenant_id == Chunk.tenant_id))
        .join(IndexVersion, (IndexVersion.id == Chunk.index_version_id) & (IndexVersion.tenant_id == Chunk.tenant_id))
        .join(KnowledgeBase, (KnowledgeBase.id == Chunk.kb_id) & (KnowledgeBase.tenant_id == Chunk.tenant_id))
        .where(
            document_access_predicate(Chunk, principal),
            document_access_predicate(Document, principal),
            Document.status == "active", KnowledgeBase.status == "active",
            IndexVersion.status == "active", IndexVersion.document_id == Document.id,
            IndexVersion.revision_id == Document.active_revision_id,
            IndexVersion.embedding_model == get_settings().embedding_model,
            IndexVersion.embedding_dimension == 1024,
        )
    )


def reciprocal_rank_fusion(rankings: list[list[Candidate]], limit: int) -> list[Candidate]:
    """合并已授权的候选集合，不引入集合外内容。"""
    items: dict[UUID, Candidate] = {}
    scores: dict[UUID, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            items[item.id] = item
            scores[item.id] = scores.get(item.id, 0.0) + 1.0 / (60 + rank)
    return [replace(items[key], score=scores[key])
            for key in sorted(items, key=lambda key: (-scores[key], str(key)))[:limit]]


async def retrieve(session: AsyncSession, principal: Principal, question: str, *, diagnostics: dict | None = None) -> list[Candidate]:
    """先在 SQL 内过滤权限，再取两路 Top K、执行 RRF。"""
    vector = (await embed_texts([question]))[0]
    await set_tenant_rls_context(session, tenant_id=principal.tenant_id,
                                 subject_id=principal.user_id, clearance_level=principal.clearance_level)
    limit = get_settings().rag_recall_limit
    base = visible_chunks(principal)
    distance = text("chunks.embedding <=> CAST(:query_vector AS vector)")
    dense_rows = (await session.execute(
        base.where(Chunk.embedding.is_not(None)).order_by(distance, Chunk.id).limit(limit),
        {"query_vector": "[" + ",".join(map(str, vector)) + "]"},
    )).all()
    terms = func.tsvector_to_array(func.to_tsvector("simple", func.sagtrag_search_text(question)))
    tsquery = func.to_tsquery("simple", func.array_to_string(terms, " | "))
    fts_rows = (await session.execute(
        base.where(Chunk.search_vector.op("@@")(tsquery))
        .order_by(func.ts_rank_cd(Chunk.search_vector, tsquery).desc(), Chunk.id).limit(limit)
    )).all()
    if diagnostics is not None:
        diagnostics["dense_count"] = len(dense_rows)
        diagnostics["fts_count"] = len(fts_rows)
        diagnostics["dense_order"] = [str(row.id) for row in dense_rows]
        diagnostics["fts_order"] = [str(row.id) for row in fts_rows]
    return reciprocal_rank_fusion(
        [[Candidate(**row._mapping) for row in dense_rows], [Candidate(**row._mapping) for row in fts_rows]], limit,
    )


async def revalidate_candidates(
    session: AsyncSession, principal: Principal, candidates: list[Candidate],
) -> bool:
    """在重排、生成和引用发送边界断言所有候选仍然可见。"""
    if not candidates:
        return True
    ids = {item.id for item in candidates}
    current = set((await session.execute(visible_chunks(principal).where(Chunk.id.in_(ids)))).scalars())
    return current == ids
