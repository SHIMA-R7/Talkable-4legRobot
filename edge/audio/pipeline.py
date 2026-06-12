"""
edge/audio/pipeline.py
======================
AIY Voice HAT 音声パイプライン。

AIYサウンドカードのデバイス名: snd_rpi_googlevoicehat_soundcar (card 1)
PyAudio デバイス名で自動検出する。
再生は plughw:1,0 相当 (plug経由でフォーマット変換)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import subprocess
import tempfile
import time
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

try:
    import pyaudio
    import vosk
    _AUDIO_OK = True
except ImportError:
    _AUDIO_OK = False
    log.warning("[audio] pyaudio/vosk not available")

from shared.protocol.config import cfg
from shared.protocol.messages import (
    AudioStreamEnd, AudioStreamStart, MessageEnvelope,
)

SAMPLE_RATE  = 16000
CHANNELS     = 1
CHUNK_FRAMES = 1600   # 100ms


def _rms(pcm: bytes) -> float:
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm[:count * 2])
    return (sum(s * s for s in samples) / count) ** 0.5


def _find_aiy_device(pa: "pyaudio.PyAudio") -> Optional[int]:
    """AIY Voice HAT のマイクデバイスインデックスを返す"""
    keywords = ["googlevoicehat", "aiy", "voicehat"]
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        name = info["name"].lower()
        if any(k in name for k in keywords) and info["maxInputChannels"] > 0:
            log.info("[audio] found AIY device: [%d] %s", i, info["name"])
            return i
    log.warning("[audio] AIY device not found — using default input")
    return None


class WakeWordDetector:

    def __init__(self) -> None:
        self._rec = None
        self._wake_words = [w.lower() for w in cfg.audio.wake_words]

    def load(self) -> None:
        if not _AUDIO_OK:
            return
        log.info("[wake] loading model: %s", cfg.audio.vosk_model_path)
        model = vosk.Model(cfg.audio.vosk_model_path)
        self._rec = vosk.KaldiRecognizer(model, SAMPLE_RATE)
        log.info("[wake] model ready, words=%s", self._wake_words)

    def check(self, pcm: bytes) -> bool:
        if self._rec is None:
            return False
        if self._rec.AcceptWaveform(pcm):
            text = json.loads(self._rec.Result()).get("text", "").lower()
            if text:
                log.info("[wake] heard: '%s'", text)
            if any(w in text for w in self._wake_words):
                log.info("[wake] ★ WAKE WORD DETECTED")
                return True
        return False


class AudioPipeline:
    """
    ウェイクワード検知 → PCMストリーミング → TTS再生 を管理する。
    """

    def __init__(self, connection_manager) -> None:
        self._conn    = connection_manager
        self._detector = WakeWordDetector()
        self._running  = False
        self._session_counter = 0

    async def start(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._detector.load)
        asyncio.create_task(self._listen_loop(), name="audio_listen")
        log.info("[audio_pipeline] started")

    async def stop(self) -> None:
        self._running = False

    # ── TTS 再生 ────────────────────────────────────────────
    async def speak(self, text: str, language: str = "ja-JP") -> None:
        """espeak-ng でテキストを読み上げる"""
        log.info("[tts] speaking: '%s'", text[:60])
        speed = "130"
        lang  = "ja" if language.startswith("ja") else "en"

        # AIY HAT は plughw:1,0 経由でないと鳴らないので
        # espeak → wav → aplay とパイプ
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name

        try:
            # espeak-ng でwav生成
            await asyncio.create_subprocess_exec(
                "espeak-ng", "-v", lang, "-s", speed,
                "-w", wav_path, text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.sleep(0.3)  # 生成待ち

            # aplay で AIY HAT に出力 (plughw でフォーマット自動変換)
            proc = await asyncio.create_subprocess_exec(
                "aplay", "-D", "plughw:1,0", wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception as exc:
            log.error("[tts] error: %s", exc)
        finally:
            import os
            try:
                os.unlink(wav_path)
            except Exception:
                pass

    # ── メインループ ─────────────────────────────────────────
    async def _listen_loop(self) -> None:
        if not _AUDIO_OK:
            log.warning("[audio_pipeline] stub mode — no pyaudio")
            return

        pa     = pyaudio.PyAudio()
        dev_idx = _find_aiy_device(pa)

        stream = pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=dev_idx,
            frames_per_buffer=CHUNK_FRAMES,
        )
        log.info("[audio_pipeline] listening for wake word...")

        try:
            while self._running:
                pcm = stream.read(CHUNK_FRAMES, exception_on_overflow=False)

                if self._detector.check(pcm):
                    stream.stop_stream()

                    # 応答音
                    asyncio.create_task(self.speak("はい、なんでしょう"))

                    await self._stream_once(pa, dev_idx)
                    stream.start_stream()
                    log.info("[audio_pipeline] back to listening...")

        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()

    async def _stream_once(
        self,
        pa: "pyaudio.PyAudio",
        dev_idx: Optional[int],
    ) -> None:
        """発話1回分をストリーミング送信する"""
        self._session_counter += 1
        sid     = f"sess_{self._session_counter:04d}"
        sid_int = self._session_counter

        # セッション開始通知
        start = AudioStreamStart(session_id=sid)
        await self._conn.control.send(MessageEnvelope(payload=start))

        stream = pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=dev_idx,
            frames_per_buffer=CHUNK_FRAMES,
        )

        t0         = time.time()
        total      = 0
        silence_ms = 0
        log.info("[stream] recording (session=%s)...", sid)

        try:
            while True:
                pcm    = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
                total += len(pcm)

                # 音声チャネルに送信
                header = sid_int.to_bytes(4, "big")
                asyncio.create_task(
                    self._conn.audio.send_audio(sid_int, pcm)
                )

                # 無音判定
                rms = _rms(pcm)
                if rms < cfg.audio.silence_amplitude:
                    silence_ms += cfg.audio.chunk_ms
                else:
                    silence_ms = 0

                if silence_ms >= cfg.audio.silence_threshold_ms:
                    log.info("[stream] silence detected — end")
                    break
                if time.time() - t0 > 30:
                    log.warning("[stream] max duration reached")
                    break
        finally:
            stream.stop_stream()
            stream.close()

        dur_ms = int((time.time() - t0) * 1000)
        end = AudioStreamEnd(session_id=sid, duration_ms=dur_ms, total_bytes=total)
        await self._conn.control.send(MessageEnvelope(payload=end))
        log.info("[stream] done: %dms / %d bytes", dur_ms, total)
