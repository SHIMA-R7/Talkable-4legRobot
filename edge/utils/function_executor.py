"""
edge/utils/function_executor.py
=================================
Gemini からの FunctionCall を実際のハードウェア操作に変換して実行し、
結果を FunctionResult として返すモジュール。

対応関数 (shared/functions/definitions.py と同期させること):
  - execute_pose       : ServoController.execute_pose
  - set_emotion        : 感情 -> ポーズマッピングで ServoController.execute_pose
  - walk_forward       : ServoController.execute_trot
  - speak              : AudioPipeline.speak
  - get_system_status  : psutil でCPU/メモリ/温度を取得
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from shared.protocol.messages import FunctionCall, FunctionResult

log = logging.getLogger(__name__)


# main.py の EMOTION_TO_POSE と同じマッピング (循環import回避のため複製)
_EMOTION_TO_POSE = {
    "neutral":   "neutral",
    "happy":     "happy_wag",
    "sad":       "sad_droop",
    "surprised": "alert",
    "thinking":  "thinking",
    "excited":   "happy_wag",
    "sleepy":    "sleep",
    "angry":     "shake_head",
}


class FunctionExecutor:
    """
    ServoController / AudioPipeline のインスタンスを束ねて、
    FunctionCall を実行する。main.py から呼び出される。
    """

    def __init__(self, servo, audio) -> None:
        self._servo = servo
        self._audio = audio

    async def execute(self, fc: FunctionCall) -> FunctionResult:
        try:
            handler = getattr(self, f"_fn_{fc.name}", None)
            if handler is None:
                return FunctionResult(
                    call_id=fc.call_id, name=fc.name,
                    error=f"unknown function: {fc.name}",
                )
            result = await handler(fc.arguments)
            return FunctionResult(call_id=fc.call_id, name=fc.name, result=result)

        except Exception as exc:
            log.error("[function_executor] %s failed: %s", fc.name, exc, exc_info=True)
            return FunctionResult(call_id=fc.call_id, name=fc.name, error=str(exc))

    # ── 個別ハンドラ ──────────────────────────────────────

    async def _fn_execute_pose(self, args: Dict[str, Any]) -> Dict[str, Any]:
        pose_name = args.get("pose_name")
        duration_ms = args.get("duration_ms")
        if not pose_name:
            raise ValueError("pose_name is required")

        await self._servo.execute_pose(pose_name, duration_ms=duration_ms)
        return {"pose": pose_name, "status": "done"}

    async def _fn_set_emotion(self, args: Dict[str, Any]) -> Dict[str, Any]:
        emotion = args.get("emotion", "neutral")
        pose_name = _EMOTION_TO_POSE.get(emotion, "neutral")
        await self._servo.execute_pose(pose_name)
        return {"emotion": emotion, "pose": pose_name, "status": "done"}

    async def _fn_walk_forward(self, args: Dict[str, Any]) -> Dict[str, Any]:
        steps = int(args.get("steps", 2))
        steps = max(1, min(20, steps))
        await self._servo.execute_trot(steps=steps)
        return {"steps": steps, "status": "done"}

    async def _fn_speak(self, args: Dict[str, Any]) -> Dict[str, Any]:
        text = args.get("text", "")
        language = args.get("language", "ja-JP")
        if not text:
            raise ValueError("text is required")
        await self._audio.speak(text, lang=language)
        return {"text": text, "status": "spoken"}

    async def _fn_get_system_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        import psutil

        cpu = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory().percent
        temp = None
        try:
            temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        except Exception:
            pass

        return {
            "cpu_pct": cpu,
            "mem_pct": mem,
            "temp_c": temp,
        }
