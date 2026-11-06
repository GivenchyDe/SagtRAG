"""S08 有限会话状态：纯补全、持久化隔离、顺序和重复消息测试。"""

from datetime import datetime, timedelta, timezone
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

from app import conversation, rag_worker  # noqa: E402
from app.intent_router import IntentResult  # noqa: E402
from app.models import ConversationState, User, UserStatus  # noqa: E402
from app.rag import RagAnswer  # noqa: E402


def knowledge_intent():
    return IntentResult("knowledge_query", "knowledge_query", 1.0, "rule", None,
                        "fixture_rule", None, 1, None, "fixture-policy")


async def snapshot(factory, tenant, user):
    async with factory() as session:
        return await conversation.load_snapshot(session, tenant_id=tenant, user_id=user)


async def state_row(factory, tenant, user):
    async with factory() as session:
        return await session.scalar(select(ConversationState).where(
            ConversationState.tenant_id == tenant, ConversationState.user_id == user))


async def run_turn(factory, tenant, user, content, worker_id, delivery,
                   *, wecom_userid="fixture"):
    mid = await enqueue(factory, tenant, user, content=content, wecom_userid=wecom_userid)
    assert await rag_worker.claim_message(worker_id) == mid
    await rag_worker.process_message(mid, worker_id, delivery)
    return await saved(factory, mid)


def test_full_new_topic_without_marker_does_not_reuse_old_context():
    now = naive_now()
    state = conversation.ConversationSnapshot(
        topic="请假", confirmed={"domain": "请假"}, version=1,
        expires_at=now + timedelta(minutes=60))
    result = conversation.resolve("公司年会在哪里", state, now=now, clarify_message="CLARIFY")
    assert result.topic is None
    assert result.query == "公司年会在哪里"
    assert result.source == "new"
    assert result.should_persist is True


@pytest.mark.asyncio
async def test_short_answers_correction_and_topic_change(sqlite_worker, monkeypatch):
    factory, tenant, user = sqlite_worker
    calls = []

    async def answer(session, principal, question, *, diagnostics):
        calls.append(question)
        diagnostics.update({"diagnostic_code": "answered", "outcome": "answered"})
        return RagAnswer("虚构答案", "answered", diagnostic_code="answered")

    monkeypatch.setattr(rag_worker, "detect_intent", AsyncMock(return_value=knowledge_intent()))
    monkeypatch.setattr(rag_worker, "answer_question", answer)
    delivery = SimpleNamespace(send_text=AsyncMock(return_value="fixture-msg"))

    first = await run_turn(factory, tenant, user, "请假流程", "s08-a", delivery)
    assert first.content == "请假流程"
    assert first.diagnostics["conversation"]["topic"] == "请假"
    assert first.diagnostics["conversation"]["applied"] is True

    second = await run_turn(factory, tenant, user, "病假", "s08-b", delivery)
    assert second.content == "病假"
    assert calls[-1] == "病假请假"
    assert second.diagnostics["conversation"]["topic"] == "病假请假"
    assert second.diagnostics["conversation"]["query_completed"] is True
    row = await state_row(factory, tenant, user)
    assert row.confirmed_info["leave_type"] == "病假"
    assert row.confirmed_info["domain"] == "请假"

    third = await run_turn(factory, tenant, user, "需要什么材料", "s08-c", delivery)
    assert calls[-1] == "需要什么材料 病假请假"
    assert third.diagnostics["conversation"]["source"] == "continued"

    corrected = await run_turn(factory, tenant, user, "不是病假，是事假", "s08-d", delivery)
    assert calls[-1] == "事假请假"
    assert corrected.diagnostics["conversation"]["correction"] is True
    assert corrected.diagnostics["conversation"]["topic"] == "事假请假"

    switched = await run_turn(factory, tenant, user, "换个话题，报销流程", "s08-e", delivery)
    assert calls[-1] == "报销流程"
    assert switched.diagnostics["conversation"]["source"] == "topic_changed"
    assert switched.diagnostics["conversation"]["topic"] == "报销"

    row = await state_row(factory, tenant, user)
    assert row is not None and row.version == 5
    assert row.last_message_id == switched.id
    assert len(calls) == 5  # 每轮都重新进入检索/生成，不缓存历史答案作为事实


@pytest.mark.asyncio
async def test_clarification_expiry_and_pending_fields(sqlite_worker, monkeypatch):
    factory, tenant, user = sqlite_worker
    calls = []

    async def answer(session, principal, question, *, diagnostics):
        calls.append(question)
        diagnostics.update({"diagnostic_code": "answered", "outcome": "answered"})
        return RagAnswer("虚构答案", "answered", diagnostic_code="answered")

    monkeypatch.setattr(rag_worker, "detect_intent", AsyncMock(return_value=knowledge_intent()))
    monkeypatch.setattr(rag_worker, "answer_question", answer)
    delivery = SimpleNamespace(send_text=AsyncMock(return_value="fixture-msg"))

    # 无主题的短问题先固定澄清，并把待补字段保存下来。
    clarify = await run_turn(factory, tenant, user, "需要什么材料", "s08-clarify", delivery)
    assert "补充" in clarify.answer["text"]
    assert clarify.diagnostics["diagnostic_code"] == "conversation_clarify"
    assert clarify.diagnostics["conversation"]["pending_fields"] == ["topic"]
    row = await state_row(factory, tenant, user)
    assert row.pending_fields == ["topic"] and row.pending_prompt
    assert row.pending_prompt == clarify.answer["text"]
    assert row.confirmed_info == {}

    # 下一轮短答只补当前待补字段，不重复追问。
    resolved = await run_turn(factory, tenant, user, "病假", "s08-resolve", delivery)
    assert resolved.diagnostics["conversation"]["source"] == "pending_resolved"
    assert resolved.diagnostics["conversation"]["pending_fields"] == []
    assert resolved.diagnostics["conversation"]["topic"] == "病假请假"
    row = await state_row(factory, tenant, user)
    assert row.pending_prompt is None

    # 过期状态被忽略；没有主题的短问题重新进入固定澄清，而不是套用旧主题。
    async with factory() as session, session.begin():
        current = await session.scalar(select(ConversationState).where(
            ConversationState.tenant_id == tenant, ConversationState.user_id == user).with_for_update())
        current.expires_at = naive_now() - timedelta(minutes=1)
    expired = await run_turn(factory, tenant, user, "需要什么材料", "s08-expired", delivery)
    assert expired.diagnostics["conversation"]["source"] == "clarify"
    assert expired.diagnostics["diagnostic_code"] == "conversation_clarify"
    row = await state_row(factory, tenant, user)
    assert row.topic is None and row.pending_fields == ["topic"]


@pytest.mark.asyncio
async def test_duplicate_message_does_not_advance_and_conversations_are_isolated(sqlite_worker, monkeypatch):
    factory, tenant, user = sqlite_worker
    second_user = uuid4()

    async def answer(session, principal, question, *, diagnostics):
        diagnostics.update({"diagnostic_code": "answered", "outcome": "answered"})
        return RagAnswer("虚构答案", "answered", diagnostic_code="answered")

    monkeypatch.setattr(rag_worker, "detect_intent", AsyncMock(return_value=knowledge_intent()))
    monkeypatch.setattr(rag_worker, "answer_question", answer)
    delivery = SimpleNamespace(send_text=AsyncMock(return_value="fixture-msg"))

    async with factory() as session, session.begin():
        session.add(User(id=second_user, tenant_id=tenant, external_id="s08-second",
                         display_name="虚构第二员工", wecom_userid="fixture-second",
                         clearance_level=0, status=UserStatus.ACTIVE))

    first = await run_turn(factory, tenant, user, "请假流程", "s08-iso-a", delivery)
    row = await state_row(factory, tenant, user)
    version_before = row.version

    # 同一消息再次推进必须被 last_message_id 拦住。
    snap = await snapshot(factory, tenant, user)
    resolution = conversation.resolve(
        first.content, snap, now=naive_now(), clarify_message="CLARIFY")
    async with factory() as session, session.begin():
        applied = await conversation.apply_resolution(
            session, first, resolution, now=naive_now(), ttl_minutes=60)
    assert applied is False
    row = await state_row(factory, tenant, user)
    assert row.version == version_before

    # 第二员工没有第一员工的“请假”主题，不共享状态。
    other = await run_turn(factory, tenant, second_user, "病假", "s08-iso-b", delivery,
                           wecom_userid="fixture-second")
    other_row = await state_row(factory, tenant, second_user)
    assert other_row is not None
    assert other_row.topic == "病假请假"
    assert other.diagnostics["conversation"]["topic"] == "病假请假"
    assert other.diagnostics["conversation"]["source"] == "new"
    assert row.topic == "请假"
    assert row.last_message_id == first.id
