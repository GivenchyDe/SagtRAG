"""企业微信回调签名校验与 AES-CBC 载荷解密。"""

import base64
import hashlib
import hmac
import os
import struct
import time
import xml.etree.ElementTree as XMLTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from defusedxml import ElementTree
from fastapi import HTTPException, status

from app.config import get_settings


def verify_signature(*, signature: str, timestamp: str, nonce: str, encrypted: str) -> None:
    """解密前先验证加密回调信封的签名。"""
    token = get_settings().we_com_token.get_secret_value()
    expected = hashlib.sha1("".join(sorted((token, timestamp, nonce, encrypted))).encode()).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid WeCom callback signature")


def decrypt_message(encrypted: str) -> bytes:
    """解码企业微信 AES-CBC 信封，并校验其中的 CorpID。"""
    settings = get_settings()
    key = base64.b64decode(settings.we_com_encoding_aes_key.get_secret_value() + "=", validate=True)
    try:
        ciphertext = base64.b64decode(encrypted, validate=True)
        if not ciphertext or len(ciphertext) % 16:
            raise ValueError("invalid ciphertext size")
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        pad_length = padded[-1]
        if pad_length < 1 or pad_length > 32 or padded[-pad_length:] != bytes([pad_length]) * pad_length:
            raise ValueError("invalid padding")
        plain = padded[:-pad_length]
        if len(plain) < 20:
            raise ValueError("truncated message")
        message_length = struct.unpack(">I", plain[16:20])[0]
        end = 20 + message_length
        if end > len(plain) or len(plain) - end > 128:
            raise ValueError("invalid message length")
        message = plain[20:end]
        receive_id = plain[end:].decode("utf-8")
        if not hmac.compare_digest(receive_id, settings.we_com_corp_id):
            raise ValueError("CorpID mismatch")
        return message
    except (ValueError, UnicodeDecodeError, struct.error) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid WeCom encrypted payload") from exc


def get_encrypted_element(body: bytes) -> str:
    """使用安全 XML 解析器提取长度受限的 Encrypt 字段。"""
    try:
        root = ElementTree.fromstring(body)
        value = root.findtext("Encrypt")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed WeCom callback XML") from exc
    if not value or len(value) > 1_000_000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing WeCom encrypted payload")
    return value.strip()


def parse_message(message: bytes) -> dict[str, str]:
    """解析解密后的 XML，并拒绝发往其他应用或企业的消息。"""
    try:
        root = ElementTree.fromstring(message)
        fields = {child.tag: (child.text or "").strip() for child in root}
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed WeCom message XML") from exc
    settings = get_settings()
    if fields.get("ToUserName") != settings.we_com_corp_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="WeCom CorpID mismatch")
    if fields.get("AgentID") != settings.we_com_agent_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="WeCom AgentID mismatch")
    if not fields.get("FromUserName") or not fields.get("MsgId"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Incomplete WeCom message")
    return fields


def encrypt_message(message: bytes) -> str:
    """使用企业微信 AES-CBC 信封格式加密待回复 XML。"""
    settings = get_settings()
    key = base64.b64decode(settings.we_com_encoding_aes_key.get_secret_value() + "=", validate=True)
    plain = os.urandom(16) + struct.pack(">I", len(message)) + message + settings.we_com_corp_id.encode("utf-8")
    padding = 32 - len(plain) % 32
    plain += bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(plain) + encryptor.finalize()).decode("ascii")


def build_encrypted_reply(*, recipient: str, content: str, nonce: str) -> bytes:
    """构造固定文本的被动回复，并按企业微信回调协议签名加密。"""
    settings = get_settings()
    reply = XMLTree.Element("xml")
    fields = {
        "ToUserName": recipient,
        "FromUserName": settings.we_com_corp_id,
        "CreateTime": str(int(time.time())),
        "MsgType": "text",
        "Content": content,
        "AgentID": settings.we_com_agent_id,
    }
    for name, value in fields.items():
        XMLTree.SubElement(reply, name).text = value
    encrypted = encrypt_message(XMLTree.tostring(reply, encoding="utf-8"))
    timestamp = str(int(time.time()))
    signature = hashlib.sha1(
        "".join(sorted((settings.we_com_token.get_secret_value(), timestamp, nonce, encrypted))).encode()
    ).hexdigest()
    envelope = XMLTree.Element("xml")
    XMLTree.SubElement(envelope, "Encrypt").text = encrypted
    XMLTree.SubElement(envelope, "MsgSignature").text = signature
    XMLTree.SubElement(envelope, "TimeStamp").text = timestamp
    XMLTree.SubElement(envelope, "Nonce").text = nonce
    return XMLTree.tostring(envelope, encoding="utf-8")
