"""S12 重排模型切换、shadow对照和阈值校准测试（模型替身）。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app import rag
from app.rerank_calibration import choose_threshold, evaluate_threshold
from app.retrieval import Candidate


def candidate(content: str):
    return Candidate(uuid4(), uuid4(), uuid4(), "t", content, 0)


def settings(**overrides):
    values = dict(
        rerank_url="https://rerank.fixture/legacy",
        rerank_target_url="https://rerank.fixture/target",
        rerank_model="old-rerank",
        rerank_target_model="new-rerank",
        rerank_rollout_mode="legacy",
        rag_min_relevance=0.25,
        rerank_target_min_relevance=0.40,
        rerank_target_threshold_calibrated=True,
        rag_context_limit=5,
        embedding_api_key=SecretStr("fixture"),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def install_transport(monkeypatch, seen, scores_by_model):
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload["model"])
        scores = scores_by_model.get(payload["model"], [0.9])
        return httpx.Response(200, json={"output": {"results": [
            {"index": index, "relevance_score": score}
            for index, score in enumerate(scores)
        ]}})

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(rag.http_clients, "get_client",
                        lambda: original(transport=transport))


@pytest.mark.asyncio
async def test_legacy_is_default_and_unchanged(monkeypatch):
    seen = []
    install_transport(monkeypatch, seen, {"old-rerank": [0.9, 0.1]})
    monkeypatch.setattr(rag, "get_settings", lambda: settings())
    diagnostics = {}
    ranked = await rag.rerank("问题", [candidate("a"), candidate("b")], diagnostics=diagnostics)
    assert seen == ["old-rerank"]
    assert [item.content for item in ranked] == ["a"]
    assert diagnostics["rerank_mode"] == "legacy"
    assert diagnostics["rerank_model"] == "old-rerank"
    assert diagnostics["rerank_threshold"] == 0.25


@pytest.mark.asyncio
async def test_target_mode_requires_calibration(monkeypatch):
    seen = []
    install_transport(monkeypatch, seen, {"old-rerank": [0.9]})
    monkeypatch.setattr(rag, "get_settings",
                        lambda: settings(rerank_rollout_mode="target",
                                         rerank_target_threshold_calibrated=False))
    diagnostics = {}
    ranked = await rag.rerank("问题", [candidate("a")], diagnostics=diagnostics)
    assert seen == ["old-rerank"]
    assert diagnostics["rerank_target_blocked"] == "threshold_not_calibrated"
    assert ranked[0].content == "a"


@pytest.mark.asyncio
async def test_target_model_uses_calibrated_threshold(monkeypatch):
    seen = []
    install_transport(monkeypatch, seen, {"new-rerank": [0.9, 0.3]})
    monkeypatch.setattr(rag, "get_settings",
                        lambda: settings(rerank_rollout_mode="target"))
    diagnostics = {}
    ranked = await rag.rerank("问题", [candidate("a"), candidate("b")], diagnostics=diagnostics)
    assert seen == ["new-rerank"]
    assert [item.content for item in ranked] == ["a"]
    assert diagnostics["rerank_threshold"] == 0.40


@pytest.mark.asyncio
async def test_shadow_records_target_without_changing_user_order(monkeypatch):
    seen = []
    install_transport(monkeypatch, seen, {
        "old-rerank": [0.5, 0.1],
        "new-rerank": [0.1, 0.9],
    })
    monkeypatch.setattr(rag, "get_settings",
                        lambda: settings(rerank_rollout_mode="shadow"))
    diagnostics = {}
    ranked = await rag.rerank("问题", [candidate("legacy"), candidate("target")],
                              diagnostics=diagnostics)
    assert seen == ["old-rerank", "new-rerank"]
    assert [item.content for item in ranked] == ["legacy"]
    assert diagnostics["rerank_shadow_model"] == "new-rerank"
    assert len(diagnostics["rerank_shadow_scores"]) == 2
    assert diagnostics["rerank_shadow_selected_ids"]


def test_threshold_calibration_prefers_recall_then_precision():
    samples = [
        {"scores": {"a": 0.9, "b": 0.3, "c": 0.1}, "relevant": ["a"]},
        {"scores": {"d": 0.6, "e": 0.2}, "relevant": ["d"]},
    ]
    assert evaluate_threshold(samples, 0.25).relevant_selected == 2
    chosen = choose_threshold(samples, [0.1, 0.25, 0.5, 0.8], min_recall=1.0)
    assert chosen.threshold >= 0.25
    assert chosen.recall == 1.0
