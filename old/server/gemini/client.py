"""
server/gemini/client.py
=======================
Gemini 1.5 Flash クライアント。

Stage 1: スタブ実装 (インターフェース定義のみ)
Stage 3: 音声入力・Function Calling の完全実装に差し替える

設計:
  - audio_queue に PCM チャンクが積まれると、セッション終了時に
    まとめて Gemini Multimodal API へ送信する。
  - Function Calling の結果は receive_function_result() で受け取り、
    マルチターン会話として Gemini に返す。
  - response_callback を通じて GeminiResponse をサーバー API 層に通知する。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Dict, List, Optional

from shared.protocol.messages import FunctionResult, GeminiResponse, EmotionState
from shared.protocol.config import cfg
from shared.functions.definitions import get_tools_schema

log = logging.getLogger(__name__)

ResponseCallback = Callable[[GeminiResponse], Awaitable[None]]


class AudioBuffer:
    """セッション単位で PCM を蓄積するバッファ"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.chunks: List[bytes] = []
        self.total_bytes = 0

    def append(self, chunk: bytes) -> None:
        self.chunks.append(chunk)
        self.total_bytes += len(chunk)

    def get_pcm(self) -> bytes:
        return b"".join(self.chunks)

    def clear(self) -> None:
        self.chunks.clear()
        self.total_bytes = 0


class GeminiClient:
    """
    Gemini 1.5 Flash とのやり取りを管理するクライアント。

    Stage 1 では begin_session / feed_audio / finalize_session の
    インターフェースのみ定義し、Stage 3 で本実装に置き換える。
    """

    def __init__(self) -> None:
        self._response_callback: Optional[ResponseCallback] = None
        self._sessions: Dict[str, AudioBuffer] = {}
        # Function Call 待機中の Future: call_id → Future
        self._pending_fc: Dict[str, asyncio.Future] = {}
        self._tools = get_tools_schema()
        self._running = False

    async def start(self) -> None:
        self._running = True
        api_key = cfg.secrets.gemini_api_key
        if api_key is None:
            log.warning("[gemini] GEMINI_API_KEY not set — running in stub mode")
        else:
            log.info("[gemini] client ready (model=%s)", cfg.gemini.model)

    async def stop(self) -> None:
        self._running = False
        self._sessions.clear()
        for fut in self._pending_fc.values():
            fut.cancel()
        self._pending_fc.clear()

    def set_response_callback(self, cb: Optional[ResponseCallback]) -> None:
        self._response_callback = cb

    # ------------------------------------------------------------------
    # セッション管理
    # ------------------------------------------------------------------
    async def begin_session(self, session_id: str) -> None:
        """音声セッション開始。バッファを初期化する。"""
        self._sessions[session_id] = AudioBuffer(session_id)
        log.debug("[gemini] session begin: %s", session_id)

    async def feed_audio(self, session_id: int, chunk: bytes) -> None:
        """PCM チャンクをバッファに蓄積する"""
        sid = str(session_id)
        if sid not in self._sessions:
            # begin_session より先にバイナリが届く場合の保護
            self._sessions[sid] = AudioBuffer(sid)
        self._sessions[sid].append(chunk)

    async def finalize_session(self, session_id: str) -> None:
        """
        セッション終了。蓄積した音声を Gemini に送信する。
        Stage 3 で完全実装に差し替え。
        """
        buf = self._sessions.pop(session_id, None)
        if buf is None:
            log.warning("[gemini] finalize_session: unknown session %s", session_id)
            return

        log.info("[gemini] [STUB] finalize session %s (%.1f kB PCM)",
                 session_id, buf.total_bytes / 1024)

        # --- Stage 1 スタブ応答 ---
        stub_response = GeminiResponse(
            session_id=session_id,
            text="[Stage1 stub] Gemini 応答はStage3で実装されます。",
            emotion=EmotionState.NEUTRAL,
        )
        await self._emit(stub_response)

    async def receive_function_result(self, result: FunctionResult) -> None:
        """
        エッジから Function Calling の実行結果を受け取る。
        Stage 3 で Gemini マルチターン会話に組み込む。
        """
        fut = self._pending_fc.pop(result.call_id, None)
        if fut and not fut.done():
            fut.set_result(result)
        else:
            log.debug("[gemini] unexpected function result: %s", result.call_id)

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------
    async def _emit(self, response: GeminiResponse) -> None:
        if self._response_callback:
            try:
                await self._response_callback(response)
            except Exception as exc:
                log.error("[gemini] response callback error: %s", exc)
