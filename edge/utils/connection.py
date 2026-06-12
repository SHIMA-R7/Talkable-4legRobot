"""
edge/utils/connection.py
========================
エッジ側 WebSocket 接続マネージャー。

- JSON 制御チャネル (/ws/control) と
  音声バイナリチャネル (/ws/audio) を管理する。
- 切断時の自動再接続、ハートビート送信を担う。
- asyncio.Queue でメッセージを非同期に受け渡す。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import websockets
import websockets.exceptions

from shared.protocol.messages import (
    Heartbeat, MessageEnvelope, MessageType,
    AUDIO_CHUNK_BYTES,
)
from shared.protocol.config import cfg

log = logging.getLogger(__name__)

# バイナリ音声フレームのセッションIDヘッダサイズ (uint32 BE)
_SESSION_ID_BYTES = 4


class ControlChannel:
    """
    JSON 双方向制御チャネル。
    受信メッセージは recv_queue に積む。
    送信は send() を呼ぶ。
    """

    def __init__(self) -> None:
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = asyncio.Event()
        self.recv_queue: asyncio.Queue[MessageEnvelope] = asyncio.Queue(maxsize=64)
        self._send_lock = asyncio.Lock()
        self._running = False
        self._reconnect_count = 0

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.close_code

    async def start(self) -> None:
        """バックグラウンドで接続ループを開始する"""
        self._running = True
        asyncio.create_task(self._connect_loop(), name="ctrl_connect_loop")
        asyncio.create_task(self._heartbeat_loop(), name="ctrl_heartbeat")

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def send(self, envelope: MessageEnvelope) -> bool:
        """エンベロープを JSON として送信。失敗時は False を返す"""
        if not self.is_connected:
            log.warning("[ctrl] send skipped: not connected")
            return False
        try:
            async with self._send_lock:
                await self._ws.send(envelope.to_json())
            return True
        except Exception as exc:
            log.error("[ctrl] send error: %s", exc)
            await self._handle_disconnect()
            return False

    async def _connect_loop(self) -> None:
        uri = (
            f"ws://{cfg.network.server_host}:{cfg.network.server_port}"
            f"{cfg.network.ws_control_path}"
        )
        max_attempts = cfg.network.max_reconnect_attempts  # 0 = 無限

        while self._running:
            try:
                log.info("[ctrl] connecting to %s", uri)
                async with websockets.connect(
                    uri,
                    open_timeout=cfg.network.connect_timeout_sec,
                    ping_interval=None,   # 独自ハートビートを使う
                    max_size=1024 * 1024, # 1 MB
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    self._reconnect_count = 0
                    log.info("[ctrl] connected")
                    await self._recv_loop(ws)
            except (websockets.exceptions.WebSocketException, OSError, asyncio.TimeoutError) as exc:
                log.warning("[ctrl] disconnected: %s", exc)
            finally:
                self._connected.clear()
                self._ws = None

            if not self._running:
                break
            if max_attempts and self._reconnect_count >= max_attempts:
                log.error("[ctrl] max reconnect attempts reached")
                break

            self._reconnect_count += 1
            wait = min(cfg.network.reconnect_interval_sec * self._reconnect_count, 60)
            log.info("[ctrl] retry in %.1f s (attempt %d)", wait, self._reconnect_count)
            await asyncio.sleep(wait)

    async def _recv_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw in ws:
            if not isinstance(raw, str):
                log.debug("[ctrl] unexpected binary frame, ignored")
                continue
            try:
                env = MessageEnvelope.from_json(raw)
                await self.recv_queue.put(env)
            except Exception as exc:
                log.warning("[ctrl] parse error: %s | raw=%s", exc, raw[:120])

    async def _heartbeat_loop(self) -> None:
        interval = cfg.network.heartbeat_interval_sec
        while self._running:
            await asyncio.sleep(interval)
            if self.is_connected:
                hb = Heartbeat(node="edge")
                env = MessageEnvelope(payload=hb)
                await self.send(env)

    async def _handle_disconnect(self) -> None:
        self._connected.clear()
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


class AudioChannel:
    """
    音声バイナリ WebSocket チャネル。
    PCM データを連続バイナリフレームとして送信する。
    各フレーム先頭 4 byte はセッションID (uint32 BE)。
    """

    def __init__(self) -> None:
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = asyncio.Event()
        self._running = False
        self._send_lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._connect_loop(), name="audio_connect_loop")

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def send_audio(self, session_id: int, pcm_chunk: bytes) -> bool:
        """session_id (uint32) + PCM をバイナリフレームで送信"""
        if not self.is_connected:
            return False
        try:
            header = session_id.to_bytes(_SESSION_ID_BYTES, "big")
            frame = header + pcm_chunk
            async with self._send_lock:
                await self._ws.send(frame)
            return True
        except Exception as exc:
            log.error("[audio] send error: %s", exc)
            self._connected.clear()
            self._ws = None
            return False

    async def _connect_loop(self) -> None:
        uri = (
            f"ws://{cfg.network.server_host}:{cfg.network.server_port}"
            f"{cfg.network.ws_audio_path}"
        )
        while self._running:
            try:
                log.info("[audio] connecting to %s", uri)
                async with websockets.connect(
                    uri,
                    open_timeout=cfg.network.connect_timeout_sec,
                    ping_interval=None,
                    max_size=10 * 1024 * 1024,  # 10 MB (音声用に大きめ)
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    log.info("[audio] connected")
                    # 音声チャネルは送信専用なので recv は無視
                    await ws.wait_closed()
            except Exception as exc:
                log.warning("[audio] disconnected: %s", exc)
            finally:
                self._connected.clear()
                self._ws = None

            if not self._running:
                break
            await asyncio.sleep(cfg.network.reconnect_interval_sec)

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


class ConnectionManager:
    """エッジの全 WebSocket 接続をまとめて管理するファサード"""

    def __init__(self) -> None:
        self.control = ControlChannel()
        self.audio   = AudioChannel()

    async def start(self) -> None:
        await self.control.start()
        await self.audio.start()
        log.info("[conn] both channels starting up")

    async def stop(self) -> None:
        await self.control.stop()
        await self.audio.stop()
        log.info("[conn] all channels closed")

    async def wait_ready(self, timeout: float = 30.0) -> bool:
        ctrl_ok  = await self.control.wait_connected(timeout)
        audio_ok = await self.audio.wait_connected(timeout)
        if not ctrl_ok:
            log.error("[conn] control channel not ready within %.0f s", timeout)
        if not audio_ok:
            log.error("[conn] audio channel not ready within %.0f s", timeout)
        return ctrl_ok and audio_ok
