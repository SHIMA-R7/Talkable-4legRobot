"""
server/api/app.py
=================
FastAPI メインアプリ。

エンドポイント:
  WS  /ws/audio          音声バイナリストリーム受信
  WS  /ws/control         JSON 双方向制御チャネル
  GET  /api/status        システム状態照会
  GET  /health            ヘルスチェック

起動例:
  python -m uvicorn server.api.app:app --host 0.0.0.0 --port 8000 \
      --log-level info --timeout-keep-alive 120
"""

from __future__ import annotations

import logging
import struct
import time
from contextlib import asynccontextmanager
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from shared.protocol.messages import (
    MessageEnvelope, MessageType,
    SystemStatus, ComponentStatus, Heartbeat,
    GeminiResponse, FunctionCall,
)
from shared.protocol.config import cfg
from server.utils.logging import setup_logging
from server.gemini.client import GeminiClient

log = logging.getLogger(__name__)


class AppState:
    def __init__(self) -> None:
        self.gemini: GeminiClient | None = None
        self.control_ws: WebSocket | None = None
        self.audio_sessions: Dict[int, int] = {}
        self.started_at: float = time.time()

state = AppState()


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


app = FastAPI(title=f"{cfg.system.name} Server", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def send_to_edge(envelope: MessageEnvelope) -> bool:
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


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    """
    音声バイナリチャネル。
    現在は edge 側で Google STT によりテキスト化してから /ws/control 経由で
    GeminiRequest を送る方式に変更したため、このチャネルは Gemini には使われない。
    将来的にカメラ画像のストリーミング等に転用できるよう構造は残してある。
    """
    await ws.accept()
    log.info("[audio] edge connected from %s", ws.client)

    try:
        while True:
            data = await ws.receive_bytes()
            if len(data) < 4:
                log.warning("[audio] frame too short (%d bytes)", len(data))
                continue

            session_id = struct.unpack(">I", data[:4])[0]
            pcm_chunk  = data[4:]

            if session_id not in state.audio_sessions:
                state.audio_sessions[session_id] = 0
                log.info("[audio] new session %d", session_id)

            state.audio_sessions[session_id] += len(pcm_chunk)

    except WebSocketDisconnect:
        log.info("[audio] edge disconnected")
    except Exception as exc:
        log.error("[audio] error: %s", exc)
    finally:
        state.audio_sessions.clear()


@app.websocket("/ws/control")
async def ws_control(ws: WebSocket):
    await ws.accept()
    log.info("[ctrl] edge connected from %s", ws.client)
    state.control_ws = ws

    if state.gemini:
        state.gemini.set_response_callback(_on_gemini_response)
        state.gemini.set_function_call_sender(_send_function_call_to_edge)

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
            state.gemini.set_function_call_sender(None)


async def _handle_control_message(raw: str) -> None:
    try:
        env = MessageEnvelope.from_json(raw)
    except Exception as exc:
        log.warning("[ctrl] parse error: %s", exc)
        return

    msg = env.payload
    mtype = msg.type

    if mtype == MessageType.HEARTBEAT:
        pong = MessageEnvelope(payload=Heartbeat(node="server"))
        await send_to_edge(pong)

    elif mtype == MessageType.AUDIO_STREAM_START:
        # 録音開始の進捗ログのみ。Gemini呼び出しはGEMINI_REQUEST受信時に行う
        # (旧: ここでbegin_session()してPCM蓄積していたが、
        #  edge側でGoogle STTテキスト化する方式に変更したため不要になった)
        log.info("[ctrl] audio stream start: session=%s", msg.session_id)

    elif mtype == MessageType.AUDIO_STREAM_END:
        log.info("[ctrl] audio stream end: session=%s duration=%dms bytes=%d",
                 msg.session_id, msg.duration_ms, msg.total_bytes)

    elif mtype == MessageType.GEMINI_REQUEST:
        log.info("[ctrl] gemini request: session=%s text='%s'",
                  msg.session_id, (msg.text_input or "")[:80])
        if state.gemini and msg.text_input:
            await state.gemini.process_text_request(msg.session_id, msg.text_input)

    elif mtype == MessageType.SYSTEM_STATUS:
        log.debug("[ctrl] edge status: cpu=%.1f%% temp=%s°C", msg.cpu_pct, msg.temp_c)

    elif mtype == MessageType.FUNCTION_RESULT:
        log.info("[ctrl] function result: %s -> %s", msg.name, msg.result)
        if state.gemini:
            await state.gemini.receive_function_result(msg)

    else:
        log.debug("[ctrl] unhandled message type: %s", mtype)


async def _on_gemini_response(response: GeminiResponse) -> None:
    env = MessageEnvelope(payload=response)
    await send_to_edge(env)


async def _send_function_call_to_edge(fc: FunctionCall) -> None:
    """GeminiClient からの Function Call を edge に転送する"""
    log.info("[ctrl] forwarding function_call: %s(%s) call_id=%s",
              fc.name, fc.arguments, fc.call_id)
    env = MessageEnvelope(payload=fc)
    ok = await send_to_edge(env)
    if not ok:
        log.warning("[ctrl] failed to forward function_call %s — edge not connected", fc.call_id)


@app.get("/health")
async def health():
    uptime = int(time.time() - state.started_at)
    return {
        "status": "ok",
        "uptime_sec": uptime,
        "edge_connected": state.control_ws is not None,
        "gemini_ready": state.gemini is not None,
    }


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
    status = SystemStatus(node="server", components=components)
    return status.model_dump()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server.api.app:app",
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=cfg.system.log_level.lower(),
        timeout_keep_alive=120,
    )
