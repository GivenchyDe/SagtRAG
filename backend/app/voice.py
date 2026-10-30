"""企微语音下载、内存转码和 Qwen ASR；不把音频写入本地磁盘。"""

import asyncio
import base64
import io
import os
import subprocess
import time
import wave

import httpx
import imageio_ffmpeg

from app.config import get_settings
from app.wecom_delivery import DeliveryError, WeComDelivery

VOICE_MESSAGES = {
    "voice_media_expired": "语音已过期或无法读取，请重新发送。",
    "voice_too_large": "语音文件过大，请发送较短的语音。",
    "voice_too_long": "语音时长超过限制，请分段发送。",
    "voice_invalid_audio": "无法识别这段语音的音频格式，请重新录制。",
    "voice_no_speech": "没有识别到清晰的文字，请重新录制或发送文字问题。",
    "voice_download_error": "语音下载暂时失败，请稍后重试。",
    "voice_asr_error": "语音转写服务暂时不可用，请稍后重试。",
}


class VoiceError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


async def download_voice(delivery: WeComDelivery, media_id: str) -> bytes:
    """只使用已验证回调中的 media_id 调用企微素材接口，限制流式下载大小。"""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=8)) as client:
        for attempt in range(2):
            try:
                token = await delivery.access_token(client)
                async with client.stream("GET", f"{str(settings.we_com_api_base_url).rstrip('/')}/cgi-bin/media/get",
                                         params={"access_token": token, "media_id": media_id}) as response:
                    response.raise_for_status()
                    audio = bytearray()
                    async for part in response.aiter_bytes():
                        audio.extend(part)
                        if len(audio) > settings.voice_max_bytes:
                            raise VoiceError("voice_too_large")
                    if "json" in response.headers.get("content-type", "") or audio.lstrip().startswith(b"{"):
                        import json
                        payload = json.loads(audio)
                        code = payload.get("errcode")
                        if code in {40014, 42001, 40001} and attempt == 0:
                            await delivery.redis.delete(delivery.token_key)
                            continue
                        if code in {40007, 40008, 41005}:
                            raise VoiceError("voice_media_expired")
                        raise VoiceError("voice_download_error", retryable=code in {-1, 45009})
                    if not audio:
                        raise VoiceError("voice_invalid_audio")
                    return bytes(audio)
            except DeliveryError as exc:
                raise VoiceError("voice_download_error", retryable=exc.retryable) from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise VoiceError("voice_download_error", retryable=True) from exc
    raise VoiceError("voice_download_error", retryable=True)


async def to_wav(audio: bytes) -> tuple[bytes, float]:
    """内存管道将 AMR 等输入转成单声道 16k WAV，超长语音明确拒绝。"""
    limit = get_settings().voice_max_seconds
    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0", "-t", str(limit + 1), "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(audio), timeout=20)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.communicate()
        raise
    if process.returncode:
        raise VoiceError("voice_invalid_audio")
    try:
        with wave.open(io.BytesIO(output)) as wav:
            frames = wav.readframes(wav.getnframes())
            duration = len(frames) / (wav.getframerate() * wav.getnchannels() * wav.getsampwidth())
    except (wave.Error, EOFError) as exc:
        raise VoiceError("voice_invalid_audio") from exc
    if duration > limit:
        raise VoiceError("voice_too_long")
    if duration == 0:
        raise VoiceError("voice_no_speech")
    return output, duration


async def transcribe_wav(audio: bytes) -> str:
    """通过 Data URI 提交虚构/获准语音，不公开音频 URL，不保存隐藏内容。"""
    settings = get_settings()
    data = "data:audio/wav;base64," + base64.b64encode(audio).decode("ascii")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=8)) as client:
            response = await client.post(
                f"{str(settings.asr_base_url).rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.embedding_api_key.get_secret_value()}"},
                json={"model": settings.asr_model, "stream": False,
                      "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": data}}]}],
                      "asr_options": {"enable_itn": False}},
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("invalid ASR response")
            transcript = content.strip()
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise VoiceError("voice_asr_error", retryable=True) from exc
    if not transcript:
        raise VoiceError("voice_no_speech")
    if len(transcript) > settings.wecom_max_text_chars:
        raise VoiceError("voice_too_long")
    return transcript


async def transcribe_voice(delivery: WeComDelivery, media_id: str, diagnostics: dict) -> str:
    """音频只在当前调用的内存中存活，诊断仅保存状态、时长和耗时。"""
    info = {"state": "downloading", "model": get_settings().asr_model}
    diagnostics["voice"] = info
    start = time.perf_counter()
    audio = await download_voice(delivery, media_id)
    info["download_ms"] = round((time.perf_counter() - start) * 1000)
    info["audio_bytes"] = len(audio)
    info["state"] = "transcribing"
    wav, duration = await to_wav(audio)
    info["duration_seconds"] = round(duration, 2)
    start = time.perf_counter()
    transcript = await transcribe_wav(wav)
    info["asr_ms"] = round((time.perf_counter() - start) * 1000)
    info["state"] = "transcribed"
    return transcript
