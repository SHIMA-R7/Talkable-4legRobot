"""
edge/display/controller.py
===========================
SPI ディスプレイドライバ。

AIY Voice HAT ピンアサイン:
  SPI バス  : SPI0 (BCM 10=MOSI, 9=MISO, 11=SCLK)
  CS        : SPI0 CE0 (BCM 8)
  DC  (A0)  : Servo 1 Breakout = BCM 6  (Pin 31)
  RESET     : Servo 0 Breakout = BCM 26 (Pin 37)
  BACKLIGHT : Servo 2 Breakout = BCM 13 (Pin 33)  HW PWM ch1

依存 (Pi実機):
  pip install luma.lcd RPi.GPIO pillow spidev
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from typing import Optional, Tuple

log = logging.getLogger(__name__)

PIN_DC        = 6
PIN_RESET     = 26
PIN_BACKLIGHT = 13
SPI_BUS       = 0
SPI_DEVICE    = 0
SPI_SPEED_HZ  = 40_000_000

DEFAULT_WIDTH  = 240
DEFAULT_HEIGHT = 240

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


class _MockDevice:
    width  = DEFAULT_WIDTH
    height = DEFAULT_HEIGHT
    def display(self, image): pass
    def cleanup(self):        pass


class DisplayController:
    """
    SPI LCDを非同期で制御するクラス。
    """

    def __init__(self, driver: str = "st7789",
                 width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT) -> None:
        self._driver_name = driver
        self.width  = width
        self.height = height
        self._device = None
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="display_spi"
        )
        self._initialized = False
        self._backlight_pct = 1.0

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

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        GPIO.setup(PIN_BACKLIGHT, GPIO.OUT)
        self._pwm = GPIO.PWM(PIN_BACKLIGHT, 1000)
        self._pwm.start(100)

        serial = spi(
            port=SPI_BUS,
            device=SPI_DEVICE,
            gpio=GPIO,
            reset_pin=PIN_RESET,
            dc_pin=PIN_DC,
            bus_speed_hz=SPI_SPEED_HZ,
            gpio_mode=GPIO.BCM,
        )

        drivers = {"st7789": st7789, "ili9341": ili9341}
        driver_cls = drivers.get(self._driver_name, st7789)
        self._device = driver_cls(serial, width=self.width, height=self.height, rotate=0)
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

    async def set_backlight(self, brightness: float) -> None:
        self._backlight_pct = max(0.0, min(1.0, brightness))
        pct = self._backlight_pct * 100
        if _HW and hasattr(self, "_pwm"):
            self._pwm.ChangeDutyCycle(pct)
        else:
            log.debug("[display] backlight -> %.0f%%", pct)

    async def fade_backlight(self, target: float, duration_ms: int = 500) -> None:
        start = self._backlight_pct
        steps = max(1, duration_ms // 20)
        for i in range(1, steps + 1):
            val = start + (target - start) * (i / steps)
            await self.set_backlight(val)
            await asyncio.sleep(0.02)

    async def clear(self, color: Tuple[int, int, int] = (0, 0, 0)) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._draw_fill, color)

    def _draw_fill(self, color: Tuple[int, int, int]) -> None:
        if not _PIL:
            return
        img = Image.new("RGB", (self.width, self.height), color)
        self._device.display(img)

    async def show_status(self, text: str, emotion: str = "neutral",
                           sub_text: str = "",
                           bg_color: Optional[Tuple[int, int, int]] = None) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor, self._render_status, text, emotion, sub_text, bg_color
        )

    def _render_status(self, text: str, emotion: str, sub_text: str,
                        bg_color: Optional[Tuple[int, int, int]]) -> None:
        if not _PIL:
            return

        EMOTION_STYLE = {
            "neutral":   ((30,  30,  40),  "._."),
            "happy":     ((30,  60,  30),  "^_^"),
            "sad":       ((20,  20,  60),  ";_;"),
            "surprised": ((60,  40,  10),  "O_O"),
            "thinking":  ((30,  30,  50),  ".oO"),
            "excited":   ((60,  20,  20),  "*_*"),
            "sleepy":    ((10,  10,  30),  "-_-"),
            "angry":     ((60,  10,  10),  ">_<"),
        }
        bg, icon = EMOTION_STYLE.get(emotion, EMOTION_STYLE["neutral"])
        if bg_color:
            bg = bg_color

        img  = Image.new("RGB", (self.width, self.height), bg)
        draw = ImageDraw.Draw(img)

        try:
            font_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
            font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
            font_ic = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
        except Exception:
            font_lg = font_sm = font_ic = ImageFont.load_default()

        cx = self.width // 2
        draw.text((cx, 40),  icon,  font=font_ic, fill=(220, 220, 220), anchor="mm")
        draw.text((cx, 110), text,  font=font_lg, fill=(255, 255, 255), anchor="mm")
        if sub_text:
            draw.text((cx, 150), sub_text, font=font_sm, fill=(180, 180, 180), anchor="mm")
        draw.line([(20, self.height-30), (self.width-20, self.height-30)],
                  fill=(80, 80, 80), width=1)

        self._device.display(img)

    async def show_log(self, lines: list[tuple[str, tuple[int,int,int]]]) -> None:
        """
        色付きログ行をディスプレイに描画する。
        lines: [(text, (R,G,B)), ...] のリスト
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._render_log, lines)

    def _render_log(self, lines: list[tuple[str, tuple[int,int,int]]]) -> None:
        if not _PIL:
            return
        img  = Image.new("RGB", (self.width, self.height), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 13
            )
        except Exception:
            font = ImageFont.load_default()
        for i, (text, color) in enumerate(lines[:14]):
            draw.text((2, 2 + i * 16), text, font=font, fill=color)
        self._device.display(img)

    async def show_debug(self, lines: list[str]) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._render_debug, lines)

    def _render_debug(self, lines: list[str]) -> None:
        if not _PIL:
            return
        img  = Image.new("RGB", (self.width, self.height), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 13)
        except Exception:
            font = ImageFont.load_default()
        for i, line in enumerate(lines[:14]):
            draw.text((4, 4 + i * 16), line, font=font, fill=(0, 255, 0))
        self._device.display(img)

    async def show_image(self, image: "Image.Image") -> None:
        img = image.resize((self.width, self.height)).convert("RGB")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._device.display, img)
