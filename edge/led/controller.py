"""
edge/led/controller.py
======================
PCA9685 (I2C 0x60) に接続したフルカラーLEDの制御。

配線:
  CH 13: G (グリーン)
  CH 12: R (レッド)
  CH 11: B (ブルー)

PCA9685 の PWM は 0=フル点灯 / 4095=消灯 (アノードコモン) の場合と
0=消灯 / 4095=フル点灯 (カソードコモン) の場合がある。
ここでは一般的なカソードコモン (0=消灯 / 4095=フル) を前提とする。
アノードコモンの場合は LED_INVERT = True にすること。

状態一覧 (LedState):
  BOOT        : 起動中   → 赤点滅
  IDLE        : 待機中   → 緑ゆっくり点滅
  LISTENING   : 会話受付 → 青点灯
  THINKING    : 処理中   → 黄色パルス
  SPEAKING    : 発声中   → 白点灯
  ERROR       : エラー   → 赤高速点滅
  OFF         : 消灯
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Optional, Tuple

log = logging.getLogger(__name__)

ADDR_LED  = 0x60
CH_GREEN  = 13
CH_RED    = 12
CH_BLUE   = 11

LED_INVERT = False   # カソードコモン=False, アノードコモン=True

try:
    import Adafruit_PCA9685
    _HW = True
except ImportError:
    _HW = False
    log.warning("[led] Adafruit_PCA9685 not available — mock mode")


class LedState(Enum):
    BOOT      = "boot"
    IDLE      = "idle"
    LISTENING = "listening"
    THINKING  = "thinking"
    SPEAKING  = "speaking"
    ERROR     = "error"
    OFF       = "off"


def _duty(brightness: float) -> int:
    """0.0〜1.0 の明るさを PCA9685 カウント値 (0〜4095) に変換"""
    v = int(brightness * 4095)
    v = max(0, min(4095, v))
    return (4095 - v) if LED_INVERT else v


class LedController:
    """フルカラーLED制御クラス"""

    def __init__(self) -> None:
        self._pca: Optional[object] = None
        self._state = LedState.OFF
        self._anim_task: Optional[asyncio.Task] = None
        self._running = False

    def init(self) -> None:
        if not _HW:
            log.info("[led] mock mode")
            return
        try:
            self._pca = Adafruit_PCA9685.PCA9685(address=ADDR_LED, busnum=1)
            self._pca.set_pwm_freq(1000)   # 1kHz PWM (LEDに適した周波数)
            self._set_raw(0, 0, 0)
            log.info("[led] initialized (HW, addr=0x%02X CH G=%d R=%d B=%d)",
                     ADDR_LED, CH_GREEN, CH_RED, CH_BLUE)
        except Exception as exc:
            log.warning("[led] init failed: %s — mock mode", exc)
            self._pca = None

    def shutdown(self) -> None:
        self._stop_anim()
        self._set_raw(0, 0, 0)
        log.info("[led] shutdown")

    # ── 内部: 生PWM設定 ──────────────────────────────────────
    def _set_raw(self, r: float, g: float, b: float) -> None:
        """r, g, b: 各 0.0〜1.0"""
        if self._pca is None:
            return
        try:
            self._pca.set_pwm(CH_RED,   0, _duty(r))
            self._pca.set_pwm(CH_GREEN, 0, _duty(g))
            self._pca.set_pwm(CH_BLUE,  0, _duty(b))
        except Exception as exc:
            log.debug("[led] set_raw error: %s", exc)

    # ── 状態変更 ─────────────────────────────────────────────
    def set_state(self, state: LedState) -> None:
        if state == self._state:
            return
        self._state = state
        self._stop_anim()
        self._anim_task = asyncio.create_task(
            self._animate(state), name=f"led_{state.value}"
        )

    def _stop_anim(self) -> None:
        if self._anim_task and not self._anim_task.done():
            self._anim_task.cancel()
        self._anim_task = None

    # ── アニメーション ────────────────────────────────────────
    async def _animate(self, state: LedState) -> None:
        try:
            if state == LedState.BOOT:
                await self._blink(r=1.0, g=0.0, b=0.0, on=0.3, off=0.3)
            elif state == LedState.IDLE:
                await self._breathe(r=0.0, g=1.0, b=0.0, period=3.0)
            elif state == LedState.LISTENING:
                self._set_raw(0, 0, 1.0)
                await asyncio.sleep(9999)
            elif state == LedState.THINKING:
                await self._breathe(r=1.0, g=0.8, b=0.0, period=0.8)
            elif state == LedState.SPEAKING:
                self._set_raw(1.0, 1.0, 1.0)
                await asyncio.sleep(9999)
            elif state == LedState.ERROR:
                await self._blink(r=1.0, g=0.0, b=0.0, on=0.1, off=0.1)
            elif state == LedState.OFF:
                self._set_raw(0, 0, 0)
        except asyncio.CancelledError:
            self._set_raw(0, 0, 0)

    async def _blink(self, r: float, g: float, b: float,
                     on: float, off: float) -> None:
        while True:
            self._set_raw(r, g, b)
            await asyncio.sleep(on)
            self._set_raw(0, 0, 0)
            await asyncio.sleep(off)

    async def _breathe(self, r: float, g: float, b: float,
                        period: float) -> None:
        import math
        step = 0.03
        t = 0.0
        while True:
            brightness = (math.sin(2 * math.pi * t / period) + 1) / 2
            self._set_raw(r * brightness, g * brightness, b * brightness)
            await asyncio.sleep(step)
            t += step
