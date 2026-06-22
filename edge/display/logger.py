"""
edge/display/logger.py
======================
Pythonのloggingシステムにフックし、重要なログをSPIディスプレイに
リアルタイム表示するモジュール。

使い方:
    from edge.display.logger import DisplayLogger
    from edge.display.controller import DisplayController

    display = DisplayController()
    await display.init()
    disp_logger = DisplayLogger(display)
    disp_logger.install()   # logging.root にハンドラを追加

以後、logging.INFO 以上のログが自動的にディスプレイに表示される。
"""

from __future__ import annotations

import asyncio
import logging
import textwrap
from collections import deque
from typing import Optional

# 表示するログの最大行数 (240px / 16px行高 = 14行)
MAX_LINES = 14
# 表示する最大文字数/行 (13px幅フォントで240pxに収まる目安)
CHARS_PER_LINE = 28

# 表示をスキップするALSAノイズパターン
_SKIP_PREFIXES = (
    "ALSA lib",
    "Cannot connect to server",
    "jack server",
    "JackShm",
    "Cannot connect to server socket",
)

# 重要度に応じた色 (R, G, B)
_LEVEL_COLOR = {
    logging.DEBUG:    (80, 80, 80),
    logging.INFO:     (0, 220, 0),
    logging.WARNING:  (220, 180, 0),
    logging.ERROR:    (220, 60, 60),
    logging.CRITICAL: (255, 0, 0),
}


def _shorten(text: str) -> list[str]:
    """1行のテキストを CHARS_PER_LINE で折り返して返す"""
    return textwrap.wrap(text, width=CHARS_PER_LINE) or [""]


class _DisplayHandler(logging.Handler):
    """logging.Handler の実装。ログをキューに追加してディスプレイに反映する。"""

    def __init__(self, logger: "DisplayLogger") -> None:
        super().__init__(level=logging.INFO)
        self._logger = logger

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        # ALSAノイズを除去
        if any(msg.startswith(p) for p in _SKIP_PREFIXES):
            return
        # ロガー名の短縮 (edge.audio.pipeline -> audio)
        name = record.name.split(".")[-1] if "." in record.name else record.name
        # フォーマット: [level] name: message
        prefix = f"[{'WRN' if record.levelno == logging.WARNING else record.levelname[0]}] {name}: "
        short = msg[:60]  # 長すぎるメッセージを切り詰め
        line = prefix + short
        self._logger._push(line, record.levelno)


class DisplayLogger:
    """
    ログ行を deque に蓄積し、変化があるたびにディスプレイを更新する。
    """

    def __init__(self, display) -> None:
        self._display = display
        self._lines: deque[tuple[str, int]] = deque(maxlen=MAX_LINES)
        self._handler = _DisplayHandler(self)
        self._dirty = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def install(self) -> None:
        """logging.root にハンドラを追加する。asyncio ループ開始後に呼ぶこと。"""
        self._loop = asyncio.get_event_loop()
        logging.root.addHandler(self._handler)
        # 起動メッセージを即時プッシュして初回描画をトリガー
        self._push("=== RobotChan ===", logging.INFO)
        self._push("Display logger OK", logging.INFO)
        # バックグラウンドで描画ループを起動
        asyncio.create_task(self._render_loop(), name="display_render")
        logging.getLogger(__name__).info("[display] logger installed")

    def _push(self, line: str, level: int) -> None:
        """ハンドラスレッドから呼ばれる。スレッドセーフにキューに追加。"""
        self._lines.append((line, level))
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._dirty.set)

    async def _render_loop(self) -> None:
        """変化があるたびにディスプレイを再描画する"""
        # 起動時に即時描画
        await self._render()
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            await self._render()
            # 高頻度更新を抑制 (最短50ms間隔)
            await asyncio.sleep(0.05)

    async def _render(self) -> None:
        lines_snapshot = list(self._lines)
        colored = [
            (text[:CHARS_PER_LINE], _LEVEL_COLOR.get(level, (0, 200, 0)))
            for text, level in lines_snapshot
        ]
        await self._display.show_log(colored)
