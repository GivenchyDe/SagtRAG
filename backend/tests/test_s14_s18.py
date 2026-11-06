"""S14/S18 结构邻块与版本清单的轻量回归。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import rag
from app.retrieval import Candidate
from app.version_manifest import behavior_manifest


def candidate(ordinal: int):
    return Candidate(uuid4(), uuid4(), uuid4(), "t", f"chunk-{ordinal}", ordinal, 0.8)


@pytest.mark.asyncio
async def test_neighbor_expansion_is_disabled_by_default(monkeypatch):
    item = candidate(1)
    monkeypatch.setattr(rag, "get_settings", lambda: SimpleNamespace(rag_neighbor_limit=0))
    assert await rag._expand_neighbors(None, SimpleNamespace(), [item], {}) == [item]


def test_manifest_contains_versions_without_secrets():
    settings = SimpleNamespace(
        embedding_model="embed", generation_model="gen", rerank_model="old",
        rerank_rollout_mode="legacy", rerank_target_model="new", intent_rules_version="rules-v3",
        intent_prompt_version="prompt-v2", rag_recall_limit=30, rag_context_limit=5,
        rag_neighbor_limit=0, rag_min_relevance=0.25,
    )
    manifest = behavior_manifest(settings)
    assert manifest["manifest_version"] == "sagtrag-behavior-1"
    assert manifest["models"]["rerank"] == "old"
    assert "api_key" not in str(manifest).lower()
    assert "url" not in str(manifest).lower()
