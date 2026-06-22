"""
edge/audio/pipeline.py
======================
AIY Voice HAT 音声パイプライン (speech_recognition / Google Web Speech API版)。

設計変更の経緯:
  旧: Vosk常時ストリーミングでウェイクワード検知 -> Google Cloud STT(要サービス
      アカウント認証)で本文認識、という2段構成だった。
  新: ウェイクワード検知・本文認識ともに speech_recognition ライブラリの
      recognizer.recognize_google() (Google Web Speech API、APIキー不要) に
      一本化した。Voskより認識精度が高く、反応性の問題を解消するため。

動作フロー:
  1. 常時 Microphone から listen() で発話区間を自動検出 (無音検知込み)
  2. recognize_google() でテキスト化
  3. テキストにウェイクワードが含まれていれば「はい、なんでしょう」と応答し
     会話モードへ。会話モード中はもう一度 listen() して本文を取得する
  4. 取得した本文を GeminiRequest としてサーバーに送信

TTSは VOICEVOX (サーバー側エンジン) を優先し、失敗時は espeak-ng にフォールバック。
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Optional

log = logging.getLogger(__name__)

try:
    import speech_recognition as sr
    _SR_OK = True
except ImportError:
    _SR_OK = False
    log.warning("[audio] speech_recognition not available — stub mode")

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False
    log.warning("[tts] requests not available — VOICEVOX disabled")

from shared.protocol.config import cfg
from shared.protocol.messages import (
    AudioStreamEnd, AudioStreamStart, MessageEnvelope, GeminiRequest,
)


class AudioPipeline:
    """
    ウェイクワード検知 (Google Web Speech API) -> 本文認識 -> TTS再生 を管理する。
    """

    def __init__(self, connection_manager) -> None:
        self._conn = connection_manager
        self._running = False
        self._n = 0
        self._recognizer: Optional["sr.Recognizer"] = None
        self._mic: Optional["sr.Microphone"] = None
        self._wake_words = [w.lower() for w in cfg.audio.wake_words]
        self._language = cfg.google_stt.language_code
        self._is_speaking = asyncio.Event()  # set中はTTS再生中 (listen()を避ける)

    async def start(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._setup_microphone)
        asyncio.create_task(self._loop(), name="audio_listen")
        log.info("[audio_pipeline] started")

    async def stop(self) -> None:
        self._running = False

    def _setup_microphone(self) -> None:
        if not _SR_OK:
            return
        self._recognizer = sr.Recognizer()
        # AIY Voice HAT を明示的に探す (見つからなければデフォルト入力デバイス)
        device_index = self._find_aiy_device_index()
        self._mic = sr.Microphone(device_index=device_index)
        log.info("[audio] microphone ready (device_index=%s), wake_words=%s",
                  device_index, self._wake_words)

    def _find_aiy_device_index(self) -> Optional[int]:
        keywords = ["googlevoicehat", "aiy", "voicehat"]
        try:
            for i, name in enumerate(sr.Microphone.list_microphone_names()):
                if any(k in name.lower() for k in keywords):
                    log.info("[audio] AIY device: [%d] %s", i, name)
                    return i
        except Exception as exc:
            log.warning("[audio] device enumeration failed: %s", exc)
        log.warning("[audio] AIY device not found — using default input")
        return None

    # ── 音声認識 (ブロッキング、executor経由で呼ぶ) ──────────────
    def _listen_once(self) -> Optional["sr.AudioData"]:
        """
        発話区間を1つ録音して返す。タイムアウト/エラー時はNone。
        参考実装と同様、listen()の直前に毎回 adjust_for_ambient_noise() を呼び、
        その場の環境ノイズに追従させる。timeout/phrase_time_limitは指定せず
        speech_recognitionのデフォルト挙動 (発話開始まで無期限待機、
        発話終了は自然な無音検知に任せる) に委ねる。
        """
        if self._recognizer is None or self._mic is None:
            return None
        try:
            with self._mic as source:
                self._recognizer.adjust_for_ambient_noise(source, duration=0.5)
                audio = self._recognizer.listen(source)
            return audio
        except Exception as exc:
            log.error("[audio] listen error: %s", exc)
            return None

    def _recognize(self, audio: "sr.AudioData") -> str:
        if self._recognizer is None or audio is None:
            return ""
        try:
            return self._recognizer.recognize_google(audio, language=self._language)
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as exc:
            log.error("[stt] google STT request error: %s", exc)
            return ""

    # ── TTS ──────────────────────────────────────────────────
    async def speak(self, text: str, lang: str = "ja-JP") -> None:
        """
        VOICEVOX (サーバー側エンジン) でテキストを音声合成して再生する。
        VOICEVOXが無効/接続失敗の場合は espeak-ng にフォールバックする。

        再生中は is_speaking フラグを立て、マイクが自分の声を拾って
        誤ってウェイクワード判定/本文認識をしてしまうのを防ぐ。
        """
        log.info("[tts] '%s'", text[:50])
        self._is_speaking.set()
        try:
            if cfg.voicevox.enabled and _REQUESTS_OK:
                wav_bytes = await asyncio.get_running_loop().run_in_executor(
                    None, self._synthesize_voicevox, text
                )
                if wav_bytes is not None:
                    await self._play_wav_bytes(wav_bytes)
                    return
                log.warning("[tts] VOICEVOX failed — falling back to espeak-ng")

            await self._speak_espeak(text, lang)
        finally:
            self._is_speaking.clear()

    def _synthesize_voicevox(self, text: str) -> Optional[bytes]:
        base = f"http://{cfg.voicevox.host}:{cfg.voicevox.port}"
        speaker = cfg.voicevox.speaker_id
        timeout = cfg.voicevox.timeout_sec

        try:
            r = requests.post(
                f"{base}/audio_query",
                params={"text": text, "speaker": speaker},
                timeout=timeout,
            )
            r.raise_for_status()
            audio_query = r.json()

            r2 = requests.post(
                f"{base}/synthesis",
                params={"speaker": speaker},
                json=audio_query,
                timeout=timeout,
            )
            r2.raise_for_status()
            return r2.content
        except Exception as exc:
            log.error("[tts] voicevox error: %s", exc)
            return None

    async def _play_wav_bytes(self, wav_bytes: bytes) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
            f.write(wav_bytes)
        try:
            p = await asyncio.create_subprocess_exec(
                "aplay", "-q", "-D", "plughw:1,0", wav_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
        except Exception as exc:
            log.error("[tts] playback error: %s", exc)
        finally:
            try:
                os.unlink(wav_path)
            except Exception:
                pass

    async def _speak_espeak(self, text: str, lang: str = "ja-JP") -> None:
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
                "aplay", "-q", "-D", "plughw:1,0", wav_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await p2.wait()
        except Exception as exc:
            log.error("[tts] espeak error: %s", exc)
        finally:
            try:
                os.unlink(wav_path)
            except Exception:
                pass

    # ── メインループ ─────────────────────────────────────────
    async def _loop(self) -> None:
        if not _SR_OK or self._recognizer is None:
            log.warning("[audio_pipeline] stub mode")
            return

        loop = asyncio.get_running_loop()
        log.info("[audio_pipeline] listening for wake word...")

        while self._running:
            # TTS再生中はマイクが自分の声を拾ってしまうため、再生終了まで待つ
            while self._is_speaking.is_set():
                await asyncio.sleep(0.1)

            audio = await loop.run_in_executor(None, self._listen_once)
            if audio is None:
                continue

            text = await loop.run_in_executor(None, self._recognize, audio)
            if not text:
                continue

            log.info("[wake] heard: '%s'", text)
            lowered = text.lower()
            if any(w in lowered for w in self._wake_words):
                log.info("[wake] * DETECTED")
                # 自分の発声をマイクが拾わないよう、再生完了を待ってから録音を始める
                await self.speak("はい、なんでしょう")
                await self._handle_conversation_turn(loop)
                log.info("[audio_pipeline] back to listening for wake word...")

    async def _handle_conversation_turn(self, loop) -> None:
        """
        ウェイクワード検知後、1回分の本文発話を録音・認識して
        GeminiRequest としてサーバーへ送信する。
        """
        self._n += 1
        sid = f"sess_{self._n:04d}"

        await self._conn.control.send(MessageEnvelope(payload=AudioStreamStart(session_id=sid)))

        audio = await loop.run_in_executor(None, self._listen_once)

        await self._conn.control.send(
            MessageEnvelope(payload=AudioStreamEnd(session_id=sid, duration_ms=0, total_bytes=0))
        )

        if audio is None:
            log.info("[stt] no audio captured — skipping")
            await self.speak("聞き取れませんでした、もう一度お願いします")
            return

        text = await loop.run_in_executor(None, self._recognize, audio)
        if not text:
            log.info("[stt] no speech recognized — skipping Gemini request")
            await self.speak("聞き取れませんでした、もう一度お願いします")
            return

        log.info("[stt] recognized: '%s'", text)
        await self._conn.control.send(
            MessageEnvelope(payload=GeminiRequest(session_id=sid, text_input=text))
        )
