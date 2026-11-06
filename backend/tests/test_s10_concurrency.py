"""S10 有界并发、同会话顺序、公平领取、恢复和摄取隔离子测试。"""

import asyncio
import os
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

pytest.importorskip("aiosqlite")

from sqlalchemy import select  # noqa: E402

from test_s04_observability_persistence_sqlite import (  # noqa: E402
    enqueue, naive_now, saved, sqlite_worker,
)
from test_rag_diagnostics_persistence import worker_db  # noqa: E402

from app import rag_worker, worker  # noqa: E402
from app.models import ConversationState, User, UserStatus, WeComInboundMessage  # noqa: E402


async def add_user(factory, tenant, *, suffix="second"):
    uid = uuid4()
    async with factory() as session, session.begin():
        session.add(User(id=uid, tenant_id=tenant, external_id=f"s10-{suffix}",
                         display_name=f"虚构{suffix}", wecom_userid=f"fixture-{suffix}",
                         clearance_level=0, status=UserStatus.ACTIVE))
    return uid


@pytest.mark.asyncio
async def test_same_conversation_ordered_but_different_conversations_parallel(sqlite_worker):
    factory, tenant, user1 = sqlite_worker
    user2 = await add_user(factory, tenant)
    now = naive_now()
    m1 = await enqueue(factory, tenant, user1, content="m1",
                       received_at=now - timedelta(seconds=30), wecom_userid="fixture")
    m2 = await enqueue(factory, tenant, user1, content="m2",
                       received_at=now - timedelta(seconds=20), wecom_userid="fixture")
    m3 = await enqueue(factory, tenant, user2, content="m3",
                       received_at=now - timedelta(seconds=10), wecom_userid="fixture-second")

    first = await rag_worker.claim_message("s10-a")
    second = await rag_worker.claim_message("s10-b")
    assert first == m1
    # 同一员工已有处理中消息，不能并行领取 m2；另一员工可并行领取 m3。
    assert second == m3

    async with factory() as session, session.begin():
        row = await session.get(WeComInboundMessage, m1)
        row.state, row.worker_id, row.lease_expires_at = "completed", None, None

    third = await rag_worker.claim_message("s10-c")
    assert third == m2
    assert (await saved(factory, m2)).state == "processing"


@pytest.mark.asyncio
async def test_fairness_prefers_user_not_just_served(sqlite_worker):
    factory, tenant, user1 = sqlite_worker
    user2 = await add_user(factory, tenant)
    now = naive_now()
    m1 = await enqueue(factory, tenant, user1, content="m1",
                       received_at=now - timedelta(seconds=30), wecom_userid="fixture")
    m2 = await enqueue(factory, tenant, user1, content="m2",
                       received_at=now - timedelta(seconds=20), wecom_userid="fixture")
    m3 = await enqueue(factory, tenant, user2, content="m3",
                       received_at=now - timedelta(seconds=10), wecom_userid="fixture-second")

    assert await rag_worker.claim_message("s10-fair-a") == m1
    async with factory() as session, session.begin():
        row = await session.get(WeComInboundMessage, m1)
        row.state, row.worker_id, row.lease_expires_at = "completed", None, None

    # user1 刚被服务过；即使 m2 更早，也应先给从未被服务的 user2。
    assert await rag_worker.claim_message("s10-fair-b") == m3
    async with factory() as session, session.begin():
        row = await session.get(WeComInboundMessage, m3)
        row.state, row.worker_id, row.lease_expires_at = "completed", None, None
    assert await rag_worker.claim_message("s10-fair-c") == m2


@pytest.mark.asyncio
async def test_lease_recovery_reclaims_expired_processing(sqlite_worker):
    factory, tenant, user = sqlite_worker
    mid = await enqueue(factory, tenant, user)
    assert await rag_worker.claim_message("s10-recover-a") == mid
    assert await rag_worker.claim_message("s10-recover-b") is None
    async with factory() as session, session.begin():
        row = await session.get(WeComInboundMessage, mid)
        row.lease_expires_at = naive_now() - timedelta(seconds=1)
    assert await rag_worker.claim_message("s10-recover-b") == mid
    row = await saved(factory, mid)
    assert row.attempt_count == 2 and row.state == "processing"


@pytest.mark.asyncio
async def test_worker_loop_is_bounded_and_slow_tasks_do_not_block_others(monkeypatch):
    tasks = [uuid4() for _ in range(5)]
    cursor = {"index": 0}
    active = {"count": 0, "max": 0}
    started = []

    async def claim(_worker_id):
        if cursor["index"] >= len(tasks):
            return None
        value = tasks[cursor["index"]]
        cursor["index"] += 1
        return value

    async def process(message_id, _worker_id, _delivery):
        active["count"] += 1
        active["max"] = max(active["max"], active["count"])
        started.append(message_id)
        await asyncio.sleep(0.15)
        active["count"] -= 1

    monkeypatch.setattr(rag_worker, "purge_message_content", AsyncMock())
    stop = asyncio.Event()
    runner = asyncio.create_task(rag_worker.run_worker_loop(
        "s10-loop", None, concurrency=3, stop_event=stop,
        claim=claim, process=process))
    await asyncio.sleep(0.08)
    assert len(started) >= 2
    assert active["max"] >= 2
    stop.set()
    await asyncio.wait_for(runner, timeout=2)
    assert active["max"] <= 3


@pytest.mark.asyncio
async def test_ingestion_cpu_work_runs_outside_event_loop():
    ticks = {"count": 0}

    def blocking():
        time.sleep(0.12)
        return 7

    async def ticker():
        for _ in range(20):
            ticks["count"] += 1
            await asyncio.sleep(0.005)

    ticker_task = asyncio.create_task(ticker())
    result = await worker._in_ingestion_thread(blocking)
    await ticker_task
    assert result == 7
    assert ticks["count"] >= 5


@pytest.mark.skipif(os.getenv("SAGTRAG_RUN_DB_TESTS") != "1",
                    reason="requires explicit opt-in to local PostgreSQL fixtures")
@pytest.mark.asyncio
async def test_postgres_two_workers_do_not_double_claim(worker_db):
    factory, tenant, user = worker_db
    mid = await enqueue(factory, tenant, user, content="S10-PG-claim")
    results = await asyncio.gather(
        rag_worker.claim_message("s10-pg-a"),
        rag_worker.claim_message("s10-pg-b"),
    )
    assert [item for item in results if item is not None] == [mid]
