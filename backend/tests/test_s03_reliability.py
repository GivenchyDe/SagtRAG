"""S03消息可靠性固定场景：真实本机事务，模型与发送不外发。"""

import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from test_rag_diagnostics_persistence import worker_db  # 共用opt-in、本机随机schema fixture。

pytestmark = pytest.mark.skipif(
    os.getenv("SAGTRAG_RUN_DB_TESTS") != "1", reason="requires opt-in to local PostgreSQL fixtures",
)


async def enqueue(factory, tenant, user, **overrides):
    from app.models import WeComInboundMessage as Message
    from app.rag_worker import now
    mid = uuid4()
    values = dict(id=mid, tenant_id=tenant, user_id=user, wecom_userid="fixture",
                  wecom_msg_id=str(mid), msg_type="text", content="虚构可靠性问题",
                  state="processing", worker_id="s03-worker", attempt_count=1,
                  lease_expires_at=now()-timedelta(seconds=1),
                  next_attempt_at=now()-timedelta(seconds=1), permission_version=1,
                  answer={"text":"虚构旧答案", "plain_text":"虚构旧答案", "outcome":"answered", "sources":[]})
    values.update(overrides)
    async with factory() as session, session.begin():
        session.add(Message(**values))
    return mid


async def saved(factory, mid):
    from app.models import WeComInboundMessage as Message
    async with factory() as fresh:
        return await fresh.get(Message, mid)


@pytest.mark.asyncio
async def test_duplicate_message_id_rejected_by_real_database(worker_db):
    from app.models import WeComInboundMessage as Message
    factory, tenant, user = worker_db
    mid = await enqueue(factory, tenant, user)
    with pytest.raises(IntegrityError):
        await enqueue(factory, tenant, user, wecom_msg_id=str(mid))
    async with factory() as fresh:
        assert len((await fresh.execute(select(Message.id))).all()) == 1


@pytest.mark.asyncio
async def test_expired_lease_reuses_answer_without_models(worker_db, monkeypatch):
    from app import rag_worker
    factory, tenant, user = worker_db
    mid = await enqueue(factory, tenant, user, worker_id="crashed")
    classify, generate = AsyncMock(), AsyncMock()
    monkeypatch.setattr(rag_worker, "detect_intent", classify)
    monkeypatch.setattr(rag_worker, "answer_question", generate)
    assert await rag_worker.claim_message("recovered") == mid
    delivery = SimpleNamespace(send_text=AsyncMock(return_value="fixture-send"))
    await rag_worker.process_message(mid, "recovered", delivery)
    row = await saved(factory, mid)
    assert row.state == "completed" and row.send_state == "sent"
    assert row.answer["text"] == "虚构旧答案"
    classify.assert_not_awaited()
    generate.assert_not_awaited()
    delivery.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_sending_becomes_unknown_and_is_not_reclaimed(worker_db):
    from app import rag_worker
    factory, tenant, user = worker_db
    mid = await enqueue(factory, tenant, user, send_state="sending")
    assert await rag_worker.claim_message("recovered") is None
    row = await saved(factory, mid)
    assert (row.state, row.send_state, row.error_code) == ("failed", "unknown", "send_interrupted_unknown")
    assert await rag_worker.claim_message("again") is None


@pytest.mark.asyncio
async def test_permission_version_change_replaces_saved_answer_before_send(worker_db):
    from app import rag_worker
    from app.models import User
    from app.rag import PERMISSION_CHANGED
    factory, tenant, user = worker_db
    mid = await enqueue(factory, tenant, user)
    async with factory() as session, session.begin():
        await session.execute(update(User).where(User.id == user).values(permission_version=2))
    delivery = SimpleNamespace(send_text=AsyncMock(return_value="fixture-send"))
    await rag_worker.process_message(mid, "s03-worker", delivery)
    row = await saved(factory, mid)
    assert row.answer["outcome"] == "permission_changed"
    assert row.diagnostics["outcome"] == "permission_changed"
    assert delivery.send_text.await_args.args[1] == PERMISSION_CHANGED


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"wecom_userid":None}, {"status":"disabled"}])
async def test_revoked_identity_cancels_saved_answer(worker_db, change):
    from app import rag_worker
    from app.models import User
    factory, tenant, user = worker_db
    mid = await enqueue(factory, tenant, user)
    async with factory() as session, session.begin():
        await session.execute(update(User).where(User.id == user).values(**change))
    delivery = SimpleNamespace(send_text=AsyncMock())
    await rag_worker.process_message(mid, "s03-worker", delivery)
    row = await saved(factory, mid)
    assert row.send_state == "cancelled" and row.answer is None
    delivery.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_delivery_is_not_automatically_requeued(worker_db):
    from app import rag_worker
    from app.wecom_delivery import DeliveryError
    factory, tenant, user = worker_db
    mid = await enqueue(factory, tenant, user)
    delivery = SimpleNamespace(send_text=AsyncMock(side_effect=DeliveryError("fixture_unknown", uncertain=True)))
    await rag_worker.process_message(mid, "s03-worker", delivery)
    row = await saved(factory, mid)
    assert (row.state, row.send_state) == ("failed", "unknown")
    assert await rag_worker.claim_message("again") is None
    delivery.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_message_is_not_sent_twice(worker_db):
    from app import rag_worker
    factory, tenant, user = worker_db
    mid = await enqueue(factory, tenant, user)
    delivery = SimpleNamespace(send_text=AsyncMock(return_value="fixture-send"))
    await rag_worker.process_message(mid, "s03-worker", delivery)
    await rag_worker.process_message(mid, "s03-worker", delivery)
    delivery.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_attempt_limit_preserves_final_failure(worker_db):
    from app import rag_worker
    factory, tenant, user = worker_db
    mid = await enqueue(factory, tenant, user, send_attempt_count=rag_worker.MAX_ATTEMPTS)
    delivery = SimpleNamespace(send_text=AsyncMock())
    await rag_worker.process_message(mid, "s03-worker", delivery)
    row = await saved(factory, mid)
    assert (row.state, row.send_state, row.error_code) == ("failed", "failed", "send_attempt_limit")
    delivery.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_processing_exception_after_sending_checkpoint_becomes_unknown(worker_db):
    from app import rag_worker
    factory, tenant, user = worker_db
    mid = await enqueue(factory, tenant, user, send_state="sending")
    await rag_worker.record_failure(mid, "s03-worker", RuntimeError("fixture-crash"))
    row = await saved(factory, mid)
    assert row.state == "failed" and row.send_state == "unknown"
    assert await rag_worker.claim_message("again") is None
