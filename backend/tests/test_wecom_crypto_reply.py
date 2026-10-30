"""验证企业微信被动回复的加密信封及签名格式。"""

import hashlib
import hmac
import xml.etree.ElementTree as XMLTree
from types import SimpleNamespace

from pydantic import SecretStr

from app.auth import wecom_crypto


def test_encrypted_reply_round_trips_and_has_valid_signature(monkeypatch) -> None:
    """被动回复可解密、签名正确且包含安全编码的正文。"""
    settings = SimpleNamespace(
        we_com_encoding_aes_key=SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        we_com_corp_id="corp-test",
        we_com_agent_id="100001",
        we_com_token=SecretStr("token-test"),
    )
    monkeypatch.setattr(wecom_crypto, "get_settings", lambda: settings)
    nonce = "nonce-test"
    envelope_bytes = wecom_crypto.build_encrypted_reply(recipient="employee-1", content="已收到：<安全>", nonce=nonce)
    envelope = XMLTree.fromstring(envelope_bytes)
    encrypted = envelope.findtext("Encrypt")
    timestamp = envelope.findtext("TimeStamp")
    signature = envelope.findtext("MsgSignature")
    expected = hashlib.sha1("".join(sorted(("token-test", timestamp, nonce, encrypted))).encode()).hexdigest()
    assert hmac.compare_digest(signature or "", expected)
    inner = XMLTree.fromstring(wecom_crypto.decrypt_message(encrypted or ""))
    assert inner.findtext("ToUserName") == "employee-1"
    assert inner.findtext("Content") == "已收到：<安全>"

