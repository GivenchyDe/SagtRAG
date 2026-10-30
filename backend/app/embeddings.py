"""Model Studio 文本向量服务适配器。"""

import math
from collections.abc import Awaitable, Callable

import httpx

from app.config import get_settings


async def embed_texts(
    texts: list[str],
    *,
    on_batch: Callable[[], Awaitable[None]] | None = None,
) -> list[list[float]]:
    """批量获取 1024 维向量并校验响应数量、维度与数值有效性。"""
    if not texts:
        return []
    settings = get_settings()
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=8.0)) as client:
        for start in range(0, len(texts), 10):
            batch = texts[start : start + 10]
            try:
                response = await client.post(
                    f"{str(settings.embedding_base_url).rstrip('/')}/embeddings",
                    headers={"Authorization": f"Bearer {settings.embedding_api_key.get_secret_value()}"},
                    json={
                        "model": settings.embedding_model,
                        "input": batch,
                        "dimensions": 1024,
                        "encoding_format": "float",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                data = payload["data"]
                data.sort(key=lambda item: item["index"])
                batch_vectors = [item["embedding"] for item in data]
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise RuntimeError("embedding_provider_error") from exc
            if len(batch_vectors) != len(batch):
                raise RuntimeError("embedding_result_count_mismatch")
            for vector in batch_vectors:
                if len(vector) != 1024 or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
                    raise RuntimeError("embedding_dimension_or_value_invalid")
                vectors.append([float(value) for value in vector])
            if on_batch is not None:
                await on_batch()
    return vectors
