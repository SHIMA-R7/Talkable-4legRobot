"""
edge/servo/controller.py
=========================
サーボコントローラ（実機検証済み: Adafruit_PCA9685 旧ライブラリ使用）。

新ライブラリ (adafruit-circuitpython-pca9685, busio/board) では
Raspberry Pi 3B + Python3.13 環境で I2C アドレス解決に失敗したため、
旧ライブラリ Adafruit_PCA9685 (busnum=1 明示指定) を採用している。
pip install Adafruit_PCA9685

キャリブレーションは hardware.py の CALIB_CENTER/P1/P2 (PCA9685生カウント値)
を用いた変換で行う。これは実機Arduinoスケッチの値をそのまま移植したもの。

PCA9685 #1 (0x41, 首部) は基板未接続の場合は自動的にモックへフォールバックし、
脚部のみで動作を継続する。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import time
from typing import Dict, List, Tuple

from edge.servo.hardware import (
    ALL_SERVOS, SERVO_BY_NAME, ADDR_LEG, ADDR_HEAD,
    PULSE_MIN_US, PULSE_MAX_US, PULSE_NEUT_US, PWM_FREQ_HZ, ServoId,
    CALIB_CENTER, CALIB_P1, CALIB_P2,
)
from edge.servo.poses import POSE_CATALOG

log = logging.getLogger(__name__)

try:
    import Adafruit_PCA9685
    _HW = True
except ImportError:
    _HW = False
    log.warning("[servo] Adafruit_PCA9685 not available — mock mode")


class _MockPCA:
    """非Pi環境 / I2Cデバイス未検出時のモック"""
    def set_pwm(self, ch: int, on: int, off: int) -> None:
        log.debug("[mock_pca] ch%d -> count=%d", ch, off)

    def set_pwm_freq(self, freq: int) -> None:
        pass


# ──────────────────────────────────────
# イージング関数
# ──────────────────────────────────────
def _ease_linear(t: float) -> float: return t
def _ease_out(t: float)    -> float: return 1.0 - (1.0 - t) ** 2
def _ease_in(t: float)     -> float: return t ** 2
def _ease_in_out(t: float) -> float: return 0.5 - math.cos(math.pi * t) / 2

_EASINGS = {
    "linear":      _ease_linear,
    "ease_out":    _ease_out,
    "ease_in":     _ease_in,
    "ease_in_out": _ease_in_out,
}


def _val_to_count(servo_name: str, pulse_us: int) -> int:
    """
    pulse_us (500-2500, 中立1500) を PCA9685 生カウント値 (0-4095) に変換する。

    キャリブ定義がある場合:
      pulse_us <= 1500: CENTER と P1 (前方/持ち上げ方向の最大) の間を線形補間
      pulse_us >  1500: CENTER と P2 (後方/接地方向の最大)     の間を線形補間

    キャリブ未定義 (例: 首部 0x41 が接続されていない場合) は
    50Hz/4096段階のシンプルな線形変換にフォールバックする。
    """
    c = CALIB_CENTER.get(servo_name)
    if c is None:
        return int(pulse_us / 20000 * 4096)

    p1 = CALIB_P1.get(servo_name, c)
    p2 = CALIB_P2.get(servo_name, c)

    if pulse_us <= PULSE_NEUT_US:
        val = (PULSE_NEUT_US - pulse_us) / (PULSE_NEUT_US - PULSE_MIN_US)
        return int(c + (p1 - c) * val)
    else:
        val = (pulse_us - PULSE_NEUT_US) / (PULSE_MAX_US - PULSE_NEUT_US)
        return int(c + (p2 - c) * val)


class ServoController:
    """
    全10サーボ (脚部8 + 首部2) を非同期制御するクラス。

    使い方:
        ctrl = ServoController()
        await ctrl.init()
        await ctrl.execute_pose("stand")
        await ctrl.set_servo(HEAD_PAN, 1200)
        await ctrl.shutdown()
    """

    TICK_MS = 20  # 補間ステップ間隔 (50fps)

    def __init__(self, addr_leg: int = ADDR_LEG, addr_head: int = ADDR_HEAD) -> None:
        self._addr_leg  = addr_leg
        self._addr_head = addr_head
        self._pca_leg   = None
        self._pca_head  = None
        self._current: Dict[str, int] = {s.name: PULSE_NEUT_US for s in ALL_SERVOS}
        self._lock = asyncio.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="servo_i2c"
        )
        self._initialized = False

    # ── 初期化 / 終了 ─────────────────────

    async def init(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._hw_init)
        self._initialized = True
        log.info("[servo] initialized (%s)", "HW" if _HW else "Mock")

    def _hw_init(self) -> None:
        if _HW:
            self._pca_leg = Adafruit_PCA9685.PCA9685(address=self._addr_leg, busnum=1)
            self._pca_leg.set_pwm_freq(PWM_FREQ_HZ)
            log.info("[servo] pca_leg (0x%02x) OK", self._addr_leg)

            try:
                self._pca_head = Adafruit_PCA9685.PCA9685(address=self._addr_head, busnum=1)
                self._pca_head.set_pwm_freq(PWM_FREQ_HZ)
                log.info("[servo] pca_head (0x%02x) OK", self._addr_head)
            except Exception:
                log.warning("[servo] pca_head (0x%02x) not found — head servos disabled",
                             self._addr_head)
                self._pca_head = _MockPCA()
        else:
            self._pca_leg  = _MockPCA()
            self._pca_head = _MockPCA()

        for s in ALL_SERVOS:
            self._write_raw(s, PULSE_NEUT_US)
        time.sleep(0.5)

    async def shutdown(self) -> None:
        log.info("[servo] shutdown — returning to neutral")
        await self.execute_pose("neutral", duration_ms=600)
        await asyncio.sleep(0.7)
        self._executor.shutdown(wait=True)

    # ── 公開API ──────────────────────────

    async def set_servo(self, servo: ServoId, pulse_us: int,
                         duration_ms: int = 400, easing: str = "ease_out") -> None:
        self._assert_ready()
        pulse_us = max(PULSE_MIN_US, min(PULSE_MAX_US, pulse_us))
        async with self._lock:
            await self._interpolate({servo.name: pulse_us}, duration_ms, easing)

    async def execute_pose(self, pose_name: str,
                            duration_ms: int | None = None,
                            easing: str | None = None) -> None:
        self._assert_ready()
        pose = POSE_CATALOG.get(pose_name)
        if pose is None:
            log.warning("[servo] unknown pose: '%s'", pose_name)
            return

        dur  = duration_ms if duration_ms is not None else pose.duration_ms
        ease = easing      if easing      is not None else pose.easing
        targets = {k: v for k, v in pose.pulses.items() if v is not None}

        async with self._lock:
            await self._interpolate(targets, dur, ease)
        log.info("[servo] pose '%s' done", pose_name)

    async def execute_trot(self, steps: int = 4) -> None:
        for _ in range(steps):
            await self.execute_pose("trot_a")
            await self.execute_pose("trot_b")

    def get_all_pulses(self) -> Dict[str, int]:
        return dict(self._current)

    def get_pulse(self, servo: ServoId) -> int:
        return self._current.get(servo.name, PULSE_NEUT_US)

    # ── 補間エンジン ─────────────────────

    async def _interpolate(self, targets: Dict[str, int], duration_ms: int, easing: str) -> None:
        if duration_ms <= self.TICK_MS:
            loop = asyncio.get_running_loop()
            for name, pulse in targets.items():
                s = SERVO_BY_NAME[name]
                await loop.run_in_executor(self._executor, self._write_raw, s, pulse)
            return

        ease_fn = _EASINGS.get(easing, _ease_out)
        steps   = max(1, duration_ms // self.TICK_MS)
        starts  = {n: self._current[n] for n in targets}
        loop    = asyncio.get_running_loop()

        for step in range(1, steps + 1):
            t = ease_fn(step / steps)
            writes: List[Tuple[ServoId, int]] = []
            for name, target in targets.items():
                pulse = int(starts[name] + (target - starts[name]) * t)
                pulse = max(PULSE_MIN_US, min(PULSE_MAX_US, pulse))
                writes.append((SERVO_BY_NAME[name], pulse))
            await loop.run_in_executor(self._executor, self._batch_write, writes)
            await asyncio.sleep(self.TICK_MS / 1000)

    def _batch_write(self, writes: List[Tuple[ServoId, int]]) -> None:
        for servo, pulse in writes:
            self._write_raw(servo, pulse)

    def _write_raw(self, servo: ServoId, pulse_us: int) -> None:
        pca = self._pca_leg if servo.addr == self._addr_leg else self._pca_head
        count = _val_to_count(servo.name, pulse_us)
        count = max(0, min(4095, count))
        pca.set_pwm(servo.channel, 0, count)
        self._current[servo.name] = pulse_us

    def _assert_ready(self) -> None:
        if not self._initialized:
            raise RuntimeError("ServoController.init() が呼ばれていません")
