"""
edge/main.py
============
エッジ (Raspberry Pi) のエントリーポイント。

起動順序:
  1. 設定ロード・ログ初期化
  2. HW レイヤー初期化 (Stage 2 で実装)
  3. サーバーへの WebSocket 接続確立
  4. 音声パイプライン起動 (Stage 3 で実装)
  5. メッセージディスパッチループ
  6. 終了処理 (サーボ中立化)

Stage 1: 通信・接続確立のみ実装。HW はスタブ。
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from shared.protocol.config import cfg
from shared.protocol.messages import (
    MessageEnvelope, MessageType,
    PoseCommand, ServoCommand, SystemStatus,
)
from edge.utils.connection import ConnectionManager

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# メッセージディスパッチャ
# ---------------------------------------------------------------------------
async def dispatch_loop(conn: ConnectionManager) -> None:
    """
    制御チャネルから受信したメッセージを各ハンドラにルーティングする。
    Stage 2以降: HW クラスを注入して実際に動かす。
    """
    log.info("[dispatch] loop started")
    while True:
        try:
            env: MessageEnvelope = await asyncio.wait_for(
                conn.control.recv_queue.get(), timeout=1.0
            )
        except asyncio.TimeoutError:
            continue

        msg = env.payload
        mtype = msg.type

        if mtype == MessageType.POSE_COMMAND:
            log.info("[dispatch] POSE: %s (%dms)", msg.pose_name, msg.duration_ms)
            # Stage 2 で ServoController.execute_pose(msg) に差し替え

        elif mtype == MessageType.SERVO_COMMAND:
            log.info("[dispatch] SERVO ch=%d pulse=%dμs", msg.channel, msg.pulse_us)
            # Stage 2 で ServoController.set_pulse(msg) に差し替え

        elif mtype == MessageType.GEMINI_RESPONSE:
            log.info("[dispatch] GEMINI_RESPONSE: emotion=%s text=%s",
                     msg.emotion, (msg.text or "")[:60])
            # Stage 3 で AudioPipeline.speak(msg.text) + emotion handling に差し替え

        elif mtype == MessageType.CAMERA_REQUEST:
            log.info("[dispatch] CAMERA_REQUEST: mode=%s", msg.mode)
            # Stage 2 で CameraController.handle_request(msg) に差し替え

        elif mtype == MessageType.HEARTBEAT:
            log.debug("[dispatch] heartbeat from server")

        elif mtype == MessageType.ERROR:
            log.warning("[dispatch] server error: [%s] %s", msg.code, msg.message)

        else:
            log.debug("[dispatch] unhandled: %s", mtype)


# ---------------------------------------------------------------------------
# ステータス報告ループ
# ---------------------------------------------------------------------------
async def status_loop(conn: ConnectionManager) -> None:
    """CPU・温度等のシステム状態をサーバーに定期送信する"""
    import psutil, os

    while True:
        await asyncio.sleep(30)
        if not conn.control.is_connected:
            continue

        cpu_pct = psutil.cpu_percent(interval=None)
        mem_pct = psutil.virtual_memory().percent
        temp_c: float | None = None

        # Raspberry Pi CPU温度
        try:
            raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text()
            temp_c = int(raw.strip()) / 1000.0
        except Exception:
            pass

        from shared.protocol.messages import ComponentStatus
        status = SystemStatus(
            node="edge",
            cpu_pct=cpu_pct,
            mem_pct=mem_pct,
            temp_c=temp_c,
        )
        await conn.control.send(MessageEnvelope(payload=status))


# ---------------------------------------------------------------------------
# メインエントリ
# ---------------------------------------------------------------------------
async def main() -> None:
    # ログ初期化
    logging.basicConfig(
        level=getattr(logging, cfg.system.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    log.info("=== %s edge starting ===", cfg.system.name)

    conn = ConnectionManager()

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(sig):
        log.info("received %s, shutting down...", sig.name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    # 接続開始
    await conn.start()
    log.info("waiting for server connection...")
    ready = await conn.wait_ready(timeout=cfg.network.connect_timeout_sec)
    if not ready:
        log.warning("server not reachable — starting in offline mode")

    # タスク群
    tasks = [
        asyncio.create_task(dispatch_loop(conn), name="dispatch"),
        asyncio.create_task(status_loop(conn),   name="status"),
        # Stage 3 で AudioPipeline タスクを追加
    ]

    # 停止シグナルを待つ
    await stop_event.wait()

    # クリーンアップ
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await conn.stop()
    log.info("=== edge shutdown complete ===")


if __name__ == "__main__":
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    asyncio.run(main())
