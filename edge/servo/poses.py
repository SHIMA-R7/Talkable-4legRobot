"""
edge/servo/poses.py
===================
ポーズカタログ。
Pose.pulses は servo_name -> pulse_us(int) | None(現在値維持) の辞書。
pulse_us は 500-2500 の中立1500基準値で、CALIB値への変換は controller が行う。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Optional

from edge.servo.hardware import (
    RF_HIP, LF_HIP, RR_HIP, LR_HIP,
    RF_KNEE, LF_KNEE, RR_KNEE, LR_KNEE,
    HEAD_TILT, HEAD_PAN,
    ALL_SERVOS, PULSE_NEUT_US, PULSE_MIN_US, PULSE_MAX_US,
)

# 根本（水平）
HIP_CENTER   = 1500
HIP_FORWARD  = 1300
HIP_BACKWARD = 1700

# 関節（上下）
KNEE_DOWN  = 1600
KNEE_UP    = 1200
KNEE_STAND = 1450

# 首
HEAD_TILT_NEU  = 1500
HEAD_TILT_UP   = 1200
HEAD_TILT_DOWN = 1750
HEAD_PAN_NEU   = 1500
HEAD_PAN_LEFT  = 1200
HEAD_PAN_RIGHT = 1800


@dataclass
class Pose:
    name:        str
    pulses:      Dict[str, Optional[int]]
    duration_ms: int = 600
    easing:      str = "ease_out"

    def __post_init__(self):
        for name, val in self.pulses.items():
            if val is not None and not (PULSE_MIN_US <= val <= PULSE_MAX_US):
                raise ValueError(f"Pose '{self.name}': servo '{name}' pulse {val} out of range")


def _all_neutral() -> Dict[str, Optional[int]]:
    return {s.name: PULSE_NEUT_US for s in ALL_SERVOS}


def _legs_only(
    rf_hip=None, lf_hip=None, rr_hip=None, lr_hip=None,
    rf_knee=None, lf_knee=None, rr_knee=None, lr_knee=None,
    head_tilt=None, head_pan=None,
) -> Dict[str, Optional[int]]:
    return {
        RF_HIP.name: rf_hip, LF_HIP.name: lf_hip,
        RR_HIP.name: rr_hip, LR_HIP.name: lr_hip,
        RF_KNEE.name: rf_knee, LF_KNEE.name: lf_knee,
        RR_KNEE.name: rr_knee, LR_KNEE.name: lr_knee,
        HEAD_TILT.name: head_tilt, HEAD_PAN.name: head_pan,
    }


POSE_CATALOG: Dict[str, Pose] = {}

def _reg(pose: Pose) -> Pose:
    POSE_CATALOG[pose.name] = pose
    return pose


_reg(Pose("neutral", _all_neutral(), duration_ms=800))

_reg(Pose("stand", _legs_only(
    rf_hip=HIP_CENTER, lf_hip=HIP_CENTER, rr_hip=HIP_CENTER, lr_hip=HIP_CENTER,
    rf_knee=KNEE_STAND, lf_knee=KNEE_STAND, rr_knee=KNEE_STAND, lr_knee=KNEE_STAND,
    head_tilt=HEAD_TILT_NEU, head_pan=HEAD_PAN_NEU,
), duration_ms=1000))

_reg(Pose("sit", _legs_only(
    rf_hip=HIP_CENTER, lf_hip=HIP_CENTER, rr_hip=HIP_BACKWARD, lr_hip=HIP_BACKWARD,
    rf_knee=KNEE_DOWN, lf_knee=KNEE_DOWN, rr_knee=KNEE_DOWN, lr_knee=KNEE_DOWN,
    head_tilt=HEAD_TILT_NEU, head_pan=HEAD_PAN_NEU,
), duration_ms=900))

_reg(Pose("low_crouch", _legs_only(
    rf_hip=HIP_CENTER, lf_hip=HIP_CENTER, rr_hip=HIP_CENTER, lr_hip=HIP_CENTER,
    rf_knee=KNEE_DOWN+100, lf_knee=KNEE_DOWN+100, rr_knee=KNEE_DOWN+100, lr_knee=KNEE_DOWN+100,
    head_tilt=HEAD_TILT_DOWN, head_pan=HEAD_PAN_NEU,
), duration_ms=700))

_reg(Pose("head_nod", _legs_only(head_tilt=HEAD_TILT_DOWN), duration_ms=300, easing="ease_in_out"))
_reg(Pose("head_up", _legs_only(head_tilt=HEAD_TILT_UP), duration_ms=300))
_reg(Pose("head_left", _legs_only(head_pan=HEAD_PAN_LEFT), duration_ms=400))
_reg(Pose("head_right", _legs_only(head_pan=HEAD_PAN_RIGHT), duration_ms=400))
_reg(Pose("look_around", _legs_only(head_pan=HEAD_PAN_LEFT, head_tilt=HEAD_TILT_NEU), duration_ms=600))

_reg(Pose("happy_wag", _legs_only(
    rr_hip=HIP_FORWARD-100, lr_hip=HIP_BACKWARD+100,
    rr_knee=KNEE_STAND, lr_knee=KNEE_STAND,
    head_tilt=HEAD_TILT_UP, head_pan=HEAD_PAN_NEU,
), duration_ms=350))

_reg(Pose("sad_droop", _legs_only(
    rf_knee=KNEE_DOWN+150, lf_knee=KNEE_DOWN+150, rr_knee=KNEE_DOWN+150, lr_knee=KNEE_DOWN+150,
    head_tilt=HEAD_TILT_DOWN, head_pan=HEAD_PAN_NEU,
), duration_ms=1200, easing="ease_in"))

_reg(Pose("alert", _legs_only(
    rf_knee=KNEE_STAND-100, lf_knee=KNEE_STAND-100, rr_knee=KNEE_STAND-100, lr_knee=KNEE_STAND-100,
    head_tilt=HEAD_TILT_UP, head_pan=HEAD_PAN_NEU,
), duration_ms=400))

_reg(Pose("shake_head", _legs_only(head_pan=HEAD_PAN_RIGHT, head_tilt=HEAD_TILT_NEU), duration_ms=250))

_reg(Pose("thinking", _legs_only(
    head_pan=HEAD_PAN_LEFT+100, head_tilt=HEAD_TILT_UP+100,
), duration_ms=700, easing="ease_in_out"))

_reg(Pose("sleep", _legs_only(
    rf_knee=KNEE_DOWN+200, lf_knee=KNEE_DOWN+200, rr_knee=KNEE_DOWN+200, lr_knee=KNEE_DOWN+200,
    head_tilt=HEAD_TILT_DOWN+100, head_pan=HEAD_PAN_NEU,
), duration_ms=2000, easing="ease_in"))

# トロット歩行
_reg(Pose("trot_a", _legs_only(
    rf_hip=HIP_FORWARD, rf_knee=KNEE_UP,
    lr_hip=HIP_FORWARD, lr_knee=KNEE_UP,
    lf_hip=HIP_BACKWARD, lf_knee=KNEE_DOWN,
    rr_hip=HIP_BACKWARD, rr_knee=KNEE_DOWN,
    head_tilt=HEAD_TILT_NEU, head_pan=HEAD_PAN_NEU,
), duration_ms=200, easing="linear"))

_reg(Pose("trot_b", _legs_only(
    lf_hip=HIP_FORWARD, lf_knee=KNEE_UP,
    rr_hip=HIP_FORWARD, rr_knee=KNEE_UP,
    rf_hip=HIP_BACKWARD, rf_knee=KNEE_DOWN,
    lr_hip=HIP_BACKWARD, lr_knee=KNEE_DOWN,
    head_tilt=HEAD_TILT_NEU, head_pan=HEAD_PAN_NEU,
), duration_ms=200, easing="linear"))

_reg(Pose("turn_left_a", _legs_only(
    rf_hip=HIP_FORWARD, rf_knee=KNEE_UP,
    rr_hip=HIP_FORWARD, rr_knee=KNEE_UP,
    lf_hip=HIP_BACKWARD, lf_knee=KNEE_DOWN,
    lr_hip=HIP_BACKWARD, lr_knee=KNEE_DOWN,
), duration_ms=250))

_reg(Pose("turn_right_a", _legs_only(
    lf_hip=HIP_FORWARD, lf_knee=KNEE_UP,
    lr_hip=HIP_FORWARD, lr_knee=KNEE_UP,
    rf_hip=HIP_BACKWARD, rf_knee=KNEE_DOWN,
    rr_hip=HIP_BACKWARD, rr_knee=KNEE_DOWN,
), duration_ms=250))


POSE_NAMES = list(POSE_CATALOG.keys())
