"""
edge/main.py  (Stage 3 音声統合版)
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from shared.protocol.config import cfg
from shared.protocol.messages import (
    MessageEnvelope, MessageType,
    SystemStatus, ComponentStatus,
)
from edge.utils.connection import ConnectionManager
from edge.audio.pipeline import AudioPipeline

log = logging.getLogger(__name__)


# ── ディスパッチループ ────────────────────────────────────────
async def dispatch_loop(conn: ConnectionManager, audio: AudioPipeline) -> None:
    log.info("[dispatch] loop started")
    while True:
        try:
            env: MessageEnvelope = await asyncio.wait_for(
                conn.control.recv_queue.get(), timeout=1.0
            )
        except asyncio.TimeoutError:
            continue

        msg   = env.payload
        mtype = msg.type

        if mtype == MessageType.GEMINI_RESPONSE:
            log.info("[dispatch] GEMINI_RESPONSE: emotion=%s text=%s",
                     msg.emotion, (msg.text or "")[:80])
            if msg.text:
                asyncio.create_task(audio.speak(msg.text))

        elif mtype == MessageType.HEARTBEAT:
            log.debug("[dispatch] heartbeat from server")

        elif mtype == MessageType.ERROR:
            log.warning("[dispatch] server error [%s]: %s", msg.code, msg.message)

        else:
            log.debug("[dispatch] unhandled: %s", mtype)


# ── ステータス報告ループ ─────────────────────────────────────
async def status_loop(conn: ConnectionManager) -> None:
    import psutil

    while True:
        await asyncio.sleep(30)
        if not conn.control.is_connected:
            continue

        cpu   = psutil.cpu_percent(interval=None)
        mem   = psutil.virtual_memory().percent
        temp  = None
        try:
            temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        except Exception:
            pass

        status = SystemStatus(
            node="edge",
            cpu_pct=cpu,
            mem_pct=mem,
            temp_c=temp,
        )
        await conn.control.send(MessageEnvelope(payload=status))


# ── メイン ───────────────────────────────────────────────────
async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.system.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    log.info("=== %s edge starting ===", cfg.system.name)

    conn  = ConnectionManager()
    audio = AudioPipeline(conn)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(sig):
        log.info("received %s, shutting down...", sig.name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    # 接続
    await conn.start()
    log.info("waiting for server connection...")
    ready = await conn.wait_ready(timeout=cfg.network.connect_timeout_sec)
    if not ready:
        log.warning("server not reachable — starting in offline mode")

    # 音声パイプライン起動
    await audio.start()

    tasks = [
        asyncio.create_task(dispatch_loop(conn, audio), name="dispatch"),
        asyncio.create_task(status_loop(conn),          name="status"),
    ]

    await stop_event.wait()

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await audio.stop()
    await conn.stop()
    log.info("=== edge shutdown complete ===")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    asyncio.run(main())
