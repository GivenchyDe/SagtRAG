"""S05 SQLite 提交后新 Session 读回：修复预算、claim_citations 与诊断。

本文件单独放置，避免 aiosqlite 缺失时跳过 S05 纯函数/替身测试。
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_s04_observability_persistence_sqlite import (  # noqa: E402
    enqueue, saved, sqlite_worker,
)
from app import rag
from app.retrieval import Candidate


def _source():
    return Candidate(uuid4(), uuid4(), uuid4(), "S05虚构制度",
                     "报销期限为30天。补贴比例为1.5%。", 0)


def _settings():
    return SimpleNamespace(generation_model="fixture-gen", rerank_model="fixture-rerank",
                           rag_min_relevance=0.25)


@pytest.mark.asyncio
async def test_repair_and_claim_citations_persist_in_new_session(sqlite_worker, monkeypatch):
    from app.models import WeComInboundMessage as Message

    factory, tenant, user = sqlite_worker
    item = _source()
    mid = await enqueue(factory, tenant, user)

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

    monkeypatch.setattr(rag, "get_settings", _settings)
    monkeypatch.setattr(rag, "retrieve", retrieve)
    monkeypatch.setattr(rag, "rerank", rerank)
    monkeypatch.setattr(rag, "revalidate_candidates", revalidate)
    monkeypatch.setattr(rag, "model_json", model)

    snapshots = []

    async def checkpoint(snapshot):
        snapshots.append(dict(snapshot))
        async with factory() as session, session.begin():
            message = await session.get(Message, mid)
            message.diagnostics = dict(snapshot)

    diagnostics = {}
    async with factory() as session:
        answer = await rag.answer_question(
            session, SimpleNamespace(permission_version=1, tenant_id=tenant, user_id=user),
            "虚构问题", diagnostics=diagnostics, checkpoint=checkpoint)
    assert answer.outcome == "answered" and calls["count"] == 2
    assert [item["generation_calls"] for item in snapshots] == [1, 2]
    assert diagnostics["generation_calls"] == 2
    assert diagnostics["repair_attempted"] is True
    assert diagnostics["repair_succeeded"] is True

    async with factory() as session, session.begin():
        message = await session.get(Message, mid)
        message.answer = answer.payload()
        message.diagnostics = dict(diagnostics)
    row = await saved(factory, mid)
    assert row.answer["outcome"] == "answered"
    assert len(row.answer["claim_citations"]) == 2
    assert row.diagnostics["generation_calls"] == 2
    assert row.diagnostics["repair_attempted"] is True
    assert row.diagnostics["repair_succeeded"] is True
