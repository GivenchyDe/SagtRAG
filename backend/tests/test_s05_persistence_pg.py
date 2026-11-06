"""S05 真实本机 PostgreSQL 读回：修复预算与 claim_citations（需 opt-in）。"""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_rag_diagnostics_persistence import worker_db

pytestmark = pytest.mark.skipif(
    os.getenv("SAGTRAG_RUN_DB_TESTS") != "1",
    reason="requires explicit opt-in to local PostgreSQL fixtures",
)


def _candidate():
    from app.retrieval import Candidate

    return Candidate(uuid4(), uuid4(), uuid4(), "S05虚构制度", "报销期限为30天。补贴比例为1.5%。", 0)


@pytest.mark.asyncio
async def test_s05_repair_budget_and_claim_citations_read_back(worker_db, monkeypatch):
    from app import rag
    from app.models import WeComInboundMessage as Message
    from app.rag import answer_question

    factory, tenant, user = worker_db
    item = _candidate()
    mid = uuid4()
    async with factory() as session, session.begin():
        session.add(Message(id=mid, tenant_id=tenant, user_id=user, wecom_userid="fixture",
                            wecom_msg_id=str(mid), msg_type="text", content="虚构S05问题",
                            state="processing", worker_id="s05-worker", attempt_count=1))

    async def retrieve(session, principal, question, *, diagnostics=None):
        return [item]

    async def rerank(question, candidates, *, diagnostics=None):
        return candidates

    async def revalidate(session, principal, candidates):
        return True

    calls = {"count": 0}

    async def model(system, data, *, diagnostics=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return {"refused": False, "claims": [{"text": "报销期限为30天。", "citations": [
                {"chunk_id": str(item.id), "quote": "不存在的摘录"}]}]}
        return {"refused": False, "claims": [
            {"text": "报销期限为30天。", "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
            {"text": "补贴比例为1.5%。", "citations": [{"chunk_id": str(item.id), "quote": "补贴比例为1.5%"}]},
        ]}

    monkeypatch.setattr(rag, "get_settings", lambda: SimpleNamespace(
        generation_model="fixture-gen", rerank_model="fixture-rerank", rag_min_relevance=0.25))
    monkeypatch.setattr(rag, "retrieve", retrieve)
    monkeypatch.setattr(rag, "rerank", rerank)
    monkeypatch.setattr(rag, "revalidate_candidates", revalidate)
    monkeypatch.setattr(rag, "model_json", model)

    async def checkpoint(snapshot):
        async with factory() as session, session.begin():
            message = await session.get(Message, mid)
            message.diagnostics = dict(snapshot)

    diagnostics = {}
    async with factory() as session:
        answer = await answer_question(
            session, SimpleNamespace(permission_version=1, tenant_id=tenant, user_id=user),
            "虚构问题", diagnostics=diagnostics, checkpoint=checkpoint)
    assert answer.outcome == "answered" and calls["count"] == 2
    async with factory() as session, session.begin():
        message = await session.get(Message, mid)
        message.answer = answer.payload()
        message.diagnostics = dict(diagnostics)

    async with factory() as fresh:
        saved = await fresh.get(Message, mid)
        assert saved.answer["claim_citations"][0]["quote"] == "报销期限为30天"
        assert len(saved.answer["claim_citations"]) == 2
        assert saved.diagnostics["generation_calls"] == 2
        assert saved.diagnostics["repair_attempted"] is True
        assert saved.diagnostics["repair_succeeded"] is True
