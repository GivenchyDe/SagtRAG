"""S04 细分诊断码、计时辅助与历史兼容的离线定向测试。

全部使用虚构数据和模型/发送替身，不访问数据库、真实模型或企微。
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app import observability, rag
from app.api import admin_qa
from app.rag import NO_ANSWER, RagAnswer, validate_answer
from app.retrieval import Candidate


def source(content="虚构星河公司的报销期限为 30 天。"):
    return Candidate(uuid4(), uuid4(), uuid4(), "虚构报销制度", content, 0)


def draft(item, *, quote="报销期限为 30 天", chunk_id=None):
    return {"refused": False, "claims": [{"text": "报销期限为 30 天。", "citations": [
        {"chunk_id": str(chunk_id or item.id), "quote": quote},
    ]}]}


def test_validation_diagnostic_codes_keep_outcome_and_text():
    item = source()
    cases = [
        ({**draft(item), "unexpected_control": "不属于允许元信息"}, observability.SCHEMA_INVALID),
        ({"refused": False, "claims": [{"text": 123, "citations": [
            {"chunk_id": str(item.id), "quote": "报销期限为 30 天"}]}]}, observability.FIELD_TYPE_INVALID),
        (draft(item, chunk_id="not-a-uuid"), observability.CHUNK_ID_INVALID),
        (draft(item, chunk_id=uuid4()), observability.SOURCE_NOT_WHITELISTED),
        (draft(item, quote="  "), observability.QUOTE_TOO_SHORT),
        (draft(item, quote="报销期限为 90 天"), observability.QUOTE_MISMATCH),
    ]
    for raw, expected in cases:
        answer = validate_answer(raw, [item])
        assert answer.outcome == "invalid_citation"
        assert answer.text == NO_ANSWER
        assert answer.diagnostic_code == expected
        assert answer.sources == []

    harmless = validate_answer({**draft(item), "extra_note": "无害额外字段"}, [item])
    assert harmless.outcome == "answered"
    assert harmless.detail["ignored_metadata_fields"] == ["extra_note"]


def test_answer_too_long_code_and_counts_without_raw_text():
    item = source("报销期限为 30 天。" + "虚构补充说明" * 80)
    quote = "报销期限为 30 天"
    raw = {"refused": False, "claims": [
        {"text": "🙂" * 150, "citations": [{"chunk_id": str(item.id), "quote": quote}]},
        {"text": "🙂" * 150, "citations": [{"chunk_id": str(item.id), "quote": quote}]},
        {"text": "🙂" * 150, "citations": [{"chunk_id": str(item.id), "quote": quote}]},
        {"text": "🙂" * 150, "citations": [{"chunk_id": str(item.id), "quote": quote}]},
    ]}
    answer = validate_answer(raw, [item])
    assert answer.outcome == "answer_too_long"
    assert answer.diagnostic_code == observability.ANSWER_TOO_LONG
    assert answer.detail["rendered_bytes"] > answer.detail["limit_bytes"] == 2000


def test_refusal_and_empty_claims_are_distinguished():
    item = source()
    refused = validate_answer({"refused": True, "claims": [], "refusal_reason": "缺少差旅标准"}, [item])
    assert refused.outcome == "no_answer" and refused.diagnostic_code == observability.MODEL_REFUSED
    empty = validate_answer({"refused": False, "claims": []}, [item])
    assert empty.outcome == "no_answer" and empty.diagnostic_code == observability.EMPTY_CLAIMS


def test_valid_answer_keeps_behavior_and_records_safe_counts():
    item = source()
    answer = validate_answer(draft(item), [item])
    assert answer.outcome == "answered"
    assert answer.diagnostic_code == "answered"
    assert answer.detail["claim_count"] == 1
    assert answer.detail["source_count"] == 1
    assert answer.detail["claim_citation_count"] == 1
    assert answer.detail["rendered_bytes"] == len(answer.text.encode())
    assert "[1]" in answer.text and answer.plain_text == "报销期限为 30 天。"
    assert "报销期限为 30 天" not in json.dumps(answer.detail, ensure_ascii=False)


def test_policy_snapshot_uses_allowlist_and_never_persists_secrets():
    settings = SimpleNamespace(
        generation_model="fixture-generation", embedding_model="fixture-embedding",
        rerank_model="fixture-rerank", rag_recall_limit=30, rag_context_limit=5,
        rag_min_relevance=0.25, intent_rules_version="rules-v2", intent_prompt_version="prompt-v1",
        intent_routing_enabled=True, intent_model=None, embedding_api_key=SecretStr("secret-key"),
        database_url="postgresql://user:password@fixture/db", we_com_secret=SecretStr("wecom-secret"),
    )
    snapshot = observability.policy_snapshot(settings)
    encoded = json.dumps(snapshot, ensure_ascii=False)
    assert snapshot["config"]["generation_model"] == "fixture-generation"
    assert snapshot["config"]["rag_min_relevance"] == 0.25
    assert snapshot["source_whitelist"]["predicate"] == "visible_chunks"
    assert len(snapshot["config_sha256"]) == 64
    for forbidden in ("secret-key", "password", "wecom-secret", "postgresql://"):
        assert forbidden not in encoded


def test_attempt_helpers_preserve_history_and_unknown_values():
    diagnostics = {}
    started = datetime.now(timezone.utc)
    observability.append_attempt(diagnostics, number=1, kind="processing", started_at=started)
    first_started = diagnostics["attempts"][0]["started_at"]
    observability.append_attempt(diagnostics, number=2, kind="processing", started_at=started + timedelta(seconds=1))
    assert diagnostics["attempts"][0]["state"] == "incomplete"
    assert diagnostics["attempts"][0]["started_at"] == first_started
    assert [item["number"] for item in diagnostics["attempts"]] == [1, 2]
    observability.finish_open_attempt(diagnostics, outcome="answered", duration_ms=7, finished_at=started)
    second = diagnostics["attempts"][-1]
    assert (second["state"], second["outcome"], second["duration_ms"]) == ("completed", "answered", 7)

    missing = {}
    observability.finish_open_attempt(missing, outcome="processing_exception", error_code="fixture_error")
    record = missing["attempts"][0]
    assert record["started_at"] is None and "duration_ms" not in record
    assert record["state"] == "failed" and record["error_code"] == "fixture_error"

    for number in range(1, 13):
        observability.append_attempt(missing, number=number, kind="processing", started_at=started)
    assert len(missing["attempts"]) == observability.MAX_RECORDED_ATTEMPTS


def test_completion_time_and_failure_outcomes_do_not_fake_zero():
    diagnostics = {}
    observability.record_completion_time(diagnostics, received_at=None, completed_at=datetime.now(timezone.utc))
    assert "end_to_end_ms" not in diagnostics
    now = datetime.now(timezone.utc)
    observability.record_completion_time(diagnostics, received_at=now, completed_at=now + timedelta(milliseconds=12))
    assert diagnostics["end_to_end_ms"] == 12
    assert observability.is_failure_outcome("service_error")
    assert observability.is_failure_outcome("invalid_citation")
    assert not observability.is_failure_outcome("no_answer")


class _SlowSSE(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"{\\"refused\\": true, \\"claims\\": []}"}}]}\n\n'
        await asyncio.sleep(0.02)
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_model_json_records_first_content_separately(monkeypatch):
    settings = SimpleNamespace(generation_base_url="https://fixture.invalid/v1",
                               generation_model="fixture-model", embedding_api_key=SecretStr("fixture"))
    monkeypatch.setattr(rag, "get_settings", lambda: settings)
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, stream=_SlowSSE(), headers={"content-type": "text/event-stream"}))
    monkeypatch.setattr(rag.http_clients, "get_client", lambda: original(transport=transport))
    diagnostics = {}
    started = time.perf_counter()
    result = await rag.model_json("fixture system", {"question": "fixture"}, diagnostics=diagnostics)
    total_ms = round((time.perf_counter() - started) * 1000)
    assert result == {"refused": True, "claims": []}
    assert 0 <= diagnostics["generation_first_content_ms"] < total_ms


class _StopOnlySSE(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_model_json_does_not_fake_ttft_when_no_content(monkeypatch):
    settings = SimpleNamespace(generation_base_url="https://fixture.invalid/v1",
                               generation_model="fixture-model", embedding_api_key=SecretStr("fixture"))
    monkeypatch.setattr(rag, "get_settings", lambda: settings)
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, stream=_StopOnlySSE(), headers={"content-type": "text/event-stream"}))
    monkeypatch.setattr(rag.http_clients, "get_client", lambda: original(transport=transport))
    diagnostics = {}
    with pytest.raises(RuntimeError):
        await rag.model_json("fixture system", {"question": "fixture"}, diagnostics=diagnostics)
    assert "generation_first_content_ms" not in diagnostics


@pytest.mark.asyncio
async def test_answer_question_success_records_stage_timings(monkeypatch):
    item = source()

    async def fake_retrieve(session, principal, question, *, diagnostics=None):
        return [item]

    async def fake_rerank(question, candidates, *, diagnostics=None):
        return candidates

    async def fake_model(system, data, *, diagnostics=None):
        diagnostics["generation_first_content_ms"] = 3
        return draft(item)

    async def fake_revalidate(session, principal, candidates):
        return True

    monkeypatch.setattr(rag, "get_settings", lambda: SimpleNamespace(
        generation_model="fixture-gen", rerank_model="fixture-rerank", rag_min_relevance=0.25))
    monkeypatch.setattr(rag, "retrieve", fake_retrieve)
    monkeypatch.setattr(rag, "rerank", fake_rerank)
    monkeypatch.setattr(rag, "model_json", fake_model)
    monkeypatch.setattr(rag, "revalidate_candidates", fake_revalidate)
    diagnostics = {}
    answer = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题", diagnostics=diagnostics)
    assert answer.outcome == "answered" and answer.diagnostic_code == "answered"
    assert diagnostics["outcome"] == "answered" and diagnostics["diagnostic_code"] == "answered"
    assert diagnostics["retrieval_ms"] >= 0 and diagnostics["rerank_ms"] >= 0
    assert diagnostics["generation_ms"] >= 0 and diagnostics["generation_first_content_ms"] == 3
    assert diagnostics["rag_total_ms"] >= 0
    assert diagnostics["diagnostic_detail"]["claim_count"] == 1


@pytest.mark.asyncio
async def test_answer_question_records_provider_error_and_elapsed_generation(monkeypatch):
    item = source()

    async def fake_retrieve(session, principal, question, *, diagnostics=None):
        return [item]

    async def fake_rerank(question, candidates, *, diagnostics=None):
        return candidates

    async def broken_model(system, data, *, diagnostics=None):
        raise RuntimeError("generation_provider_error")

    async def fake_revalidate(session, principal, candidates):
        return True

    monkeypatch.setattr(rag, "get_settings", lambda: SimpleNamespace(
        generation_model="fixture-gen", rerank_model="fixture-rerank", rag_min_relevance=0.25))
    monkeypatch.setattr(rag, "retrieve", fake_retrieve)
    monkeypatch.setattr(rag, "rerank", fake_rerank)
    monkeypatch.setattr(rag, "model_json", broken_model)
    monkeypatch.setattr(rag, "revalidate_candidates", fake_revalidate)
    diagnostics = {}
    answer = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题", diagnostics=diagnostics)
    assert answer.outcome == "service_error"
    assert diagnostics["error_code"] == "generation_provider_error"
    assert diagnostics["diagnostic_code"] == "generation_provider_error"
    assert diagnostics["generation_ms"] >= 0
    assert "generation_first_content_ms" not in diagnostics


@pytest.mark.asyncio
async def test_answer_question_no_candidates_is_observable(monkeypatch):
    async def fake_retrieve(session, principal, question, *, diagnostics=None):
        return []

    monkeypatch.setattr(rag, "get_settings", lambda: SimpleNamespace(
        generation_model="fixture-gen", rerank_model="fixture-rerank", rag_min_relevance=0.25))
    monkeypatch.setattr(rag, "retrieve", fake_retrieve)
    diagnostics = {}
    answer = await rag.answer_question(None, SimpleNamespace(permission_version=1), "虚构问题", diagnostics=diagnostics)
    assert answer.outcome == "no_answer" and answer.diagnostic_code == observability.NO_CANDIDATES
    assert diagnostics["diagnostic_code"] == observability.NO_CANDIDATES
    assert diagnostics["retrieval_ms"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,expected", [
    ("download", {"download_ms"}),
    ("convert", {"download_ms", "convert_ms"}),
    ("asr", {"download_ms", "convert_ms", "asr_ms"}),
])
async def test_voice_failure_keeps_started_stage_durations(monkeypatch, stage, expected):
    from app import voice

    diagnostics = {}

    async def download(delivery, media_id):
        if stage == "download":
            raise voice.VoiceError("voice_download_error", retryable=True)
        return b"fixture-audio"

    async def convert(audio):
        if stage == "convert":
            raise voice.VoiceError("voice_invalid_audio")
        return b"fixture-wav", 1.0

    async def transcribe(audio):
        if stage == "asr":
            raise voice.VoiceError("voice_asr_error", retryable=True)
        return "虚构语音问题"

    monkeypatch.setattr(voice, "get_settings", lambda: SimpleNamespace(asr_model="fixture"))
    monkeypatch.setattr(voice, "download_voice", download)
    monkeypatch.setattr(voice, "to_wav", convert)
    monkeypatch.setattr(voice, "transcribe_wav", transcribe)
    with pytest.raises(voice.VoiceError):
        await voice.transcribe_voice(None, "fixture", diagnostics)
    info = diagnostics["voice"]
    assert expected.issubset(info)
    assert "voice_ms" in info
    assert "asr_ms" not in info if stage != "asr" else "asr_ms" in info
    assert ("convert_ms" in info) is (stage in {"convert", "asr"})


def _message(**overrides):
    received = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    values = dict(
        id=uuid4(), employee_name="虚构员工", msg_type="text", content="虚构问题", question="虚构问题", state="completed",
        send_state="sent", outcome="answered", error_code=None, received_at=received,
        first_claimed_at=received + timedelta(seconds=3), completed_at=received + timedelta(seconds=13),
        attempt_count=1, send_attempt_count=1, content_expired=False, intent="knowledge_query",
        predicted_intent="knowledge_query", intent_confidence=1.0, intent_source="rule",
        route_ms=0, diagnostic_code="answered",
        timing={"end_to_end_ms": 13000, "queue_wait_ms": 3000, "processing_ms": 7000},
        answer={"outcome": "answered"}, diagnostics={},
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_admin_summary_splits_scopes_and_keeps_history_unknown(monkeypatch):
    monkeypatch.setattr(admin_qa, "get_settings", lambda: SimpleNamespace(rag_message_retention_days=7))
    message = _message()
    message.diagnostics = {"queue_wait_ms": 3000, "processing_ms": 7000, "send_ms": 400,
                           "diagnostic_code": "answered", "retrieval_ms": 120}
    result = admin_qa.summary_item(message, "虚构员工")
    assert result["timing"]["end_to_end_ms"] == 13000
    assert result["timing"]["queue_wait_ms"] == 3000
    assert result["timing"]["processing_ms"] == 7000
    assert result["timing"]["send_ms"] == 400
    assert result["diagnostic_code"] == "answered"
    assert result["first_claimed_at"] == message.first_claimed_at

    legacy = _message(first_claimed_at=None, completed_at=None, route_ms=None, diagnostic_code=None,
                      diagnostics={})
    legacy_result = admin_qa.summary_item(legacy, "虚构员工")
    assert legacy_result["timing"]["end_to_end_ms"] is None
    assert legacy_result["timing"]["queue_wait_ms"] is None
    assert legacy_result["diagnostic_code"] is None
    assert legacy_result["route_ms"] is None
    assert legacy_result["timing"]["generation_ms"] is None
