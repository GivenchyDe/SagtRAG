"""S04 计时/重试持久化的真实本机 PostgreSQL 回归（需显式启用）。

运行前设置 SAGTRAG_RUN_DB_TESTS=1，并确保本机 PostgreSQL 可用。
只写随机临时 schema，模型和发送调用均为替身，不访问生产。
"""

import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from test_rag_diagnostics_persistence import worker_db  # 共用本机随机 schema fixture

pytestmark = pytest.mark.skipif(
    os.getenv("SAGTRAG_RUN_DB_TESTS") != "1",
    reason="requires explicit opt-in to local PostgreSQL fixtures",
)


def intent_result():
    from app.intent_router import IntentResult

    return IntentResult("knowledge_query", "knowledge_query", 1.0, "rule", None,
                        "fixture_rule", None, 3, None, "fixture-v1")


async def enqueue(factory, tenant, user):
    from app.models import WeComInboundMessage as Message
    from app.rag_worker import now

    mid = uuid4()
    received = now() - timedelta(seconds=5)
    async with factory() as session, session.begin():
        session.add(Message(
            id=mid, tenant_id=tenant, user_id=user, wecom_userid="fixture",
            wecom_msg_id=str(mid), msg_type="text", content="虚构S04问题", state="queued",
            received_at=received, next_attempt_at=received, attempt_count=0, diagnostics={},
        ))
    return mid


async def saved(factory, mid):
    from app.models import WeComInboundMessage as Message

    async with factory() as fresh:
        return await fresh.get(Message, mid)


@pytest.mark.asyncio
async def test_postgres_commit_then_new_session_keeps_claim_and_timing(worker_db, monkeypatch):
    from app import rag_worker
    from app.rag import RagAnswer

    factory, tenant, user = worker_db
    mid = await enqueue(factory, tenant, user)
    monkeypatch.setattr(rag_worker, "detect_intent", AsyncMock(return_value=intent_result()))

    async def answer(session, principal, question, *, diagnostics):
        diagnostics.update({
            "retrieval_ms": 12, "rerank_ms": 7, "generation_ms": 34,
            "generation_first_content_ms": 5, "rag_total_ms": 60,
            "diagnostic_code": "answered", "outcome": "answered",
        })
        return RagAnswer("虚构答案", "answered", diagnostic_code="answered",
                         detail={"claim_count": 1, "source_count": 0, "rendered_bytes": 12})

    generate = AsyncMock(side_effect=answer)
    monkeypatch.setattr(rag_worker, "answer_question", generate)
    delivery = SimpleNamespace(send_text=AsyncMock(return_value="fixture-msg"))

    assert await rag_worker.claim_message("s04-worker") == mid
    claimed = await saved(factory, mid)
    first_claimed_at = claimed.first_claimed_at
    queue_wait_ms = claimed.diagnostics["queue_wait_ms"]
    assert first_claimed_at is not None and queue_wait_ms >= 4000

    await rag_worker.process_message(mid, "s04-worker", delivery)
    row = await saved(factory, mid)
    assert row.state == "completed" and row.send_state == "sent"
    assert row.first_claimed_at == first_claimed_at
    assert row.diagnostics["queue_wait_ms"] == queue_wait_ms
    assert row.diagnostics["processing_ms"] is not None
    assert row.diagnostics["attempts"][0]["state"] == "completed"
    assert row.diagnostics["send_attempts"][-1]["state"] == "sent"
    assert row.diagnostics["end_to_end_ms"] >= queue_wait_ms
    assert row.diagnostics["diagnostic_code"] == "answered"
    assert row.diagnostics["policy"]["source_whitelist"]["predicate"] == "visible_chunks"
    generate.assert_awaited_once()
