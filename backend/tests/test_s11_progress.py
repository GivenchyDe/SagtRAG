"""S11 有限进度提示与 unknown 可诊断行为测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

pytest.importorskip("aiosqlite")

from test_s04_observability_persistence_sqlite import enqueue, saved, sqlite_worker  # noqa: E402

from app import rag_worker  # noqa: E402
from app.wecom_delivery import DeliveryError  # noqa: E402


def delivery(send=None):
    return SimpleNamespace(send_text=send or AsyncMock(return_value="progress-msg"))


@pytest.mark.asyncio
async def test_progress_sent_once_and_not_final(sqlite_worker):
    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user, content="虚构慢问题")
    assert await rag_worker.claim_message("s11-a") == mid
    sender = delivery()
    await rag_worker.maybe_send_progress(mid, "s11-a", sender, after_seconds=0)
    row = await saved(factory, mid)
    assert row.progress_state == "sent"
    assert row.progress_attempt_count == 1
    assert row.progress_msg_id == "progress-msg"
    assert row.progress_sent_at is not None
    assert row.state == "processing" and row.send_state == "pending" and row.answer is None
    assert row.diagnostics["progress_state"] == "sent"
    sender.send_text.assert_awaited_once()
    assert sender.send_text.await_args.args[1] == rag_worker.PROGRESS_TEXT

    await rag_worker.maybe_send_progress(mid, "s11-a", sender, after_seconds=0)
    sender.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_progress_unknown_not_retried_and_answer_not_final(sqlite_worker):
    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user, content="虚构慢问题")
    assert await rag_worker.claim_message("s11-b") == mid
    sender = delivery(AsyncMock(side_effect=DeliveryError("wecom_send_unknown", uncertain=True)))
    await rag_worker.maybe_send_progress(mid, "s11-b", sender, after_seconds=0)
    row = await saved(factory, mid)
    assert row.progress_state == "unknown"
    assert row.progress_error == "wecom_send_unknown"
    assert row.state == "processing" and row.send_state == "pending"
    await rag_worker.maybe_send_progress(mid, "s11-b", sender, after_seconds=0)
    sender.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_progress_skipped_when_answer_or_send_already_exists(sqlite_worker, monkeypatch):
    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user, content="虚构问题")
    assert await rag_worker.claim_message("s11-c") == mid
    async with factory() as session, session.begin():
        row = await session.get(rag_worker.WeComInboundMessage, mid)
        row.answer = {"text": "最终答案", "outcome": "answered", "sources": [], "plain_text": "最终答案"}
    sender = delivery()
    await rag_worker.maybe_send_progress(mid, "s11-c", sender, after_seconds=0)
    sender.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_message_sends_progress_after_threshold(sqlite_worker, monkeypatch):
    from test_s04_observability_persistence_sqlite import enqueue

    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user, content="虚构慢问题")
    assert await rag_worker.claim_message("s11-slow") == mid
    sender = delivery()

    async def slow_process(*args, **kwargs):
        await asyncio.sleep(0.15)
        async with factory() as session, session.begin():
            row = await session.get(rag_worker.WeComInboundMessage, mid)
            row.state, row.send_state = "completed", "sent"

    monkeypatch.setattr(rag_worker, "_process_message", slow_process)
    monkeypatch.setattr(rag_worker, "get_settings", lambda: SimpleNamespace(
        we_com_tenant_id=tenant, rag_progress_after_seconds=0.02,
        rag_processing_deadline_seconds=5))
    await rag_worker.process_message(mid, "s11-slow", sender)
    sender.send_text.assert_awaited_once()
    row = await saved(factory, mid)
    assert row.progress_state == "sent"
    assert row.send_state == "sent"


@pytest.mark.asyncio
async def test_progress_task_is_cancelled_for_fast_answer(sqlite_worker, monkeypatch):
    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user, content="虚构快问题")
    assert await rag_worker.claim_message("s11-d") == mid
    sender = delivery()

    async def quick_process(*args, **kwargs):
        await asyncio.sleep(0.02)
        async with factory() as session, session.begin():
            row = await session.get(rag_worker.WeComInboundMessage, mid)
            row.state, row.send_state = "completed", "sent"

    monkeypatch.setattr(rag_worker, "_process_message", quick_process)
    await rag_worker.process_message(mid, "s11-d", sender)
    sender.send_text.assert_not_awaited()
    row = await saved(factory, mid)
    assert row.progress_state == "none"


@pytest.mark.asyncio
async def test_admin_summary_marks_unknown_for_manual_recovery(sqlite_worker, monkeypatch):
    from app.api import admin_qa

    monkeypatch.setattr(admin_qa, "content_expired", lambda message: False)
    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user, content="虚构问题")
    async with factory() as session, session.begin():
        row = await session.get(rag_worker.WeComInboundMessage, mid)
        row.state, row.send_state = "failed", "unknown"
        row.answer = {"text": "最终答案", "outcome": "answered", "sources": [], "plain_text": "最终答案"}
        row.progress_state, row.progress_attempt_count = "sent", 1
    row = await saved(factory, mid)
    item = admin_qa.summary_item(row, "虚构员工")
    assert item["manual_recovery_required"] is True
    assert item["progress_state"] == "sent"
    assert item["progress_attempt_count"] == 1
