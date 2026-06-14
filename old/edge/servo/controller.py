"""
edge/servo/controller.py
=========================
デュアル PCA9685 対応サーボコントローラ。

PCA9685 #0 (0x40): 脚部 8ch
PCA9685 #1 (0x41): 首部 2ch

設計:
  - ServoId でアドレス+チャンネルを一意に指定
  - 補間はイージング関数付きの asyncio ベースソフト補間
  - I2C 書き込みは ThreadPoolExecutor で非同期化
  - ウォッチドッグ: サーバー切断時に stand ポーズへ自動退避
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import time
from typing import Dict, List, Optional, Tuple

from edge.servo.hardware import (
    ALL_SERVOS, SERVO_BY_NAME,
    ADDR_LEG, ADDR_HEAD,
    PULSE_MIN_US, PULSE_MAX_US, PULSE_NEUT_US,
    PWM_FREQ_HZ, ServoId,
)
from edge.servo.poses import POSE_CATALOG, Pose

log = logging.getLogger(__name__)

try:
    from adafruit_pca9685 import PCA9685 as _PCA9685
    import board, busio
    _HW = True
except ImportError:
    _HW = False
    log.warning("[servo] adafruit libs not available — mock mode")


# ──────────────────────────────────────
# Mock
# ──────────────────────────────────────
class _MockChannel:
    def __init__(self, addr: int, ch: int):
        self._addr, self._ch = addr, ch
        self.duty_cycle = 0

class _MockPCA:
    def __init__(self, addr: int):
        self.channels = [_MockChannel(addr, i) for i in range(16)]
        self.frequency = PWM_FREQ_HZ


# ──────────────────────────────────────
# イージング
# ──────────────────────────────────────
def _ease_linear(t: float)   -> float: return t
def _ease_out(t: float)      -> float: return 1.0 - (1.0 - t) ** 2
def _ease_in(t: float)       -> float: return t ** 2
def _ease_in_out(t: float)   -> float: return 0.5 - math.cos(math.pi * t) / 2

_EASINGS = {
    "linear":      _ease_linear,
    "ease_out":    _ease_out,
    "ease_in":     _ease_in,
    "ease_in_out": _ease_in_out,
}

# ──────────────────────────────────────
# パルス幅 → duty_cycle 変換
# PCA9685 は 12bit (0-4095) で PWM を制御
# 50Hz = 20ms 周期
# ──────────────────────────────────────
_PERIOD_US  = 1_000_000 / PWM_FREQ_HZ   # 20000μs
_DC_MAX     = 4095

def _pulse_to_dc(pulse_us: int) -> int:
    """マイクロ秒 → PCA9685 duty_cycle (0-4095)"""
    pulse_us = max(PULSE_MIN_US, min(PULSE_MAX_US, pulse_us))
    return int(pulse_us / _PERIOD_US * _DC_MAX)


# ──────────────────────────────────────
# ServoController
# ──────────────────────────────────────
class ServoController:
    """
    デュアル PCA9685 を束ねて全10サーボを非同期制御するクラス。

    使い方:
        ctrl = ServoController()
        await ctrl.init()
        await ctrl.execute_pose("stand")
        await ctrl.set_servo(HEAD_PAN, 1200)
        await ctrl.shutdown()
    """

    TICK_MS = 20   # 補間ステップ間隔 (50fps)

    def __init__(
        self,
        addr_leg:  int = ADDR_LEG,
        addr_head: int = ADDR_HEAD,
    ) -> None:
        self._addr_leg  = addr_leg
        self._addr_head = addr_head

        # PCA9685 インスタンス (init後に設定)
        self._pca_leg:  _MockPCA | _PCA9685 = None
        self._pca_head: _MockPCA | _PCA9685 = None

        # 現在パルス値キャッシュ (servo_name → pulse_us)
        self._current: Dict[str, int] = {
            s.name: PULSE_NEUT_US for s in ALL_SERVOS
        }

        self._lock      = asyncio.Lock()
        self._executor  = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="servo_i2c"
        )
        self._initialized = False

    # ── 初期化 / 終了 ─────────────────

    async def init(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._hw_init)
        self._initialized = True
        log.info("[servo] initialized (%s)", "HW" if _HW else "Mock")

    def _hw_init(self) -> None:
        if _HW:
            i2c = busio.I2C(board.SCL, board.SDA)
            self._pca_leg  = _PCA9685(i2c, address=self._addr_leg)
            self._pca_head = _PCA9685(i2c, address=self._addr_head)
            self._pca_leg.frequency  = PWM_FREQ_HZ
            self._pca_head.frequency = PWM_FREQ_HZ
        else:
            self._pca_leg  = _MockPCA(self._addr_leg)
            self._pca_head = _MockPCA(self._addr_head)

        # 全サーボを中立に
        for s in ALL_SERVOS:
            self._write_pulse_raw(s, PULSE_NEUT_US)
        time.sleep(0.5)

    async def shutdown(self) -> None:
        """全脚を stand に戻してから電源を切る"""
        log.info("[servo] shutting down — returning to stand")
        await self.execute_pose("stand", duration_ms=800)
        await asyncio.sleep(0.9)
        self._executor.shutdown(wait=True)

    # ── 公開 API ──────────────────────

    async def set_servo(
        self,
        servo: ServoId,
        pulse_us: int,
        duration_ms: int = 400,
        easing: str = "ease_out",
    ) -> None:
        """単一サーボを指定パルスに滑らかに動かす"""
        self._assert_ready()
        pulse_us = max(PULSE_MIN_US, min(PULSE_MAX_US, pulse_us))
        async with self._lock:
            await self._interpolate(
                targets={servo.name: pulse_us},
                duration_ms=duration_ms,
                easing=easing,
            )

    async def execute_pose(
        self,
        pose_name: str,
        duration_ms: Optional[int] = None,
        easing: Optional[str] = None,
    ) -> None:
        """
        ポーズ名でポーズを実行する。
        None パルスのサーボは現在値を維持する。
        """
        self._assert_ready()

        pose = POSE_CATALOG.get(pose_name)
        if pose is None:
            log.warning("[servo] unknown pose: '%s'", pose_name)
            return

        dur   = duration_ms if duration_ms is not None else pose.duration_ms
        ease  = easing      if easing      is not None else pose.easing

        # None をフィルタして targets を作る
        targets: Dict[str, int] = {
            name: pulse
            for name, pulse in pose.pulses.items()
            if pulse is not None
        }

        async with self._lock:
            await self._interpolate(targets, dur, ease)

        log.info("[servo] pose '%s' done", pose_name)

    async def execute_trot(self, steps: int = 4) -> None:
        """
        トロット歩行を steps サイクル分実行する。
        1サイクル = trot_a → trot_b。
        """
        for _ in range(steps):
            await self.execute_pose("trot_a")
            await self.execute_pose("trot_b")

    def get_all_pulses(self) -> Dict[str, int]:
        """現在の全サーボパルス値を返す (状態確認・デバッグ用)"""
        return dict(self._current)

    def get_pulse(self, servo: ServoId) -> int:
        return self._current.get(servo.name, PULSE_NEUT_US)

    # ── 補間エンジン ──────────────────

    async def _interpolate(
        self,
        targets: Dict[str, int],
        duration_ms: int,
        easing: str,
    ) -> None:
        """
        現在値 → 目標値をイージング付きで補間する。
        ループ内の I2C 書き込みは executor で非同期化。
        """
        if duration_ms <= self.TICK_MS:
            # 瞬時移動
            loop = asyncio.get_running_loop()
            for name, pulse in targets.items():
                servo = SERVO_BY_NAME[name]
                await loop.run_in_executor(
                    self._executor, self._write_pulse_raw, servo, pulse
                )
            return

        ease_fn = _EASINGS.get(easing, _ease_out)
        steps   = max(1, duration_ms // self.TICK_MS)
        starts  = {name: self._current[name] for name in targets}
        loop    = asyncio.get_running_loop()

        for step in range(1, steps + 1):
            t = ease_fn(step / steps)
            writes: List[Tuple[ServoId, int]] = []

            for name, target in targets.items():
                pulse = int(starts[name] + (target - starts[name]) * t)
                pulse = max(PULSE_MIN_US, min(PULSE_MAX_US, pulse))
                writes.append((SERVO_BY_NAME[name], pulse))

            # I2C 書き込みをバッチで executor に投げる
            await loop.run_in_executor(
                self._executor, self._batch_write, writes
            )
            await asyncio.sleep(self.TICK_MS / 1000)

    def _batch_write(self, writes: List[Tuple[ServoId, int]]) -> None:
        """スレッド内で複数サーボをまとめて書き込む"""
        for servo, pulse in writes:
            self._write_pulse_raw(servo, pulse)

    def _write_pulse_raw(self, servo: ServoId, pulse_us: int) -> None:
        """
        PCA9685 の正しいインスタンスを選んでパルスを書き込む。
        executor 内で呼ぶこと。
        """
        pca = self._pca_leg if servo.addr == self._addr_leg else self._pca_head
        dc  = _pulse_to_dc(pulse_us)

        if _HW:
            pca.channels[servo.channel].duty_cycle = dc
        else:
            pca.channels[servo.channel].duty_cycle = dc
            log.debug("[mock] %s (0x%02x ch%02d) → %dμs (dc=%d)",
                      servo.name, servo.addr, servo.channel, pulse_us, dc)

        self._current[servo.name] = pulse_us

    # ── 内部ヘルパー ──────────────────

    def _assert_ready(self) -> None:
        if not self._initialized:
            raise RuntimeError("ServoController.init() が呼ばれていません")
