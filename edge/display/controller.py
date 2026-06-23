"""
edge/display/controller.py
===========================
SPI ディスプレイドライバ。

参考: https://asukiaaa.blogspot.com/2023/06/use-spi-display-by-python-on-raspberry-pi.html
adafruit-circuitpython-rgb-display を使う方式。

AIY Voice HAT ピンアサイン:
  SPI バス  : SPI0 (BCM 10=MOSI, 9=MISO, 11=SCLK)
  CS        : SPI0 CE0 (BCM 8)  → board.CE0
  DC  (A0)  : BCM 6  (Pin 31)  → board.D6
  RESET     : BCM 26 (Pin 37)  → board.D26
  BACKLIGHT : BCM 13 (Pin 33)  → RPi.GPIO PWM

依存 (Pi実機):
  sudo apt install -y python3-rpi.gpio python3-spidev python3-pil fonts-noto-cjk
  pip install adafruit-circuitpython-rgb-display
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Optional, Tuple

log = logging.getLogger(__name__)

PIN_DC        = 6
PIN_RESET     = 26
PIN_BACKLIGHT = 13
SPI_BUS       = 0
SPI_DEVICE    = 0
SPI_SPEED_HZ  = 40_000_000

DEFAULT_WIDTH  = 128
DEFAULT_HEIGHT = 160

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL = True
except ImportError:
    _PIL = False
    log.warning("[display] Pillow not available — rendering disabled")

try:
    import board
    import busio
    from digitalio import DigitalInOut, Direction
    from adafruit_rgb_display import st7735 as adafruit_st7735
    import RPi.GPIO as GPIO
    _HW = True
except ImportError:
    _HW = False
    log.warning("[display] luma.lcd / RPi.GPIO not available — mock mode")


class _MockDevice:
    def display(self, image): pass
    def cleanup(self):        pass


class DisplayController:
    """SPI LCDを非同期で制御するクラス。"""

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

        # バックライト (RPi.GPIO PWM)
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(PIN_BACKLIGHT, GPIO.OUT)
        self._pwm = GPIO.PWM(PIN_BACKLIGHT, 1000)
        self._pwm.start(100)

        # SPI + ピン設定 (adafruit-circuitpython-rgb-display 方式)
        cs_pin    = DigitalInOut(board.CE0)
        dc_pin    = DigitalInOut(board.D6)
        reset_pin = DigitalInOut(board.D26)

        spi = busio.SPI(clock=board.SCK, MOSI=board.MOSI, MISO=board.MISO)

        self._device = adafruit_st7735.ST7735R(
            spi,
            cs=cs_pin,
            dc=dc_pin,
            rst=reset_pin,
            width=self.width,
            height=self.height,
            rotation=90,
            baudrate=SPI_SPEED_HZ,
        )
        log.info("[display] SPI device ready (ST7735 DC=BCM%d RESET=BCM%d BL=BCM%d)",
                 PIN_DC, PIN_RESET, PIN_BACKLIGHT)

    async def shutdown(self) -> None:
        if _HW and hasattr(self, "_pwm"):
            self._pwm.ChangeDutyCycle(0)
            self._pwm.stop()
            GPIO.cleanup([PIN_BACKLIGHT])
        self._executor.shutdown(wait=True)
        log.info("[display] shutdown complete")

    # ── 描画メソッド ─────────────────────────────────────────

    def _display(self, img: "Image.Image") -> None:
        """PIL Imageをディスプレイに転送する。adafruit-rgb-displayはimage()メソッドを使う。"""
        if self._device is None:
            return
        try:
            # adafruit版は .image() / luma版は .display() どちらかを持つ
            if hasattr(self._device, 'image'):
                self._device.image(img)
            else:
                self._device.display(img)
        except Exception as exc:
            log.debug("[display] draw error: %s", exc)

    async def show_log(self, lines: list[tuple[str, tuple[int,int,int]]]) -> None:
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
        self._display(img)

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
        self._display(img)

    async def clear(self, color: Tuple[int,int,int] = (0,0,0)) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._draw_fill, color)

    def _draw_fill(self, color: Tuple[int,int,int]) -> None:
        if not _PIL:
            return
        img = Image.new("RGB", (self.width, self.height), color)
        self._display(img)

    async def show_image(self, image: "Image.Image") -> None:
        img = image.resize((self.width, self.height)).convert("RGB")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._display, img)
