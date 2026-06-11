"""
edge/servo/poses.py
===================
ポーズカタログ。

Pose は全10サーボのパルス幅 [μs] を保持する辞書。
キーは ServoId.name 文字列。None = 現在値を維持。

設計指針:
  - 左右対称なポーズは _mirror() で自動生成
  - 関節角度は「上げる = 値が小さい」方向に統一
    (機体を実際に動かして確認し、KNEE_UP/DOWN 定数で調整)
  - 歩行ポーズは Phase A/B の2フレーム × 歩容種別で定義
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Optional

from edge.servo.hardware import (
    ALL_SERVOS, ALL_LEGS, DIAGONAL_PAIR_A, DIAGONAL_PAIR_B,
    RF_HIP, LF_HIP, RR_HIP, LR_HIP,
    RF_KNEE, LF_KNEE, RR_KNEE, LR_KNEE,
    HEAD_TILT, HEAD_PAN,
    PULSE_NEUT_US, PULSE_MIN_US, PULSE_MAX_US,
)

# ──────────────────────────────────────
# 関節方向キャリブレーション
# 実機で確認して調整する定数
# ──────────────────────────────────────
# 根本（水平）
HIP_CENTER    = 1500   # 真下（中立）
HIP_FORWARD   = 1300   # 前方へ振り出す
HIP_BACKWARD  = 1700   # 後方へ引く

# 関節（上下）
KNEE_DOWN     = 1600   # 接地・加重
KNEE_UP       = 1200   # 持ち上げ・スイング
KNEE_STAND    = 1450   # 起立時の自然な曲がり

# 首
HEAD_TILT_NEU  = 1500
HEAD_TILT_UP   = 1200
HEAD_TILT_DOWN = 1750
HEAD_PAN_NEU   = 1500
HEAD_PAN_LEFT  = 1200
HEAD_PAN_RIGHT = 1800


# ──────────────────────────────────────
# Pose クラス
# ──────────────────────────────────────
@dataclass
class Pose:
    name:        str
    pulses:      Dict[str, Optional[int]]   # servo_name → pulse_us | None
    duration_ms: int = 600
    easing:      str = "ease_out"

    def __post_init__(self):
        # 値の範囲チェック
        for name, val in self.pulses.items():
            if val is not None and not (PULSE_MIN_US <= val <= PULSE_MAX_US):
                raise ValueError(
                    f"Pose '{self.name}': servo '{name}' pulse {val} out of range"
                )

    def get(self, servo_name: str) -> Optional[int]:
        return self.pulses.get(servo_name)


def _all_neutral() -> Dict[str, Optional[int]]:
    """全サーボを中立値に設定する辞書を返す"""
    return {s.name: PULSE_NEUT_US for s in ALL_SERVOS}


def _legs_only(
    rf_hip=None, lf_hip=None, rr_hip=None, lr_hip=None,
    rf_knee=None, lf_knee=None, rr_knee=None, lr_knee=None,
    head_tilt=None, head_pan=None,
) -> Dict[str, Optional[int]]:
    """指定サーボのみ設定、残りは None (現在値維持) にする辞書"""
    return {
        RF_HIP.name:   rf_hip,
        LF_HIP.name:   lf_hip,
        RR_HIP.name:   rr_hip,
        LR_HIP.name:   lr_hip,
        RF_KNEE.name:  rf_knee,
        LF_KNEE.name:  lf_knee,
        RR_KNEE.name:  rr_knee,
        LR_KNEE.name:  lr_knee,
        HEAD_TILT.name: head_tilt,
        HEAD_PAN.name:  head_pan,
    }


# ──────────────────────────────────────
# ポーズカタログ
# ──────────────────────────────────────
POSE_CATALOG: Dict[str, Pose] = {}

def _reg(pose: Pose) -> Pose:
    POSE_CATALOG[pose.name] = pose
    return pose


# --- 基本姿勢 ---
_reg(Pose(
    name="neutral",
    pulses=_all_neutral(),
    duration_ms=800,
))

_reg(Pose(
    name="stand",
    pulses=_legs_only(
        rf_hip=HIP_CENTER,  lf_hip=HIP_CENTER,
        rr_hip=HIP_CENTER,  lr_hip=HIP_CENTER,
        rf_knee=KNEE_STAND, lf_knee=KNEE_STAND,
        rr_knee=KNEE_STAND, lr_knee=KNEE_STAND,
        head_tilt=HEAD_TILT_NEU, head_pan=HEAD_PAN_NEU,
    ),
    duration_ms=1000,
))

_reg(Pose(
    name="sit",
    pulses=_legs_only(
        rf_hip=HIP_CENTER, lf_hip=HIP_CENTER,
        rr_hip=HIP_BACKWARD, lr_hip=HIP_BACKWARD,
        rf_knee=KNEE_DOWN,  lf_knee=KNEE_DOWN,
        rr_knee=KNEE_DOWN,  lr_knee=KNEE_DOWN,
        head_tilt=HEAD_TILT_NEU, head_pan=HEAD_PAN_NEU,
    ),
    duration_ms=900,
))

_reg(Pose(
    name="low_crouch",
    pulses=_legs_only(
        rf_hip=HIP_CENTER, lf_hip=HIP_CENTER,
        rr_hip=HIP_CENTER, lr_hip=HIP_CENTER,
        rf_knee=KNEE_DOWN+100, lf_knee=KNEE_DOWN+100,
        rr_knee=KNEE_DOWN+100, lr_knee=KNEE_DOWN+100,
        head_tilt=HEAD_TILT_DOWN, head_pan=HEAD_PAN_NEU,
    ),
    duration_ms=700,
))

# --- 頭部動作 ---
_reg(Pose(
    name="head_nod",
    pulses=_legs_only(head_tilt=HEAD_TILT_DOWN),
    duration_ms=300, easing="ease_in_out",
))

_reg(Pose(
    name="head_up",
    pulses=_legs_only(head_tilt=HEAD_TILT_UP),
    duration_ms=300,
))

_reg(Pose(
    name="head_left",
    pulses=_legs_only(head_pan=HEAD_PAN_LEFT),
    duration_ms=400,
))

_reg(Pose(
    name="head_right",
    pulses=_legs_only(head_pan=HEAD_PAN_RIGHT),
    duration_ms=400,
))

_reg(Pose(
    name="look_around",   # 首を左右に振る（Stage 4 で複合アニメとして使用）
    pulses=_legs_only(head_pan=HEAD_PAN_LEFT, head_tilt=HEAD_TILT_NEU),
    duration_ms=600,
))

# --- 感情ポーズ ---
_reg(Pose(
    name="happy_wag",
    pulses=_legs_only(
        rr_hip=HIP_FORWARD-100, lr_hip=HIP_BACKWARD+100,
        rr_knee=KNEE_STAND,     lr_knee=KNEE_STAND,
        head_tilt=HEAD_TILT_UP, head_pan=HEAD_PAN_NEU,
    ),
    duration_ms=350,
))

_reg(Pose(
    name="sad_droop",
    pulses=_legs_only(
        rf_knee=KNEE_DOWN+150, lf_knee=KNEE_DOWN+150,
        rr_knee=KNEE_DOWN+150, lr_knee=KNEE_DOWN+150,
        head_tilt=HEAD_TILT_DOWN, head_pan=HEAD_PAN_NEU,
    ),
    duration_ms=1200, easing="ease_in",
))

_reg(Pose(
    name="alert",
    pulses=_legs_only(
        rf_knee=KNEE_STAND-100, lf_knee=KNEE_STAND-100,
        rr_knee=KNEE_STAND-100, lr_knee=KNEE_STAND-100,
        head_tilt=HEAD_TILT_UP, head_pan=HEAD_PAN_NEU,
    ),
    duration_ms=400,
))

_reg(Pose(
    name="shake_head",
    pulses=_legs_only(head_pan=HEAD_PAN_RIGHT, head_tilt=HEAD_TILT_NEU),
    duration_ms=250,
))

_reg(Pose(
    name="thinking",
    pulses=_legs_only(
        head_pan=HEAD_PAN_LEFT+100, head_tilt=HEAD_TILT_UP+100,
    ),
    duration_ms=700, easing="ease_in_out",
))

_reg(Pose(
    name="sleep",
    pulses=_legs_only(
        rf_knee=KNEE_DOWN+200, lf_knee=KNEE_DOWN+200,
        rr_knee=KNEE_DOWN+200, lr_knee=KNEE_DOWN+200,
        head_tilt=HEAD_TILT_DOWN+100, head_pan=HEAD_PAN_NEU,
    ),
    duration_ms=2000, easing="ease_in",
))

# --- 歩行フレーム（トロット歩容: 対角2脚ずつ交互）---
# Phase A: 右前+左後 を振り出す、左前+右後 を踏む
_reg(Pose(
    name="trot_a",
    pulses=_legs_only(
        # 振り出し脚（対角A: RF, LR）
        rf_hip=HIP_FORWARD,  rf_knee=KNEE_UP,
        lr_hip=HIP_FORWARD,  lr_knee=KNEE_UP,
        # 踏込脚（対角B: LF, RR）
        lf_hip=HIP_BACKWARD, lf_knee=KNEE_DOWN,
        rr_hip=HIP_BACKWARD, rr_knee=KNEE_DOWN,
        head_tilt=HEAD_TILT_NEU, head_pan=HEAD_PAN_NEU,
    ),
    duration_ms=200, easing="linear",
))

# Phase B: 逆（左前+右後 を振り出す、右前+左後 を踏む）
_reg(Pose(
    name="trot_b",
    pulses=_legs_only(
        lf_hip=HIP_FORWARD,  lf_knee=KNEE_UP,
        rr_hip=HIP_FORWARD,  rr_knee=KNEE_UP,
        rf_hip=HIP_BACKWARD, rf_knee=KNEE_DOWN,
        lr_hip=HIP_BACKWARD, lr_knee=KNEE_DOWN,
        head_tilt=HEAD_TILT_NEU, head_pan=HEAD_PAN_NEU,
    ),
    duration_ms=200, easing="linear",
))

# --- ターン用フレーム（左旋回: 右脚を前・左脚を後） ---
_reg(Pose(
    name="turn_left_a",
    pulses=_legs_only(
        rf_hip=HIP_FORWARD,  rf_knee=KNEE_UP,
        rr_hip=HIP_FORWARD,  rr_knee=KNEE_UP,
        lf_hip=HIP_BACKWARD, lf_knee=KNEE_DOWN,
        lr_hip=HIP_BACKWARD, lr_knee=KNEE_DOWN,
    ),
    duration_ms=250,
))

_reg(Pose(
    name="turn_right_a",
    pulses=_legs_only(
        lf_hip=HIP_FORWARD,  lf_knee=KNEE_UP,
        lr_hip=HIP_FORWARD,  lr_knee=KNEE_UP,
        rf_hip=HIP_BACKWARD, rf_knee=KNEE_DOWN,
        rr_hip=HIP_BACKWARD, rr_knee=KNEE_DOWN,
    ),
    duration_ms=250,
))


# ──────────────────────────────────────
# Gemini Function Calling 用 POSE_NAMES
# shared/functions/definitions.py と同期させること
# ──────────────────────────────────────
POSE_NAMES = list(POSE_CATALOG.keys())
