"""S09 统一期限、连接复用、流式超时和生成输入预算测试。"""

import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app import http_clients, rag
from app.deadline import (Deadline, DeadlineExceeded, call_timeout, check_current,
                          reset_current_deadline, set_current_deadline)
from app.retrieval import Candidate


def test_deadline_uses_remaining_budget_and_caps_each_stage():
    clock = [0.0]
    deadline = Deadline(10.0, clock=lambda: clock[0])
    assert deadline.remaining() == 10.0
    assert deadline.timeout(3.0) == 3.0
    clock[0] = 9.0
    assert deadline.timeout(5.0) == 1.0
    clock[0] = 10.0
    with pytest.raises(DeadlineExceeded):
        deadline.timeout(5.0)


@pytest.mark.asyncio
async def test_shared_http_client_is_reused_and_closed(monkeypatch):
    created = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.closed = False
            created.append(self)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(http_clients.httpx, "AsyncClient",
                        lambda **kwargs: FakeClient(**kwargs))
    http_clients.reset_client_for_tests()
    first = http_clients.get_client()
    second = http_clients.get_client()
    assert first is second and len(created) == 1
    await http_clients.close_client()
    assert first.closed is True
    third = http_clients.get_client()
    assert third is not first and len(created) == 2
    await http_clients.close_client()
    http_clients.reset_client_for_tests()


def test_generation_documents_dedupe_and_cap_without_truncating(monkeypatch):
    monkeypatch.setattr(rag, "get_settings",
                        lambda: SimpleNamespace(rag_generation_input_chars=15))
    first = Candidate(uuid4(), uuid4(), uuid4(), "t", "AAAAAA", 0)
    duplicate = Candidate(uuid4(), uuid4(), uuid4(), "t", "AAAAAA", 1)
    second = Candidate(uuid4(), uuid4(), uuid4(), "t", "BBBBBB", 2)
    third = Candidate(uuid4(), uuid4(), uuid4(), "t", "CCCCCC", 3)
    selected = rag._generation_documents([first, duplicate, second, third])
    assert [item.content for item in selected] == ["AAAAAA", "BBBBBB"]
    assert selected[0].content == first.content  # 完整正文，不做截断

    huge = Candidate(uuid4(), uuid4(), uuid4(), "t", "X" * 40, 4)
    assert rag._generation_documents([huge])[0].content == "X" * 40


class EndlessSSE(httpx.AsyncByteStream):
    async def __aiter__(self):
        while True:
            yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_continuous_stream_is_bounded_by_deadline(monkeypatch):
    settings = SimpleNamespace(
        generation_base_url="https://fixture.invalid/v1",
        generation_model="fixture-model",
        generation_max_tokens=128,
        embedding_api_key=SecretStr("fixture"),
    )
    monkeypatch.setattr(rag, "get_settings", lambda: settings)
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, stream=EndlessSSE(), headers={"content-type": "text/event-stream"}))
    monkeypatch.setattr(rag.http_clients, "get_client",
                        lambda: original(transport=transport))
    deadline = Deadline(0.2)
    token = set_current_deadline(deadline)
    diagnostics = {}
    started = time.perf_counter()
    try:
        with pytest.raises(Exception) as result:
            await rag.model_json("fixture", {"question": "fixture"}, diagnostics=diagnostics)
        assert getattr(result.value, "code", None) == "generation_provider_error"
        assert time.perf_counter() - started < 2.0
        assert diagnostics["generation_first_content_ms"] is not None
    finally:
        reset_current_deadline(token)


@pytest.mark.asyncio
async def test_external_cancellation_propagates_from_stream(monkeypatch):
    settings = SimpleNamespace(
        generation_base_url="https://fixture.invalid/v1",
        generation_model="fixture-model",
        generation_max_tokens=128,
        embedding_api_key=SecretStr("fixture"),
    )
    monkeypatch.setattr(rag, "get_settings", lambda: settings)
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, stream=EndlessSSE(), headers={"content-type": "text/event-stream"}))
    monkeypatch.setattr(rag.http_clients, "get_client",
                        lambda: original(transport=transport))
    task = asyncio.create_task(rag.model_json("fixture", {"question": "fixture"}))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_generation_request_uses_configured_output_limit_and_shared_client(monkeypatch):
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        body = 'data: {"choices":[{"delta":{"content":"{\\"refused\\":true,\\"claims\\":[]}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    settings = SimpleNamespace(
        generation_base_url="https://fixture.invalid/v1",
        generation_model="fixture-model",
        generation_max_tokens=777,
        embedding_api_key=SecretStr("fixture"),
    )
    monkeypatch.setattr(rag, "get_settings", lambda: settings)
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(rag.http_clients, "get_client",
                        lambda: original(transport=transport))
    assert (await rag.model_json("fixture", {"question": "fixture"}))["refused"] is True
    assert seen[0]["max_tokens"] == 777


@pytest.mark.asyncio
async def test_exhausted_deadline_returns_service_error_without_calling_retrieval():
    deadline = Deadline(0.01)
    token = set_current_deadline(deadline)
    await asyncio.sleep(0.02)
    principal = SimpleNamespace(permission_version=1)
    try:
        answer = await rag.answer_question(None, principal, "虚构问题")
    finally:
        reset_current_deadline(token)
    assert answer.outcome == "service_error"
    assert answer.diagnostic_code == "processing_deadline_exceeded"


def test_call_timeout_uses_current_deadline():
    assert call_timeout(5.0) == 5.0
    token = set_current_deadline(Deadline(2.0))
    try:
        assert call_timeout(5.0) <= 2.0
    finally:
        reset_current_deadline(token)
    expired = Deadline(0.01, clock=lambda: 1.0)
    token = set_current_deadline(expired)
    try:
        with pytest.raises(DeadlineExceeded):
            check_current("after_expiry")
    finally:
        reset_current_deadline(token)
