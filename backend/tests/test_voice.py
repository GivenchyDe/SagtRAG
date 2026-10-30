"""仅检查 Phase 4 音频边界和 ASR/素材接口契约。"""

import io
import json
import wave
from types import SimpleNamespace

import httpx
import pytest

from app import voice
from app.voice import VoiceError, download_voice, to_wav, transcribe_wav


def wav_bytes(seconds=0.1):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * int(seconds * 16000))
    return output.getvalue()


@pytest.mark.asyncio
async def test_real_codec_decodes_wav_and_wecom_amr_and_enforces_duration(monkeypatch):
    wav, duration = await to_wav(wav_bytes())
    assert wav.startswith(b"RIFF") and duration == pytest.approx(0.1)
    # AMR-NB 的 20ms 帧，只用于验证企微 AMR 到 WAV 的实际解码能力。
    amr = b"#!AMR\n" + (b"\x3c" + bytes(31)) * 5
    converted, duration = await to_wav(amr)
    assert converted.startswith(b"RIFF") and duration > 0
    monkeypatch.setattr(voice, "get_settings", lambda: SimpleNamespace(voice_max_seconds=0.05))
    with pytest.raises(VoiceError, match="voice_too_long"):
        await to_wav(wav_bytes())


class FakeDelivery:
    redis = None
    async def access_token(self, client):
        return "fixture-token"


def mocked_client(monkeypatch, handle):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))


@pytest.mark.asyncio
async def test_media_download_rejects_expired_media(monkeypatch):
    def handle(request):
        assert request.url.path == "/cgi-bin/media/get"
        return httpx.Response(200, json={"errcode":40007})
    mocked_client(monkeypatch, handle)
    with pytest.raises(VoiceError, match="voice_media_expired"):
        await download_voice(FakeDelivery(), "fixture-media")


@pytest.mark.asyncio
async def test_media_download_enforces_byte_limit(monkeypatch):
    settings = voice.get_settings().model_copy(update={"voice_max_bytes": 8})
    monkeypatch.setattr(voice, "get_settings", lambda: settings)
    mocked_client(monkeypatch, lambda request: httpx.Response(200, content=b"#!AMR\n" + bytes(20)))
    with pytest.raises(VoiceError, match="voice_too_large"):
        await download_voice(FakeDelivery(), "fixture-media")


@pytest.mark.asyncio
async def test_asr_uses_audio_data_uri_and_rejects_no_speech(monkeypatch):
    def handle(request):
        body = json.loads(request.content)
        assert body["model"] == "qwen3-asr-flash"
        assert body["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/wav;base64,")
        return httpx.Response(200, json={"choices":[{"message":{"content":"  "}}]})
    mocked_client(monkeypatch, handle)
    with pytest.raises(VoiceError, match="voice_no_speech"):
        await transcribe_wav(wav_bytes())
