"""
edge/display/controller.py
===========================
SPI ディスプレイドライバ。

AIY Voice HAT ピンアサイン:
  SPI バス  : SPI0 (BCM 10=MOSI, 9=MISO, 11=SCLK)
  CS        : SPI0 CE0 (BCM 8)
  DC  (A0)  : Servo 1 Breakout = BCM 6  (Pin 31)
  RESET     : Servo 0 Breakout = BCM 26 (Pin 37)
  BACKLIGHT : Servo 2 Breakout = BCM 13 (Pin 33)  ← PWM対応ピン

対応パネル: ST7789 / ILI9341 系の小型 SPI LCD
  (コンストラクタの `driver` 引数で切り替え可)

依存ライブラリ (Pi 実機):
  pip install luma.lcd RPi.GPIO pillow

設計:
  - 描画は Pillow Image → luma.lcd のバッファ転送
  - バックライトは BCM 13 の HW PWM で輝度制御
    (GPIO 13 は Pi の HW PWM ch1 に対応)
  - 非同期呼び出し対応: draw_* メソッドは run_in_executor でラップ
  - ステータス表示 / 感情アイコン表示 / デバッグ情報表示を提供
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# ── ピン定数 (AIY Voice HAT 準拠) ────────────────
PIN_DC        = 6    # BCM: Servo 1 Breakout → DC / A0
PIN_RESET     = 26   # BCM: Servo 0 Breakout → RESET
PIN_BACKLIGHT = 13   # BCM: Servo 2 Breakout → Backlight (HW PWM ch1)
SPI_BUS       = 0    # SPI0
SPI_DEVICE    = 0    # CE0 (BCM 8)
SPI_SPEED_HZ  = 40_000_000  # 40 MHz

# デフォルト解像度 (ST7789 240x240 / ILI9341 240x320 など)
DEFAULT_WIDTH  = 240
DEFAULT_HEIGHT = 240

# Pillow はモック環境でも使用するため独立してインポート
try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL = True
except ImportError:
    _PIL = False
    log.warning("[display] Pillow not available — rendering disabled")

try:
    import RPi.GPIO as GPIO
    from luma.core.interface.serial import spi
    from luma.lcd.device import st7789, ili9341
    _HW = True
except ImportError:
    _HW = False
    log.warning("[display] luma.lcd / RPi.GPIO not available — mock mode")


# ── Mock ─────────────────────────────────────────
class _MockDevice:
    width  = DEFAULT_WIDTH
    height = DEFAULT_HEIGHT

    def display(self, image): pass
    def cleanup(self):        pass
    def backlight(self, v):   pass


# ── DisplayController ─────────────────────────────
class DisplayController:
    """
    SPI LCDを非同期で制御するクラス。

    使い方:
        disp = DisplayController()
        await disp.init()
        await disp.show_status("待機中", emotion="neutral")
        await disp.set_backlight(0.8)
        await disp.shutdown()
    """

    def __init__(
        self,
        driver: str = "st7789",
        width:  int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
    ) -> None:
        self._driver_name = driver
        self.width  = width
        self.height = height
        self._device = None
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="display_spi"
        )
        self._initialized = False
        self._backlight_pct = 1.0

    # ── 初期化 / 終了 ─────────────────────────────

    async def init(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._hw_init)
        self._initialized = True
        log.info("[display] initialized (%s %dx%d, %s)",
                 self._driver_name, self.width, self.height,
                 "HW" if _HW else "Mock")

    def _hw_init(self) -> None:
        if not _HW:
            self._device = _MockDevice()
            return

        # GPIO 初期化
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        # バックライト: HW PWM (BCM 13 = PWM ch1)
        GPIO.setup(PIN_BACKLIGHT, GPIO.OUT)
        self._pwm = GPIO.PWM(PIN_BACKLIGHT, 1000)  # 1kHz
        self._pwm.start(100)  # 100% = 最大輝度

        # SPI インターフェース
        serial = spi(
            port=SPI_BUS,
            device=SPI_DEVICE,
            gpio=GPIO,
            reset_pin=PIN_RESET,
            dc_pin=PIN_DC,
            bus_speed_hz=SPI_SPEED_HZ,
            gpio_mode=GPIO.BCM,
        )

        # ドライバ選択
        drivers = {"st7789": st7789, "ili9341": ili9341}
        driver_cls = drivers.get(self._driver_name, st7789)
        self._device = driver_cls(
            serial,
            width=self.width,
            height=self.height,
            rotate=0,
        )
        log.info("[display] SPI device ready (DC=BCM%d RESET=BCM%d BL=BCM%d)",
                 PIN_DC, PIN_RESET, PIN_BACKLIGHT)

    async def shutdown(self) -> None:
        await self.set_backlight(0.0)
        await asyncio.sleep(0.2)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._hw_shutdown)
        self._executor.shutdown(wait=True)
        log.info("[display] shutdown complete")

    def _hw_shutdown(self) -> None:
        if _HW and self._device:
            self._device.cleanup()
            if hasattr(self, "_pwm"):
                self._pwm.stop()
            GPIO.cleanup([PIN_BACKLIGHT, PIN_DC, PIN_RESET])

    # ── バックライト制御 ──────────────────────────

    async def set_backlight(self, brightness: float) -> None:
        """
        バックライト輝度を設定する。
        brightness: 0.0 (消灯) 〜 1.0 (最大)
        BCM 13 は HW PWM ch1 に対応しているため、CPU 負荷なしに滑らかな調光が可能。
        """
        self._backlight_pct = max(0.0, min(1.0, brightness))
        pct = self._backlight_pct * 100
        if _HW and hasattr(self, "_pwm"):
            self._pwm.ChangeDutyCycle(pct)
        else:
            log.debug("[display] backlight → %.0f%%", pct)

    async def fade_backlight(self, target: float, duration_ms: int = 500) -> None:
        """バックライトをフェードイン/アウトする"""
        start = self._backlight_pct
        steps = max(1, duration_ms // 20)
        for i in range(1, steps + 1):
            val = start + (target - start) * (i / steps)
            await self.set_backlight(val)
            await asyncio.sleep(0.02)

    # ── 描画 API ─────────────────────────────────

    async def clear(self, color: Tuple[int, int, int] = (0, 0, 0)) -> None:
        """画面を指定色でクリアする"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._draw_fill, color)

    def _draw_fill(self, color: Tuple[int, int, int]) -> None:
        if not _PIL:
            return
        img = Image.new("RGB", (self.width, self.height), color)
        self._device.display(img)

    async def show_status(
        self,
        text: str,
        emotion: str = "neutral",
        sub_text: str = "",
        bg_color: Optional[Tuple[int, int, int]] = None,
    ) -> None:
        """
        ステータステキストと感情アイコンを表示する。
        ロボットの現在状態を示すメイン画面。
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor,
            self._render_status, text, emotion, sub_text, bg_color,
        )

    def _render_status(
        self,
        text: str,
        emotion: str,
        sub_text: str,
        bg_color: Optional[Tuple[int, int, int]],
    ) -> None:
        if not _PIL:
            return
        # 感情 → 背景色・アイコン文字マッピング
        EMOTION_STYLE = {
            "neutral":   ((30,  30,  40),  "･_･"),
            "happy":     ((30,  60,  30),  "^▽^"),
            "sad":       ((20,  20,  60),  "；_；"),
            "surprised": ((60,  40,  10),  "OwO"),
            "thinking":  ((30,  30,  50),  "･ω･"),
            "excited":   ((60,  20,  20),  "♪♪♪"),
            "sleepy":    ((10,  10,  30),  "-_-"),
            "angry":     ((60,  10,  10),  "＞﹏＜"),
        }
        bg, icon = EMOTION_STYLE.get(emotion, EMOTION_STYLE["neutral"])
        if bg_color:
            bg = bg_color

        img  = Image.new("RGB", (self.width, self.height), bg)
        draw = ImageDraw.Draw(img)

        # フォント (Pillow デフォルト)
        try:
            from PIL import ImageFont
            font_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
            font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
            font_ic = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
        except Exception:
            font_lg = font_sm = font_ic = ImageFont.load_default()

        cx = self.width // 2

        # 感情アイコン
        draw.text((cx, 40),  icon,  font=font_ic, fill=(220, 220, 220), anchor="mm")
        # メインテキスト
        draw.text((cx, 110), text,  font=font_lg, fill=(255, 255, 255), anchor="mm")
        # サブテキスト
        if sub_text:
            draw.text((cx, 150), sub_text, font=font_sm,
                      fill=(180, 180, 180), anchor="mm")
        # 下部区切り線
        draw.line([(20, self.height-30), (self.width-20, self.height-30)],
                  fill=(80, 80, 80), width=1)

        self._device.display(img)

    async def show_debug(self, lines: list[str]) -> None:
        """デバッグ情報を複数行で表示する"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._render_debug, lines)

    def _render_debug(self, lines: list[str]) -> None:
        if not _PIL:
            return
        img  = Image.new("RGB", (self.width, self.height), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            from PIL import ImageFont
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 13
            )
        except Exception:
            font = ImageFont.load_default()

        for i, line in enumerate(lines[:14]):  # 最大14行
            draw.text((4, 4 + i * 16), line, font=font, fill=(0, 255, 0))

        self._device.display(img)

    async def show_image(self, image: "Image.Image") -> None:
        """Pillow Image を直接表示する (カメラフレーム等)"""
        img = image.resize((self.width, self.height)).convert("RGB")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._device.display, img)
