"""
edge/main.py  (Stage 3 統合版)
==============================
エッジ (Raspberry Pi) のエントリーポイント。

起動順序:
  1. 設定ロード・ログ初期化
  2. WebSocket接続確立 (control + audio)
  3. サーボ初期化 (脚部 0x40 必須, 首部 0x41 はオプション)
  4. ディスプレイ初期化 (任意)
  5. 音声パイプライン起動 (ウェイクワード検知ループ)
  6. ディスパッチループ (Geminiからの応答 -> TTS/サーボ)
  7. 終了処理 (サーボをneutralに戻す)
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
    SystemStatus, FunctionResult,
)
from edge.utils.connection import ConnectionManager
from edge.utils.function_executor import FunctionExecutor
from edge.audio.pipeline import AudioPipeline
from edge.servo import ServoController

log = logging.getLogger(__name__)


# ── 感情 -> ポーズ マッピング ─────────────────────────────────
EMOTION_TO_POSE = {
    "neutral":   "neutral",
    "happy":     "happy_wag",
    "sad":       "sad_droop",
    "surprised": "alert",
    "thinking":  "thinking",
    "excited":   "happy_wag",
    "sleepy":    "sleep",
    "angry":     "shake_head",
}


# ── ディスパッチループ ────────────────────────────────────────
async def dispatch_loop(conn: ConnectionManager, audio: AudioPipeline,
                         servo: ServoController, executor: FunctionExecutor) -> None:
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

            # Geminiが execute_pose/set_emotion を呼ばなかった場合のフォールバック。
            # 既にFunction Call経由でポーズ実行済みでも、同じポーズへの再実行は
            # 安全 (アイドル時の状態確定として機能する) なため許容する。
            pose = EMOTION_TO_POSE.get(msg.emotion.value, "neutral")
            asyncio.create_task(servo.execute_pose(pose))

            if msg.text:
                asyncio.create_task(audio.speak(msg.text))

        elif mtype == MessageType.FUNCTION_CALL:
            log.info("[dispatch] FUNCTION_CALL: %s(%s) call_id=%s",
                     msg.name, msg.arguments, msg.call_id)
            asyncio.create_task(_handle_function_call(conn, executor, msg))

        elif mtype == MessageType.POSE_COMMAND:
            log.info("[dispatch] POSE_COMMAND: %s", msg.pose_name)
            asyncio.create_task(servo.execute_pose(
                msg.pose_name, msg.duration_ms, msg.easing
            ))

        elif mtype == MessageType.HEARTBEAT:
            log.debug("[dispatch] heartbeat from server")

        elif mtype == MessageType.ERROR:
            log.warning("[dispatch] server error [%s]: %s", msg.code, msg.message)

        else:
            log.debug("[dispatch] unhandled: %s", mtype)


async def _handle_function_call(conn: ConnectionManager, executor: FunctionExecutor,
                                  fc) -> None:
    """FunctionCall を実行し、結果を FunctionResult として送信する"""
    result: FunctionResult = await executor.execute(fc)
    await conn.control.send(MessageEnvelope(payload=result))
    log.info("[dispatch] FUNCTION_RESULT sent: %s -> ok=%s",
              result.name, result.error is None)


# ── ステータス報告ループ ─────────────────────────────────────
async def status_loop(conn: ConnectionManager) -> None:
    import psutil

    while True:
        await asyncio.sleep(30)
        if not conn.control.is_connected:
            continue

        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        temp = None
        try:
            temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        except Exception:
            pass

        status = SystemStatus(node="edge", cpu_pct=cpu, mem_pct=mem, temp_c=temp)
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
    servo = ServoController()
    executor = FunctionExecutor(servo, audio)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(sig):
        log.info("received %s, shutting down...", sig.name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    # サーボ初期化
    await servo.init()

    # 接続
    await conn.start()
    log.info("waiting for server connection...")
    ready = await conn.wait_ready(timeout=cfg.network.connect_timeout_sec)
    if not ready:
        log.warning("server not reachable — starting in offline mode")

    # 音声パイプライン起動
    await audio.start()

    tasks = [
        asyncio.create_task(dispatch_loop(conn, audio, servo, executor), name="dispatch"),
        asyncio.create_task(status_loop(conn),                          name="status"),
    ]

    await stop_event.wait()

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await audio.stop()
    await servo.shutdown()
    await conn.stop()
    log.info("=== edge shutdown complete ===")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    asyncio.run(main())
