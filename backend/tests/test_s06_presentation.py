"""S06 表达与最终发送预算定向测试。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import rag
from app.rag import (
    LOW_RELEVANCE_TEXT, NO_CANDIDATES_TEXT, SERVICE_ERROR, validate_answer,
)
from app.retrieval import Candidate


def _candidate():
    return Candidate(uuid4(), uuid4(), uuid4(), "虚构制度" * 20, "报销期限为30天。", 0)


def _long_answer(item):
    claim = "🙂" * 150
    return {"refused": False, "claims": [
        {"text": claim, "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
        {"text": claim, "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
        {"text": claim, "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
        {"text": claim, "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
    ]}


def test_employee_budget_uses_plain_text_not_sources():
    item = _candidate()
    # 4×150 个 emoji 的 plain_text 已超过 2000 字节，员工和开发者都应拒绝；
    # 这里的断言确认预算字段按发送模式区分，而不是永远使用开发者全文。
    answer = validate_answer(_long_answer(item), [item], developer=False)
    assert answer.outcome == "answer_too_long"
    assert answer.detail["budget_scope"] == "employee_plain_text"
    assert "sent_bytes" in answer.detail


def test_employee_short_plain_text_is_not_rejected_by_developer_sources():
    item = _candidate()
    claim = "报销期限为30天。" * 10  # 100 个汉字，plain 约 300 字节
    raw = {"refused": False, "claims": [
        {"text": claim, "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
    ] * 4}
    employee = validate_answer(raw, [item], developer=False)
    assert employee.outcome == "answered"
    assert employee.detail["budget_scope"] == "employee_plain_text"
    assert employee.detail["sent_bytes"] <= 2000
    # 同一答案在开发者模式下包含长来源和编号预留，按最终发送版判断。
    developer = validate_answer(raw, [item], developer=True)
    assert developer.detail["budget_scope"] == "developer_with_sources_and_footer"
    assert developer.detail["sent_bytes"] == developer.detail["rendered_bytes"] + rag.DEVELOPER_FOOTER_RESERVE


@pytest.mark.asyncio
async def test_no_candidate_and_low_relevance_texts_are_distinct(monkeypatch):
    item = _candidate()

    async def no_retrieve(session, principal, question, *, diagnostics=None):
        return []

    monkeypatch.setattr(rag, "get_settings", lambda: SimpleNamespace(
        generation_model="fixture-gen", rerank_model="fixture-rerank", rag_min_relevance=0.25))
    monkeypatch.setattr(rag, "retrieve", no_retrieve)
    no_candidate = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题")
    assert no_candidate.text == NO_CANDIDATES_TEXT
    assert no_candidate.outcome == "no_answer"
    assert no_candidate.text != SERVICE_ERROR

    async def retrieve(session, principal, question, *, diagnostics=None):
        return [item]

    async def no_rerank(question, candidates, *, diagnostics=None):
        return []

    async def revalidate(session, principal, candidates):
        return True

    monkeypatch.setattr(rag, "retrieve", retrieve)
    monkeypatch.setattr(rag, "rerank", no_rerank)
    monkeypatch.setattr(rag, "revalidate_candidates", revalidate)
    low = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题")
    assert low.text == LOW_RELEVANCE_TEXT
    assert low.outcome == "low_relevance"
    assert low.text != no_candidate.text


def test_prompt_avoids_mechanical_clarifying_question():
    assert "不要机械追问" in rag.SYSTEM_PROMPT
    assert "第一句直接回答" in rag.SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_employee_mode_context_uses_plain_budget(monkeypatch):
    item = _candidate()

    async def retrieve(session, principal, question, *, diagnostics=None):
        return [item]

    async def rerank(question, candidates, *, diagnostics=None):
        return candidates

    async def revalidate(session, principal, candidates):
        return True

    claim = "报销规则" * 37 + "。"  # 150 字以内
    raw = {"refused": False, "claims": [
        {"text": claim, "citations": [{"chunk_id": str(item.id), "quote": "报销期限为30天"}]},
    ] * 4}

    async def model(system, data, *, diagnostics=None):
        return raw

    monkeypatch.setattr(rag, "get_settings", lambda: SimpleNamespace(
        generation_model="fixture-gen", rerank_model="fixture-rerank", rag_min_relevance=0.25))
    monkeypatch.setattr(rag, "retrieve", retrieve)
    monkeypatch.setattr(rag, "rerank", rerank)
    monkeypatch.setattr(rag, "revalidate_candidates", revalidate)
    monkeypatch.setattr(rag, "model_json", model)
    token = rag.set_developer_mode(False)
    try:
        answer = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题")
    finally:
        rag.reset_developer_mode(token)
    assert answer.outcome == "answered"
    assert answer.detail["budget_scope"] == "employee_plain_text"
    assert answer.detail["sent_bytes"] < answer.detail["rendered_bytes"]
