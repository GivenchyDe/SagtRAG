"""S04 计时/重试持久化的本机 ORM 提交与新 Session 读回测试。

说明：当前工作区没有可用的本机 PostgreSQL 服务，因此本文件使用 SQLite
异步引擎验证"提交后由新 Session 读回"的 ORM/JSON 变更语义、重试与计时保留。
它不是 PostgreSQL 方言、迁移或 RLS 验证；PostgreSQL 版本见
test_s04_observability_persistence_pg.py（需要 SAGTRAG_RUN_DB_TESTS=1）。
测试只使用虚构用户、虚构问题与模型/发送替身。
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

aiosqlite = pytest.importorskip("aiosqlite")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

def naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def sqlite_worker(monkeypatch):
    from app import rag_worker
    from app.models import (AuditEvent, Base, ConversationState, Tenant, User, UserStatus,
                            WeComInboundMessage)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [Tenant.__table__, User.__table__, WeComInboundMessage.__table__,
              ConversationState.__table__, AuditEvent.__table__]
    diagnostics_column = WeComInboundMessage.__table__.c.diagnostics
    original_default = diagnostics_column.server_default
    diagnostics_column.server_default = None
    conversation_defaults = {
        column.name: column.server_default
        for column in (ConversationState.__table__.c.confirmed_info,
                       ConversationState.__table__.c.pending_fields)
    }
    for column in (ConversationState.__table__.c.confirmed_info,
                   ConversationState.__table__.c.pending_fields):
        column.server_default = None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    finally:
        diagnostics_column.server_default = original_default
        for column in (ConversationState.__table__.c.confirmed_info,
                       ConversationState.__table__.c.pending_fields):
            column.server_default = conversation_defaults[column.name]

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tenant, user = uuid4(), uuid4()
    async with factory() as session, session.begin():
        session.add(Tenant(id=tenant, name="S04虚构租户", wecom_corp_id=str(tenant)))
        await session.flush()
        session.add(User(id=user, tenant_id=tenant, external_id="s04-fixture", display_name="虚构员工",
                         wecom_userid="fixture", clearance_level=0, status=UserStatus.ACTIVE))

    async def noop(*args, **kwargs):
        return None

    async def principal(session, user_row):
        return SimpleNamespace(developer_mode=False, permission_version=user_row.permission_version,
                               tenant_id=tenant)

    monkeypatch.setattr(rag_worker, "SessionFactory", factory)
    monkeypatch.setattr(rag_worker, "set_tenant_rls_context", noop)
    monkeypatch.setattr(rag_worker, "load_principal", principal)
    monkeypatch.setattr(rag_worker, "get_settings", lambda: SimpleNamespace(we_com_tenant_id=tenant))
    monkeypatch.setattr(rag_worker, "now", naive_now)
    try:
        yield factory, tenant, user
    finally:
        await engine.dispose()


async def enqueue(factory, tenant, user, **overrides):
    from app.models import WeComInboundMessage as Message

    mid = uuid4()
    received = naive_now() - timedelta(seconds=5)
    values = dict(
        id=mid, tenant_id=tenant, user_id=user, wecom_userid="fixture",
        wecom_msg_id=str(mid), msg_type="text", content="虚构S04问题", state="queued",
        received_at=received, next_attempt_at=received, attempt_count=0, diagnostics={},
    )
    values.update(overrides)
    async with factory() as session, session.begin():
        session.add(Message(**values))
    return mid


async def saved(factory, mid):
    from app.models import WeComInboundMessage as Message

    async with factory() as fresh:
        return await fresh.get(Message, mid)


def intent_result():
    from app.intent_router import IntentResult

    return IntentResult("knowledge_query", "knowledge_query", 1.0, "rule", None,
                        "fixture_rule", None, 3, None, "fixture-v1")


def fake_answer_factory():
    from app.rag import RagAnswer

    async def answer(session, principal, question, *, diagnostics):
        diagnostics.update({
            "retrieval_ms": 12, "rerank_ms": 7, "generation_ms": 34,
            "generation_first_content_ms": 5, "rag_total_ms": 60,
            "diagnostic_code": "answered", "outcome": "answered",
        })
        return RagAnswer("虚构答案", "answered", diagnostic_code="answered",
                         detail={"claim_count": 1, "source_count": 0, "rendered_bytes": 12})

    return answer


@pytest.mark.asyncio
async def test_commit_then_new_session_reads_claimed_and_timing(sqlite_worker, monkeypatch):
    from app import rag_worker

    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user)
    monkeypatch.setattr(rag_worker, "detect_intent", AsyncMock(return_value=intent_result()))
    generate = AsyncMock(side_effect=fake_answer_factory())
    monkeypatch.setattr(rag_worker, "answer_question", generate)
    delivery = SimpleNamespace(send_text=AsyncMock(return_value="fixture-msg"))

    assert await rag_worker.claim_message("s04-worker") == mid
    claimed = await saved(factory, mid)
    first_claimed_at = claimed.first_claimed_at
    queue_wait_ms = claimed.diagnostics["queue_wait_ms"]
    assert first_claimed_at is not None and queue_wait_ms >= 4000
    assert claimed.diagnostics["attempts"][0]["number"] == 1

    await rag_worker.process_message(mid, "s04-worker", delivery)
    row = await saved(factory, mid)
    assert row.state == "completed" and row.send_state == "sent"
    assert row.first_claimed_at == first_claimed_at
    assert row.diagnostics["queue_wait_ms"] == queue_wait_ms
    assert row.diagnostics["diagnostic_code"] == "answered"
    assert row.diagnostics["processing_ms"] is not None
    assert row.diagnostics["attempts"][0]["state"] == "completed"
    assert row.diagnostics["attempts"][0]["duration_ms"] is not None
    assert row.diagnostics["attempts"][0]["timings"]["generation_ms"] == 34
    assert row.diagnostics["attempts"][0]["timings"]["generation_first_content_ms"] == 5
    assert row.diagnostics["send_attempts"][-1]["state"] == "sent"
    assert row.diagnostics["send_attempts"][-1]["duration_ms"] is not None
    assert row.diagnostics["end_to_end_ms"] >= queue_wait_ms
    assert row.diagnostics["generation_first_content_ms"] == 5
    assert row.diagnostics["generation_ms"] == 34
    assert row.diagnostics["policy"]["source_whitelist"]["predicate"] == "visible_chunks"
    generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_retry_keeps_first_claim_and_reuses_generated_answer(sqlite_worker, monkeypatch):
    from app import rag_worker
    from app.wecom_delivery import DeliveryError

    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user)
    monkeypatch.setattr(rag_worker, "detect_intent", AsyncMock(return_value=intent_result()))
    generate = AsyncMock(side_effect=fake_answer_factory())
    monkeypatch.setattr(rag_worker, "answer_question", generate)
    delivery = SimpleNamespace(send_text=AsyncMock(
        side_effect=[DeliveryError("fixture_transient", retryable=True), "fixture-msg"]))

    assert await rag_worker.claim_message("s04-worker") == mid
    first = await saved(factory, mid)
    first_claimed_at = first.first_claimed_at
    queue_wait_ms = first.diagnostics["queue_wait_ms"]
    await rag_worker.process_message(mid, "s04-worker", delivery)
    retry = await saved(factory, mid)
    assert (retry.state, retry.send_state) == ("queued", "retry")
    assert retry.completed_at is None and "end_to_end_ms" not in retry.diagnostics
    assert retry.diagnostics["send_attempts"][-1]["state"] == "retry"
    generation_ms = retry.diagnostics["generation_ms"]

    from app.models import WeComInboundMessage as Message

    async with factory() as session, session.begin():
        stored = await session.get(Message, mid)
        stored.next_attempt_at = naive_now() - timedelta(seconds=1)
    assert await rag_worker.claim_message("s04-worker") == mid
    await rag_worker.process_message(mid, "s04-worker", delivery)
    done = await saved(factory, mid)
    assert done.first_claimed_at == first_claimed_at
    assert done.diagnostics["queue_wait_ms"] == queue_wait_ms
    assert done.diagnostics["generation_ms"] == generation_ms
    assert len(done.diagnostics["attempts"]) == 1
    assert len(done.diagnostics["send_attempts"]) == 2
    assert done.diagnostics["send_attempts"][-1]["state"] == "sent"
    assert done.diagnostics["end_to_end_ms"] is not None
    generate.assert_awaited_once()
    assert delivery.send_text.await_count == 2


@pytest.mark.asyncio
async def test_processing_retry_appends_attempt_and_reuses_classification(sqlite_worker, monkeypatch):
    from app import rag_worker

    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user)
    classify = AsyncMock(return_value=intent_result())
    healthy = fake_answer_factory()
    calls = {"count": 0}

    async def flaky(session, principal, question, *, diagnostics):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("fixture_crash")
        return await healthy(session, principal, question, diagnostics=diagnostics)

    generate = AsyncMock(side_effect=flaky)
    monkeypatch.setattr(rag_worker, "detect_intent", classify)
    monkeypatch.setattr(rag_worker, "answer_question", generate)
    delivery = SimpleNamespace(send_text=AsyncMock(return_value="fixture-msg"))

    assert await rag_worker.claim_message("s04-worker") == mid
    first = await saved(factory, mid)
    first_claimed_at = first.first_claimed_at
    queue_wait_ms = first.diagnostics["queue_wait_ms"]
    with pytest.raises(RuntimeError):
        await rag_worker.process_message(mid, "s04-worker", delivery)
    await rag_worker.record_failure(mid, "s04-worker", RuntimeError("fixture_crash"))
    failed = await saved(factory, mid)
    assert failed.state == "queued" and failed.completed_at is None
    assert failed.diagnostics["attempts"][0]["state"] == "failed"

    from app.models import WeComInboundMessage as Message

    async with factory() as session, session.begin():
        stored = await session.get(Message, mid)
        stored.next_attempt_at = naive_now() - timedelta(seconds=1)
    assert await rag_worker.claim_message("s04-worker") == mid
    await rag_worker.process_message(mid, "s04-worker", delivery)
    done = await saved(factory, mid)
    assert done.first_claimed_at == first_claimed_at
    assert done.diagnostics["queue_wait_ms"] == queue_wait_ms
    assert len(done.diagnostics["attempts"]) == 2
    assert done.diagnostics["attempts"][0]["state"] == "failed"
    assert done.diagnostics["attempts"][-1]["state"] == "completed"
    assert classify.await_count == 1
    assert generate.await_count == 2
    assert done.diagnostics["route_reused"] is True
    assert done.diagnostics["attempts"][-1]["timings"]["route_reused"] is True
    assert done.diagnostics["attempts"][-1]["timings"].get("route_ms") is None
    assert done.diagnostics["attempts"][-1]["timings"]["historical_route_ms"] == 3
    from app.api.admin_qa import timing_projection

    timing = timing_projection(done, done.diagnostics)
    assert timing["route_ms"] is None
    assert timing["route_reused"] is True
    assert timing["historical_route_ms"] == 3
    assert done.state == "completed" and done.send_state == "sent"


@pytest.mark.asyncio
async def test_uncaught_timeout_handoff_keeps_stage_timings_and_duration(sqlite_worker, monkeypatch):
    from app import rag_worker

    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user)
    monkeypatch.setattr(rag_worker, "detect_intent", AsyncMock(return_value=intent_result()))

    async def timeout(session, principal, question, *, diagnostics):
        diagnostics.update({"retrieval_ms": 12, "generation_ms": 99,
                            "generation_first_content_ms": 9})
        raise TimeoutError("fixture-only")

    monkeypatch.setattr(rag_worker, "answer_question", timeout)
    assert await rag_worker.claim_message("s04-timeout") == mid
    with pytest.raises(TimeoutError):
        await rag_worker.process_message(mid, "s04-timeout", SimpleNamespace(send_text=AsyncMock()))
    # 外层 Worker 即使拿到新的异常对象，也应通过交接快照恢复本次阶段。
    await rag_worker.record_failure(mid, "s04-timeout", TimeoutError("outer"))
    row = await saved(factory, mid)
    assert row.diagnostics["generation_ms"] == 99
    assert row.diagnostics["generation_first_content_ms"] == 9
    assert len(row.diagnostics["attempts"]) == 1
    attempt = row.diagnostics["attempts"][0]
    assert attempt["timings"]["retrieval_ms"] == 12
    assert attempt["timings"]["generation_ms"] == 99
    assert attempt["duration_ms"] is not None
    assert row.state == "queued"


@pytest.mark.asyncio
async def test_voice_attempt_keeps_route_and_substage_timings(sqlite_worker, monkeypatch):
    from app import rag_worker

    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user, msg_type="voice", content="", media_id="fixture")

    async def transcribe(delivery, media_id, diagnostics):
        diagnostics["voice"] = {"state": "transcribed", "download_ms": 1, "convert_ms": 2,
                                "asr_ms": 3, "voice_ms": 6}
        return "虚构语音问题"

    monkeypatch.setattr(rag_worker, "transcribe_voice", transcribe)
    monkeypatch.setattr(rag_worker, "detect_intent", AsyncMock(return_value=intent_result()))
    monkeypatch.setattr(rag_worker, "answer_question", fake_answer_factory())
    assert await rag_worker.claim_message("s04-voice") == mid
    await rag_worker.process_message(mid, "s04-voice",
                                     SimpleNamespace(send_text=AsyncMock(return_value="fixture")))
    row = await saved(factory, mid)
    timings = row.diagnostics["attempts"][-1]["timings"]
    assert timings["route_ms"] == 3
    assert timings["voice_ms"] == 6
    assert timings["asr_ms"] == 3
    assert timings["download_ms"] == 1
    assert timings["convert_ms"] == 2
    assert timings["voice_state"] == "transcribed"
    assert timings["route_reused"] is False


@pytest.mark.asyncio
async def test_classification_failure_persists_attempt_duration_and_error(sqlite_worker, monkeypatch):
    from app import rag_worker

    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user)
    monkeypatch.setattr(rag_worker, "detect_intent", AsyncMock(side_effect=RuntimeError("fixture-classify")))
    assert await rag_worker.claim_message("s04-classify") == mid
    with pytest.raises(RuntimeError):
        await rag_worker.process_message(mid, "s04-classify", SimpleNamespace(send_text=AsyncMock()))
    await rag_worker.record_failure(mid, "s04-classify", RuntimeError("outer"))
    row = await saved(factory, mid)
    assert row.state == "queued"
    assert row.error_code == "rag_processing_error"
    assert len(row.diagnostics["attempts"]) == 1
    assert row.diagnostics["attempts"][0]["duration_ms"] is not None
    assert "route_ms" not in row.diagnostics["attempts"][0]["timings"]


@pytest.mark.asyncio
async def test_voice_retryable_failure_persists_started_stage(sqlite_worker, monkeypatch):
    from app import rag_worker
    from app.voice import VoiceError

    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user, msg_type="voice", content="", media_id="fixture")

    async def broken(delivery, media_id, diagnostics):
        diagnostics["voice"] = {"state": "failed", "download_ms": 4, "voice_ms": 5,
                                "error_code": "voice_download_error", "retryable": True}
        raise VoiceError("voice_download_error", retryable=True)

    monkeypatch.setattr(rag_worker, "transcribe_voice", broken)
    assert await rag_worker.claim_message("s04-voice-fail") == mid
    with pytest.raises(VoiceError):
        await rag_worker.process_message(mid, "s04-voice-fail", SimpleNamespace(send_text=AsyncMock()))
    await rag_worker.record_failure(mid, "s04-voice-fail", VoiceError("voice_download_error", retryable=True))
    row = await saved(factory, mid)
    assert row.state == "queued" and row.error_code == "voice_download_error"
    assert row.diagnostics["voice"]["download_ms"] == 4
    assert "asr_ms" not in row.diagnostics["voice"]
    timings = row.diagnostics["attempts"][-1]["timings"]
    assert timings["download_ms"] == 4 and timings["voice_ms"] == 5


@pytest.mark.asyncio
async def test_service_error_answer_persists_failure_diagnostic(sqlite_worker, monkeypatch):
    from app import rag_worker
    from app.rag import SERVICE_ERROR, RagAnswer

    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user)
    monkeypatch.setattr(rag_worker, "detect_intent", AsyncMock(return_value=intent_result()))

    async def service_error(session, principal, question, *, diagnostics):
        diagnostics.update({"error_code": "generation_provider_error",
                            "diagnostic_code": "generation_provider_error",
                            "generation_ms": 11, "rag_total_ms": 20})
        return RagAnswer(SERVICE_ERROR, "service_error", diagnostic_code="generation_provider_error")

    monkeypatch.setattr(rag_worker, "answer_question", service_error)
    assert await rag_worker.claim_message("s04-service") == mid
    await rag_worker.process_message(mid, "s04-service",
                                     SimpleNamespace(send_text=AsyncMock(return_value="fixture")))
    row = await saved(factory, mid)
    assert row.answer["outcome"] == "service_error"
    assert row.diagnostics["diagnostic_code"] == "generation_provider_error"
    assert row.diagnostics["attempts"][-1]["state"] == "failed"
    assert row.diagnostics["attempts"][-1]["timings"]["generation_ms"] == 11


@pytest.mark.asyncio
async def test_uncertain_delivery_persists_unknown_without_retry(sqlite_worker, monkeypatch):
    from app import rag_worker
    from app.wecom_delivery import DeliveryError

    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user)
    monkeypatch.setattr(rag_worker, "detect_intent", AsyncMock(return_value=intent_result()))
    monkeypatch.setattr(rag_worker, "answer_question", fake_answer_factory())
    delivery = SimpleNamespace(send_text=AsyncMock(side_effect=DeliveryError("fixture_unknown", uncertain=True)))
    assert await rag_worker.claim_message("s04-unknown") == mid
    await rag_worker.process_message(mid, "s04-unknown", delivery)
    row = await saved(factory, mid)
    assert (row.state, row.send_state) == ("failed", "unknown")
    assert row.diagnostics["end_to_end_ms"] is not None
    assert row.diagnostics["send_attempts"][-1]["state"] == "unknown"
    assert await rag_worker.claim_message("again") is None
    delivery.send_text.assert_awaited_once()
