"""进程生命周期共享的 HTTP 连接池。

外部调用只借用同一个 AsyncClient；请求级 timeout 由 deadline 计算，退出时统一关闭，
避免每次分类/向量/检索/生成/发送都新建 TLS 连接。
"""

from __future__ import annotations

import httpx

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=8.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16,
                                keepalive_expiry=30.0),
            follow_redirects=False,
        )
    return _client


def request_timeout(seconds: float) -> httpx.Timeout:
    value = max(0.05, float(seconds))
    return httpx.Timeout(value, connect=min(8.0, value))


async def close_client() -> None:
    global _client
    client, _client = _client, None
    if client is not None:
        await client.aclose()


def reset_client_for_tests() -> None:
    """只供测试隔离；生产通过 close_client 在进程退出时关闭。"""
    global _client
    _client = None
