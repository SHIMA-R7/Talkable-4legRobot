"""
tests/test_stage2_servo.py
===========================
Stage 2 サーボ層のユニットテスト。
実機 (adafruit ライブラリ) がなくてもモックで全テスト通過する。
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from edge.servo.hardware import (
    ALL_SERVOS, ALL_LEGS, SERVO_BY_NAME,
    RF_HIP, LF_HIP, RR_HIP, LR_HIP,
    RF_KNEE, LF_KNEE, RR_KNEE, LR_KNEE,
    HEAD_TILT, HEAD_PAN,
    ADDR_LEG, ADDR_HEAD,
    PULSE_MIN_US, PULSE_MAX_US,
)
from edge.servo.poses import POSE_CATALOG, Pose, _legs_only
from edge.servo.controller import ServoController


# ── hardware.py ────────────────────────────────

class TestHardwareDefinitions:

    def test_total_servo_count(self):
        assert len(ALL_SERVOS) == 10

    def test_leg_servo_addresses(self):
        """脚部サーボは全て 0x40"""
        leg_servos = [RF_HIP, LF_HIP, RR_HIP, LR_HIP,
                      RF_KNEE, LF_KNEE, RR_KNEE, LR_KNEE]
        for s in leg_servos:
            assert s.addr == ADDR_LEG, f"{s.name} should use ADDR_LEG"

    def test_head_servo_addresses(self):
        """首部サーボは全て 0x41"""
        for s in [HEAD_TILT, HEAD_PAN]:
            assert s.addr == ADDR_HEAD, f"{s.name} should use ADDR_HEAD"

    def test_pin_assignments(self):
        """ピンアサインがテーブル通りか確認"""
        assert RF_HIP.channel  == 14
        assert LF_HIP.channel  == 15
        assert RR_HIP.channel  == 12
        assert LR_HIP.channel  == 13
        assert RF_KNEE.channel == 10
        assert LF_KNEE.channel == 11
        assert RR_KNEE.channel ==  8
        assert LR_KNEE.channel ==  9
        assert HEAD_TILT.channel == 15
        assert HEAD_PAN.channel  == 14

    def test_servo_names_unique(self):
        names = [s.name for s in ALL_SERVOS]
        assert len(names) == len(set(names)), "サーボ名に重複あり"

    def test_servo_by_name_lookup(self):
        assert SERVO_BY_NAME["RF_hip"]   == RF_HIP
        assert SERVO_BY_NAME["head_pan"] == HEAD_PAN

    def test_all_legs_defined(self):
        assert len(ALL_LEGS) == 4
        leg_names = {l.name for l in ALL_LEGS}
        assert leg_names == {"RF", "LF", "RR", "LR"}


# ── poses.py ────────────────────────────────

class TestPoses:

    def test_all_pose_names_registered(self):
        expected = {
            "neutral", "stand", "sit", "low_crouch",
            "head_nod", "head_up", "head_left", "head_right",
            "look_around", "happy_wag", "sad_droop", "alert",
            "shake_head", "thinking", "sleep",
            "trot_a", "trot_b",
            "turn_left_a", "turn_right_a",
        }
        assert expected <= set(POSE_CATALOG.keys())

    def test_pulse_values_in_range(self):
        for pose_name, pose in POSE_CATALOG.items():
            for servo_name, pulse in pose.pulses.items():
                if pulse is not None:
                    assert PULSE_MIN_US <= pulse <= PULSE_MAX_US, (
                        f"Pose '{pose_name}', servo '{servo_name}': "
                        f"pulse {pulse} out of range"
                    )

    def test_none_preserves_current(self):
        """None パルスは現在値維持の意味であることを確認"""
        pose = POSE_CATALOG["head_nod"]
        # head_nod は head_tilt のみ動かし、脚部は None
        assert pose.pulses[RF_HIP.name]  is None
        assert pose.pulses[HEAD_TILT.name] is not None

    def test_trot_phases_use_correct_legs(self):
        trot_a = POSE_CATALOG["trot_a"]
        trot_b = POSE_CATALOG["trot_b"]
        # trot_a で RF と LR が持ち上がっている（KNEE_UP = 1200 < 1450）
        assert trot_a.pulses[RF_KNEE.name] < 1450
        assert trot_a.pulses[LR_KNEE.name] < 1450
        # trot_b で LF と RR が持ち上がっている
        assert trot_b.pulses[LF_KNEE.name] < 1450
        assert trot_b.pulses[RR_KNEE.name] < 1450

    def test_invalid_pulse_raises(self):
        with pytest.raises(ValueError):
            Pose(name="bad", pulses={"RF_hip": 300})  # 500未満

    def test_stand_sets_all_legs(self):
        """stand ポーズは全8脚サーボ + 首を設定する"""
        stand = POSE_CATALOG["stand"]
        for s in ALL_SERVOS:
            assert stand.pulses.get(s.name) is not None, \
                f"stand pose missing {s.name}"


# ── controller.py (mock mode) ──────────────

class TestServoController:

    @pytest.fixture
    def ctrl(self):
        c = ServoController()
        asyncio.get_event_loop().run_until_complete(c.init())
        return c

    def test_init_sets_neutral(self, ctrl):
        pulses = ctrl.get_all_pulses()
        for s in ALL_SERVOS:
            assert pulses[s.name] == 1500, \
                f"{s.name} should be neutral after init"

    def test_execute_pose_stand(self, ctrl):
        asyncio.get_event_loop().run_until_complete(
            ctrl.execute_pose("stand")
        )
        pulses = ctrl.get_all_pulses()
        # 関節が KNEE_STAND (1450) に近いことを確認
        assert abs(pulses[RF_KNEE.name] - 1450) < 50

    def test_execute_pose_unknown(self, ctrl, caplog):
        asyncio.get_event_loop().run_until_complete(
            ctrl.execute_pose("nonexistent_pose")
        )
        assert "unknown pose" in caplog.text

    def test_set_servo_clamps(self, ctrl):
        """範囲外パルスはクランプされる"""
        asyncio.get_event_loop().run_until_complete(
            ctrl.set_servo(HEAD_PAN, 9999)
        )
        assert ctrl.get_pulse(HEAD_PAN) == PULSE_MAX_US

    def test_set_servo_single(self, ctrl):
        asyncio.get_event_loop().run_until_complete(
            ctrl.set_servo(HEAD_TILT, 1200, duration_ms=50)
        )
        assert ctrl.get_pulse(HEAD_TILT) == 1200

    def test_pca_routing_leg(self, ctrl):
        """脚部サーボは _pca_leg に書かれる"""
        asyncio.get_event_loop().run_until_complete(
            ctrl.set_servo(RF_HIP, 1300, duration_ms=50)
        )
        dc = ctrl._pca_leg.channels[RF_HIP.channel].duty_cycle
        assert dc > 0

    def test_pca_routing_head(self, ctrl):
        """首部サーボは _pca_head に書かれる"""
        asyncio.get_event_loop().run_until_complete(
            ctrl.set_servo(HEAD_PAN, 1200, duration_ms=50)
        )
        dc = ctrl._pca_head.channels[HEAD_PAN.channel].duty_cycle
        assert dc > 0

    def test_trot_completes(self, ctrl):
        """トロット2サイクルが例外なく完了する"""
        asyncio.get_event_loop().run_until_complete(
            ctrl.execute_trot(steps=2)
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
