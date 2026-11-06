"""企业微信 access token 缓存和文本主动发送。"""

import asyncio

import httpx
from redis.asyncio import Redis

from app import http_clients
from app.config import get_settings
from app.deadline import call_timeout, check_current


class DeliveryError(Exception):
    """仅向任务状态暴露错误码、是否可重试及是否可能已送达。"""

    def __init__(self, code: str, *, retryable: bool = False, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.uncertain = uncertain


class WeComDelivery:
    def __init__(self, redis: Redis):
        self.redis = redis
        self.token_lock = asyncio.Lock()

    @property
    def token_key(self) -> str:
        settings = get_settings()
        return f"sagtrag:wecom:token:{settings.we_com_corp_id}:{settings.we_com_agent_id}"

    async def access_token(self, client: httpx.AsyncClient, *,
                           timeout_seconds: float | None = None) -> str:
        """access token 只存于 Redis，不进入问答表和日志。"""
        async with self.token_lock:
            cached = await self.redis.get(self.token_key)
            if cached:
                return cached.decode() if isinstance(cached, bytes) else cached
            settings = get_settings()
            try:
                response = await client.get(
                    f"{str(settings.we_com_api_base_url).rstrip('/')}/cgi-bin/gettoken",
                    params={"corpid": settings.we_com_corp_id, "corpsecret": settings.we_com_secret.get_secret_value()},
                    timeout=http_clients.request_timeout(
                        timeout_seconds if timeout_seconds is not None else 15.0),
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise DeliveryError("wecom_token_unavailable", retryable=True) from exc
            code = payload.get("errcode", 0)
            if code:
                raise DeliveryError(f"wecom_token_{code}", retryable=code in {-1, 45009})
            if not payload.get("access_token"):
                raise DeliveryError("wecom_token_invalid")
            await self.redis.set(self.token_key, payload["access_token"], ex=max(1, int(payload.get("expires_in", 7200)) - 300))
            return payload["access_token"]

    async def send_text(self, recipient: str, content: str, *, duplicate_check: bool = True) -> str:
        """开发者模式启用重复检查；普通员工不展示编号，因此关闭内容去重。"""
        settings = get_settings()
        payload = {"touser": recipient, "msgtype": "text", "agentid": int(settings.we_com_agent_id),
                   "text": {"content": content}, "enable_duplicate_check": 1 if duplicate_check else 0}
        if duplicate_check:
            payload["duplicate_check_interval"] = 1800
        check_current("send")
        budget = call_timeout(15.0)
        client = http_clients.get_client()
        token = await self.access_token(client, timeout_seconds=budget)
        try:
            response = await client.post(
                f"{str(settings.we_com_api_base_url).rstrip('/')}/cgi-bin/message/send",
                params={"access_token": token},
                json=payload,
                timeout=http_clients.request_timeout(budget),
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise DeliveryError("wecom_connect_failed", retryable=True) from exc
        except (httpx.HTTPError, ValueError) as exc:
            # 响应丢失时可能已送达；不盲目重发，保留可诊断状态。
            raise DeliveryError("wecom_send_unknown", uncertain=True) from exc
        code = payload.get("errcode", 0)
        if code in {40014, 42001, 40001}:
            await self.redis.delete(self.token_key)
            raise DeliveryError(f"wecom_send_{code}", retryable=True)
        if code:
            raise DeliveryError(f"wecom_send_{code}", retryable=code in {-1, 45009, 45011})
        if payload.get("invaliduser") or payload.get("unlicenseduser"):
            raise DeliveryError("wecom_recipient_invalid")
        if payload.get("errcode") != 0 or not payload.get("msgid"):
            raise DeliveryError("wecom_send_response_invalid", uncertain=True)
        return str(payload["msgid"])
