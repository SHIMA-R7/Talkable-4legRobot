"""
tests/test_stage2_display.py
=============================
Stage 2 ディスプレイ層のユニットテスト。
実機なしでモックモードで全テスト通過する。
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from edge.display.controller import (
    DisplayController,
    PIN_DC, PIN_RESET, PIN_BACKLIGHT,
    SPI_BUS, SPI_DEVICE,
)


class TestPinAssignments:
    """AIY Voice HAT ピンアサインの確認"""

    def test_dc_pin_is_servo1_breakout(self):
        # Servo 1 Breakout = BCM 6 (Pin 31)
        assert PIN_DC == 6

    def test_reset_pin_is_servo0_breakout(self):
        # Servo 0 Breakout = BCM 26 (Pin 37)
        assert PIN_RESET == 26

    def test_backlight_pin_is_servo2_breakout(self):
        # Servo 2 Breakout = BCM 13 (Pin 33) — HW PWM ch1
        assert PIN_BACKLIGHT == 13

    def test_spi_bus_is_spi0(self):
        assert SPI_BUS == 0

    def test_spi_device_is_ce0(self):
        # CE0 = BCM 8
        assert SPI_DEVICE == 0

    def test_no_conflict_with_servo_channels(self):
        """
        SPI制御ピン (BCM 6, 13, 26) が
        PCA9685 のI2Cピン (BCM 2, 3) と衝突しないことを確認
        """
        i2c_pins = {2, 3}   # SDA, SCL
        spi_ctrl_pins = {PIN_DC, PIN_RESET, PIN_BACKLIGHT}
        assert not i2c_pins & spi_ctrl_pins, \
            "SPI制御ピンがI2Cピンと衝突しています"

    def test_no_conflict_with_i2s_pins(self):
        """
        SPI制御ピンがAIY Voice HAT I2S ピンと衝突しないことを確認
        I2S: BCM 18(BCLK), 19(LRCLK), 20(DIN), 21(DOUT)
        """
        i2s_pins = {18, 19, 20, 21}
        spi_ctrl_pins = {PIN_DC, PIN_RESET, PIN_BACKLIGHT}
        assert not i2s_pins & spi_ctrl_pins


class TestDisplayController:

    @pytest.fixture
    def disp(self):
        d = DisplayController(driver="st7789", width=240, height=240)
        asyncio.get_event_loop().run_until_complete(d.init())
        return d

    def test_init_mock_mode(self, disp):
        assert disp._initialized is True
        assert disp._device is not None

    def test_default_backlight_full(self, disp):
        assert disp._backlight_pct == 1.0

    def test_set_backlight_clamps(self, disp):
        asyncio.get_event_loop().run_until_complete(
            disp.set_backlight(2.0)
        )
        assert disp._backlight_pct == 1.0

        asyncio.get_event_loop().run_until_complete(
            disp.set_backlight(-0.5)
        )
        assert disp._backlight_pct == 0.0

    def test_set_backlight_value(self, disp):
        asyncio.get_event_loop().run_until_complete(
            disp.set_backlight(0.7)
        )
        assert abs(disp._backlight_pct - 0.7) < 0.001

    def test_clear_no_exception(self, disp):
        asyncio.get_event_loop().run_until_complete(
            disp.clear((0, 0, 0))
        )

    def test_show_status_no_exception(self, disp):
        for emotion in ["neutral", "happy", "sad", "surprised",
                        "thinking", "excited", "sleepy", "angry"]:
            asyncio.get_event_loop().run_until_complete(
                disp.show_status("テスト", emotion=emotion)
            )

    def test_show_debug_no_exception(self, disp):
        asyncio.get_event_loop().run_until_complete(
            disp.show_debug(["line1", "line2", "CPU: 10%"])
        )

    def test_fade_backlight_no_exception(self, disp):
        asyncio.get_event_loop().run_until_complete(
            disp.fade_backlight(0.3, duration_ms=100)
        )
        assert abs(disp._backlight_pct - 0.3) < 0.05

    def test_shutdown_no_exception(self, disp):
        asyncio.get_event_loop().run_until_complete(
            disp.shutdown()
        )


class TestSettings:
    """settings.yaml のピン定義との整合確認"""

    def test_yaml_pin_values_match_constants(self):
        import yaml
        yaml_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        if not yaml_path.exists():
            pytest.skip("settings.yaml not found")

        with yaml_path.open() as f:
            cfg = yaml.safe_load(f)

        disp_cfg = cfg.get("display", {})
        assert disp_cfg["pin_dc"]        == PIN_DC
        assert disp_cfg["pin_reset"]     == PIN_RESET
        assert disp_cfg["pin_backlight"] == PIN_BACKLIGHT
        assert disp_cfg["spi_bus"]       == SPI_BUS
        assert disp_cfg["spi_device"]    == SPI_DEVICE

    def test_yaml_servo_leg_channels(self):
        import yaml
        yaml_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        if not yaml_path.exists():
            pytest.skip("settings.yaml not found")

        with yaml_path.open() as f:
            cfg = yaml.safe_load(f)

        leg_ch = cfg["servo"]["leg"]["channels"]
        # ピンアサインテーブルとの完全一致確認
        assert leg_ch["RF_hip"]  == 14
        assert leg_ch["LF_hip"]  == 15
        assert leg_ch["RR_hip"]  == 12
        assert leg_ch["LR_hip"]  == 13
        assert leg_ch["RF_knee"] == 10
        assert leg_ch["LF_knee"] == 11
        assert leg_ch["RR_knee"] ==  8
        assert leg_ch["LR_knee"] ==  9

        head_ch = cfg["servo"]["head"]["channels"]
        assert head_ch["head_tilt"] == 15
        assert head_ch["head_pan"]  == 14


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
