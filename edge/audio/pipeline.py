"""
edge/audio/pipeline.py
======================
AIY Voice HAT 音声パイプライン (実機検証済み)。

ハードウェア制約:
  AIY Voice HAT (snd_rpi_googlevoicehat_soundcar) は 48000Hz のみ対応。
  VOSK / Gemini への送信は 16000Hz PCM16 を要求するため、
  48kHz録音 -> 1/3間引きで16kHzにダウンサンプリングする。

設計:
  - PyAudio インスタンスは1つを使い回す (再生成によるデバイス競合を回避)
  - ウェイクワード検知用ストリームと録音用ストリームは
    open/close を切り替えつつ同じ PyAudio インスタンス上で運用する
  - ストリームclose後は asyncio.sleep でデバイス解放を待つ
  - TTSは espeak-ng でWAV生成 -> aplay (plughw:1,0) で再生
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import tempfile
import time
from typing import Optional

log = logging.getLogger(__name__)

try:
    import pyaudio
    import vosk
    import numpy as np
    _AUDIO_OK = True
except ImportError:
    _AUDIO_OK = False
    log.warning("[audio] pyaudio/vosk/numpy not available — stub mode")

from shared.protocol.config import cfg
from shared.protocol.messages import AudioStreamEnd, AudioStreamStart, MessageEnvelope

HW_RATE    = 48000   # AIY Voice HAT のネイティブレート
VOSK_RATE  = 16000   # VOSK / Gemini 送信レート
CHANNELS   = 1
HW_CHUNK   = 4800    # 100ms @ 48kHz


def _downsample(pcm48: bytes) -> bytes:
    """48kHz PCM16 -> 16kHz PCM16 (1/3間引き)"""
    arr = np.frombuffer(pcm48, dtype=np.int16)
    return arr[::3].copy().tobytes()


def _rms(pcm: bytes) -> float:
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm[:count * 2])
    return (sum(s * s for s in samples) / count) ** 0.5


def _find_aiy_device(pa: "pyaudio.PyAudio") -> Optional[int]:
    keywords = ["googlevoicehat", "aiy", "voicehat"]
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        name = info["name"].lower()
        if any(k in name for k in keywords) and info["maxInputChannels"] > 0:
            log.info("[audio] AIY device: [%d] %s", i, info["name"])
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
        self._rec = vosk.KaldiRecognizer(model, VOSK_RATE)
        log.info("[wake] ready, words=%s", self._wake_words)

    def check(self, pcm16: bytes) -> bool:
        if self._rec is None:
            return False
        if self._rec.AcceptWaveform(pcm16):
            text = json.loads(self._rec.Result()).get("text", "").lower()
            if text:
                log.info("[wake] heard: '%s'", text)
            if any(w in text for w in self._wake_words):
                log.info("[wake] * DETECTED")
                return True
        return False


class AudioPipeline:
    """
    ウェイクワード検知 -> PCMストリーミング -> TTS再生 を管理する。
    """

    def __init__(self, connection_manager) -> None:
        self._conn = connection_manager
        self._det  = WakeWordDetector()
        self._running = False
        self._n = 0

    async def start(self) -> None:
        self._running = True
        await asyncio.get_running_loop().run_in_executor(None, self._det.load)
        asyncio.create_task(self._loop(), name="audio_listen")
        log.info("[audio_pipeline] started")

    async def stop(self) -> None:
        self._running = False

    # ── TTS ──────────────────────────────────────────────────
    async def speak(self, text: str, lang: str = "ja-JP") -> None:
        log.info("[tts] '%s'", text[:50])
        l = "ja" if lang.startswith("ja") else "en"
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
        try:
            p = await asyncio.create_subprocess_exec(
                "espeak-ng", "-v", l, "-s", "130", "-w", wav_path, text,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
            p2 = await asyncio.create_subprocess_exec(
                "aplay", "-D", "plughw:1,0", wav_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await p2.wait()
        except Exception as exc:
            log.error("[tts] error: %s", exc)
        finally:
            try:
                os.unlink(wav_path)
            except Exception:
                pass

    # ── メインループ ─────────────────────────────────────────
    async def _loop(self) -> None:
        if not _AUDIO_OK:
            log.warning("[audio_pipeline] stub mode")
            return

        loop = asyncio.get_running_loop()
        pa  = pyaudio.PyAudio()
        dev = _find_aiy_device(pa)

        def _open():
            return pa.open(
                format=pyaudio.paInt16, channels=CHANNELS,
                rate=HW_RATE, input=True,
                input_device_index=dev, frames_per_buffer=HW_CHUNK,
            )

        st = _open()
        log.info("[audio_pipeline] listening (48kHz->16kHz)...")

        try:
            while self._running:
                pcm48 = st.read(HW_CHUNK, exception_on_overflow=False)
                pcm16 = _downsample(pcm48)

                if self._det.check(pcm16):
                    st.stop_stream()
                    st.close()
                    await asyncio.sleep(0.3)

                    asyncio.create_task(self.speak("はい、なんでしょう"))
                    await self._record(pa, dev, loop)

                    await asyncio.sleep(0.3)
                    st = _open()
                    log.info("[audio_pipeline] back to listening...")
        finally:
            try:
                st.stop_stream()
                st.close()
            except Exception:
                pass
            pa.terminate()

    async def _record(self, pa, dev, loop) -> None:
        self._n += 1
        sid     = f"sess_{self._n:04d}"
        sid_int = self._n

        await self._conn.control.send(MessageEnvelope(payload=AudioStreamStart(session_id=sid)))

        def _open_rec():
            return pa.open(
                format=pyaudio.paInt16, channels=CHANNELS,
                rate=HW_RATE, input=True,
                input_device_index=dev, frames_per_buffer=HW_CHUNK,
            )

        st = await loop.run_in_executor(None, _open_rec)
        t0 = time.time()
        total = 0
        sil = 0
        log.info("[stream] recording %s...", sid)

        try:
            while True:
                pcm48 = await loop.run_in_executor(
                    None, lambda: st.read(HW_CHUNK, exception_on_overflow=False)
                )
                pcm16 = _downsample(pcm48)
                total += len(pcm16)

                asyncio.create_task(self._conn.audio.send_audio(sid_int, pcm16))

                rms = _rms(pcm16)
                sil = sil + 100 if rms < cfg.audio.silence_amplitude else 0

                if sil >= cfg.audio.silence_threshold_ms:
                    log.info("[stream] silence -> end")
                    break
                if time.time() - t0 > 30:
                    log.warning("[stream] timeout")
                    break
        finally:
            await loop.run_in_executor(None, lambda: (st.stop_stream(), st.close()))

        dur = int((time.time() - t0) * 1000)
        await self._conn.control.send(
            MessageEnvelope(payload=AudioStreamEnd(session_id=sid, duration_ms=dur, total_bytes=total))
        )
        log.info("[stream] done %dms/%dbytes", dur, total)
