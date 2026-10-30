"""只验证 Phase 3 主动发送协议、token 刷新和不确定结果分类。"""

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app import wecom_delivery
from app.wecom_delivery import DeliveryError, WeComDelivery


class TokenCache:
    def __init__(self):
        self.token = "fixture-old-token"

    async def get(self, key):
        return self.token

    async def set(self, key, value, **kwargs):
        self.token = value

    async def delete(self, key):
        self.token = None


def configure(monkeypatch, handler):
    settings = SimpleNamespace(we_com_corp_id="fixture-corp", we_com_agent_id="1000001",
        we_com_secret=SecretStr("fixture-secret"), we_com_api_base_url="https://wecom.fixture")
    monkeypatch.setattr(wecom_delivery, "get_settings", lambda: settings)
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(wecom_delivery.httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))


@pytest.mark.asyncio
async def test_send_payload_refreshes_expired_token_without_duplicate(monkeypatch):
    sends = []

    def handle(request):
        if request.url.path.endswith("gettoken"):
            return httpx.Response(200, json={"access_token":"fixture-new-token","expires_in":7200})
        sends.append(json.loads(request.content))
        if len(sends) == 1:
            return httpx.Response(200, json={"errcode":42001})
        return httpx.Response(200, json={"errcode":0,"msgid":"fixture-msg"})

    configure(monkeypatch, handle)
    cache = TokenCache()
    sender = WeComDelivery(cache)
    with pytest.raises(DeliveryError) as result:
        await sender.send_text("fixture-user", "答案 [1]")
    assert result.value.retryable and not result.value.uncertain and cache.token is None
    assert await sender.send_text("fixture-user", "答案 [1]") == "fixture-msg"
    assert sends[-1] == {"touser":"fixture-user","msgtype":"text","agentid":1000001,
                         "text":{"content":"答案 [1]"},"enable_duplicate_check":1,"duplicate_check_interval":1800}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,uncertain", [(httpx.ConnectTimeout,False),(httpx.ReadTimeout,True)])
async def test_only_definitely_unsent_transport_errors_are_retryable(monkeypatch, failure, uncertain):
    def handle(request):
        raise failure("fixture timeout",request=request)

    configure(monkeypatch, handle)
    with pytest.raises(DeliveryError) as result:
        await WeComDelivery(TokenCache()).send_text("fixture-user", "答案")
    assert result.value.uncertain is uncertain
    assert result.value.retryable is not uncertain
