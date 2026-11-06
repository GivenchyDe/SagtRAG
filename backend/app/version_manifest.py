"""S18 可版本跟踪的行为/配置/索引模型清单；不包含端点、密钥或正文。"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.ingestion import CHUNKER_VERSION, PARSER_VERSION

MANIFEST_VERSION = "sagtrag-behavior-1"


def behavior_manifest(settings: Any | None = None) -> dict[str, object]:
    settings = settings or get_settings()
    return {
        "manifest_version": MANIFEST_VERSION,
        "parser_version": PARSER_VERSION,
        "chunker_version": CHUNKER_VERSION,
        "models": {
            "embedding": getattr(settings, "embedding_model", None),
            "generation": getattr(settings, "generation_model", None),
            "rerank": getattr(settings, "rerank_model", None),
            "rerank_mode": getattr(settings, "rerank_rollout_mode", None),
            "rerank_target": getattr(settings, "rerank_target_model", None),
            "intent_rules": getattr(settings, "intent_rules_version", None),
            "intent_prompt": getattr(settings, "intent_prompt_version", None),
        },
        "retrieval": {
            "recall_limit": getattr(settings, "rag_recall_limit", None),
            "context_limit": getattr(settings, "rag_context_limit", None),
            "neighbor_limit": getattr(settings, "rag_neighbor_limit", None),
            "min_relevance": getattr(settings, "rag_min_relevance", None),
        },
    }
