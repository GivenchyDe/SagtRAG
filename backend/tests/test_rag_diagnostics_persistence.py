"""Opt-in local PostgreSQL regression for the actual Worker checkpoint flow.

Run with SAGTRAG_RUN_DB_TESTS=1. Only a random temporary schema is written;
all model and delivery calls are replaced, and public business tables are unused.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


pytestmark = pytest.mark.skipif(
    os.getenv("SAGTRAG_RUN_DB_TESTS") != "1",
    reason="requires explicit opt-in to local PostgreSQL fixtures",
)


@pytest_asyncio.fixture
async def worker_db(monkeypatch):
    from app import rag_worker
    from app.config import get_settings
    from app.models import (AuditEvent, Base, ConversationState, Tenant, User,
                            WeComInboundMessage)

    config = dotenv_values("../TEST/.env")
    runtime = make_url(get_settings().database_url)
    assert runtime.host in {"localhost", "127.0.0.1"}, "local database only"
    assert runtime.database == config["POSTGRES_DB"], "local fixture database only"
    schema = "test_rag_diag_" + uuid4().hex
    db = create_async_engine(
        runtime.set(username=config["POSTGRES_USER"], password=config["POSTGRES_PASSWORD"]),
        hide_parameters=True,
        connect_args={"timeout": 3},
        execution_options={"schema_translate_map": {None: schema}},
    )
    tables = [Tenant.__table__, User.__table__, WeComInboundMessage.__table__,
              ConversationState.__table__, AuditEvent.__table__]
    factory = async_sessionmaker(db, expire_on_commit=False, autoflush=False)
    tenant, user = uuid4(), uuid4()
    created = False
    try:
        async with db.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
        created = True
        async with factory() as session, session.begin():
            session.add(Tenant(id=tenant, name="diagnostics fixture", wecom_corp_id=str(tenant)))
            await session.flush()
            session.add(User(id=user, tenant_id=tenant, external_id="fixture", display_name="fixture",
                             wecom_userid="fixture", clearance_level=0))
        monkeypatch.setattr(rag_worker, "SessionFactory", factory)
        monkeypatch.setattr(rag_worker, "get_settings", lambda: SimpleNamespace(we_com_tenant_id=tenant))
        yield factory, tenant, user
    finally:
        try:
            if created:
                async with db.begin() as conn:
                    await conn.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=tables))
                    await conn.execute(text(f'DROP SCHEMA "{schema}"'))
        finally:
            await db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,retry_send", [
    ("answered", False), ("no_answer", False), ("low_relevance", False),
    ("invalid_citation", False), ("service_error", False), ("answered", True),
])
async def test_worker_commits_stage_diagnostics_and_reuses_answer(worker_db, monkeypatch, outcome, retry_send):
    from app import rag_worker
    from app.intent_router import IntentResult
    from app.models import WeComInboundMessage as Message
    from app.rag import RagAnswer
    from app.wecom_delivery import DeliveryError

    factory, tenant, user = worker_db
    message_id = uuid4()
    async with factory() as session, session.begin():
        session.add(Message(id=message_id, tenant_id=tenant, user_id=user, wecom_userid="fixture",
                            wecom_msg_id=str(message_id), msg_type="text", content="fixture question",
                            state="processing", worker_id="fixture-worker", attempt_count=1))

    classify = AsyncMock(return_value=IntentResult(
        "knowledge_query", "knowledge_query", 1.0, "rule", None, "fixture_rule", None, 1, None, "fixture-v1",
    ))
    monkeypatch.setattr(rag_worker, "detect_intent", classify)
    # ensure_intent also reads settings; supplying a result avoids any model call.
    monkeypatch.setattr(rag_worker, "build_policy_version", lambda settings: "fixture-v1")
    expected = {"retrieval_ms": 12, "dense_count": 2, "fts_count": 1,
                "rerank_ms": 7, "rerank_scores": {"fixture": 0.9},
                "outcome": outcome, "rag_total_ms": 25}
    if outcome not in {"low_relevance", "service_error"}:
        expected["generation_ms"] = 6
    if outcome == "service_error":
        expected["error_code"] = "generation_provider_error"

    async def answer(session, principal, question, *, diagnostics):
        # This is how answer_question updates the shared trace, including outcome.
        diagnostics.update(expected)
        return RagAnswer("fixture answer", outcome)

    generate = AsyncMock(side_effect=answer)
    monkeypatch.setattr(rag_worker, "answer_question", generate)
    delivery = SimpleNamespace(send_text=AsyncMock(return_value="fixture-send"))
    if retry_send:
        delivery.send_text.side_effect = [DeliveryError("fixture_transient", retryable=True), "fixture-send"]

    await rag_worker.process_message(message_id, "fixture-worker", delivery)
    # A fresh session is essential: an identity-map-only assertion misses this bug.
    async with factory() as fresh:
        saved = await fresh.get(Message, message_id)
        assert {key: saved.diagnostics.get(key) for key in expected} == expected
        assert saved.diagnostics["intent"] == "knowledge_query"
        assert "send_ms" in saved.diagnostics
        assert saved.send_state == ("retry" if retry_send else "sent")
        assert saved.answer["outcome"] == outcome

    if retry_send:
        async with factory() as session, session.begin():
            saved = await session.get(Message, message_id)
            saved.state, saved.worker_id = "processing", "fixture-worker"
        await rag_worker.process_message(message_id, "fixture-worker", delivery)
        async with factory() as fresh:
            saved = await fresh.get(Message, message_id)
            assert {key: saved.diagnostics.get(key) for key in expected} == expected
            assert saved.send_state == "sent"
        assert delivery.send_text.await_count == 2
    generate.assert_awaited_once()
    classify.assert_awaited_once()
