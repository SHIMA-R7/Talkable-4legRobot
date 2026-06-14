"""
server/api/app.py
=================
FastAPI メインアプリ。

エンドポイント:
  WS  /ws/audio      音声バイナリストリーム受信
  WS  /ws/control    JSON 双方向制御チャネル
  GET  /api/status   システム状態照会
  POST /api/camera/frame  カメラフレーム受信
  GET  /health       ヘルスチェック (Tailscale Funnel 用)
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from contextlib import asynccontextmanager
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from shared.protocol.messages import (
    MessageEnvelope, MessageType,
    SystemStatus, ComponentStatus, ErrorMessage, Heartbeat,
    AudioStreamStart, AudioStreamEnd,
    GeminiResponse,
)
from shared.protocol.config import cfg
from server.utils.logging import setup_logging
from server.gemini.client import GeminiClient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# アプリケーション状態
# ---------------------------------------------------------------------------
class AppState:
    def __init__(self) -> None:
        self.gemini: GeminiClient | None = None
        # アクティブな制御 WebSocket (エッジ1台を想定)
        self.control_ws: WebSocket | None = None
        # セッションID → 受信バイト数
        self.audio_sessions: Dict[int, int] = {}
        self.started_at: float = time.time()

state = AppState()


# ---------------------------------------------------------------------------
# ライフサイクル
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(cfg.system.log_level)
    log.info("=== %s server starting ===", cfg.system.name)

    state.gemini = GeminiClient()
    await state.gemini.start()

    yield

    log.info("=== server shutting down ===")
    if state.gemini:
        await state.gemini.stop()


# ---------------------------------------------------------------------------
# FastAPI インスタンス
# ---------------------------------------------------------------------------
app = FastAPI(
    title=f"{cfg.system.name} Server",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.server.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# ヘルパー: エッジへの送信
# ---------------------------------------------------------------------------
async def send_to_edge(envelope: MessageEnvelope) -> bool:
    """制御チャネル経由でエッジにメッセージを送る"""
    ws = state.control_ws
    if ws is None:
        log.warning("send_to_edge: no edge connected")
        return False
    try:
        await ws.send_text(envelope.to_json())
        return True
    except Exception as exc:
        log.error("send_to_edge error: %s", exc)
        state.control_ws = None
        return False


# ---------------------------------------------------------------------------
# WebSocket: 音声チャネル
# ---------------------------------------------------------------------------
@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    await ws.accept()
    log.info("[audio] edge connected from %s", ws.client)
    remote_addr = ws.client.host if ws.client else "unknown"

    try:
        while True:
            data = await ws.receive_bytes()
            if len(data) < 4:
                log.warning("[audio] frame too short (%d bytes)", len(data))
                continue

            # 先頭 4 byte: セッション ID (uint32 BE)
            session_id = struct.unpack(">I", data[:4])[0]
            pcm_chunk  = data[4:]

            if session_id not in state.audio_sessions:
                state.audio_sessions[session_id] = 0
                log.info("[audio] new session %d", session_id)

            state.audio_sessions[session_id] += len(pcm_chunk)

            # Gemini へ PCM チャンクを転送
            if state.gemini:
                await state.gemini.feed_audio(session_id, pcm_chunk)

    except WebSocketDisconnect:
        log.info("[audio] edge disconnected (%s)", remote_addr)
    except Exception as exc:
        log.error("[audio] error: %s", exc)
    finally:
        # セッションクリーンアップ
        state.audio_sessions.clear()


# ---------------------------------------------------------------------------
# WebSocket: 制御チャネル
# ---------------------------------------------------------------------------
@app.websocket("/ws/control")
async def ws_control(ws: WebSocket):
    await ws.accept()
    log.info("[ctrl] edge connected from %s", ws.client)
    state.control_ws = ws

    # Gemini のコールバックを登録: Gemini が応答を生成したら制御チャネル経由でエッジへ送る
    if state.gemini:
        state.gemini.set_response_callback(_on_gemini_response)

    try:
        while True:
            raw = await ws.receive_text()
            await _handle_control_message(raw)

    except WebSocketDisconnect:
        log.info("[ctrl] edge disconnected")
    except Exception as exc:
        log.error("[ctrl] error: %s", exc)
    finally:
        if state.control_ws is ws:
            state.control_ws = None
        if state.gemini:
            state.gemini.set_response_callback(None)


async def _handle_control_message(raw: str) -> None:
    """受信した制御メッセージをルーティングする"""
    try:
        env = MessageEnvelope.from_json(raw)
    except Exception as exc:
        log.warning("[ctrl] parse error: %s", exc)
        return

    msg = env.payload
    mtype = msg.type

    if mtype == MessageType.HEARTBEAT:
        # エコーバック
        pong = MessageEnvelope(payload=Heartbeat(node="server"))
        await send_to_edge(pong)

    elif mtype == MessageType.AUDIO_STREAM_START:
        log.info("[ctrl] audio stream start: session=%s", msg.session_id)
        if state.gemini:
            await state.gemini.begin_session(msg.session_id)

    elif mtype == MessageType.AUDIO_STREAM_END:
        log.info("[ctrl] audio stream end: session=%s duration=%dms bytes=%d",
                 msg.session_id, msg.duration_ms, msg.total_bytes)
        if state.gemini:
            await state.gemini.finalize_session(msg.session_id)

    elif mtype == MessageType.SYSTEM_STATUS:
        log.debug("[ctrl] edge status: cpu=%.1f%% temp=%s°C",
                  msg.cpu_pct, msg.temp_c)

    elif mtype == MessageType.FUNCTION_RESULT:
        log.info("[ctrl] function result: %s → %s", msg.name, msg.result)
        if state.gemini:
            await state.gemini.receive_function_result(msg)

    else:
        log.debug("[ctrl] unhandled message type: %s", mtype)


async def _on_gemini_response(response: GeminiResponse) -> None:
    """Gemini 応答をエッジに転送するコールバック"""
    env = MessageEnvelope(payload=response)
    await send_to_edge(env)


# ---------------------------------------------------------------------------
# REST: ヘルスチェック
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    uptime = int(time.time() - state.started_at)
    return {
        "status": "ok",
        "uptime_sec": uptime,
        "edge_connected": state.control_ws is not None,
        "gemini_ready": state.gemini is not None,
    }


# ---------------------------------------------------------------------------
# REST: システム状態
# ---------------------------------------------------------------------------
@app.get("/api/status")
async def api_status():
    components = [
        ComponentStatus(
            name="control_ws",
            ok=state.control_ws is not None,
            detail="edge connected" if state.control_ws else "no edge",
        ),
        ComponentStatus(
            name="gemini",
            ok=state.gemini is not None,
            detail=f"model={cfg.gemini.model}" if state.gemini else "not initialized",
        ),
    ]
    status = SystemStatus(
        node="server",
        components=components,
    )
    return status.model_dump()


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server.api.app:app",
        host=cfg.server.host,
        port=cfg.server.port,
        reload=cfg.server.reload,
        log_level=cfg.system.log_level.lower(),
    )
