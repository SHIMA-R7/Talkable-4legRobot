"""
edge/servo/hardware.py
======================
サーボハードウェア定数・ピンアサイン・キャリブレーション定義。

PCA9685 #0 (I2C 0x40) — 脚部
  CH 14: RF (右前) 根本 水平回転
  CH 15: LF (左前) 根本 水平回転
  CH 12: RR (右後) 根本 水平回転
  CH 13: LR (左後) 根本 水平回転
  CH 10: RF (右前) 関節 上下
  CH 11: LF (左前) 関節 上下
  CH  8: RR (右後) 関節 上下
  CH  9: LR (左後) 関節 上下

PCA9685 #1 (I2C 0x41) — 首部 (現状未接続 / 基板放棄)
  CH 15: 首 上下 (tilt)
  CH 14: 首 左右 (pan)
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Tuple

PULSE_MIN_US  = 500
PULSE_MAX_US  = 2500
PULSE_NEUT_US = 1500
PWM_FREQ_HZ   = 50

ADDR_LEG  = 0x40
ADDR_HEAD = 0x41


@dataclass(frozen=True)
class ServoId:
    name:    str
    addr:    int
    channel: int


# --- 脚部 根本 (水平回転) ---
RF_HIP = ServoId("RF_hip", ADDR_LEG, 14)
LF_HIP = ServoId("LF_hip", ADDR_LEG, 15)
RR_HIP = ServoId("RR_hip", ADDR_LEG, 12)
LR_HIP = ServoId("LR_hip", ADDR_LEG, 13)

# --- 脚部 関節 (上下) ---
RF_KNEE = ServoId("RF_knee", ADDR_LEG, 10)
LF_KNEE = ServoId("LF_knee", ADDR_LEG, 11)
RR_KNEE = ServoId("RR_knee", ADDR_LEG,  8)
LR_KNEE = ServoId("LR_knee", ADDR_LEG,  9)

# --- 首部 ---
HEAD_TILT = ServoId("head_tilt", ADDR_HEAD, 15)
HEAD_PAN  = ServoId("head_pan",  ADDR_HEAD, 14)

ALL_SERVOS: Tuple[ServoId, ...] = (
    RF_HIP, LF_HIP, RR_HIP, LR_HIP,
    RF_KNEE, LF_KNEE, RR_KNEE, LR_KNEE,
    HEAD_TILT, HEAD_PAN,
)

SERVO_BY_NAME: Dict[str, ServoId] = {s.name: s for s in ALL_SERVOS}


@dataclass(frozen=True)
class Leg:
    name: str
    hip:  ServoId
    knee: ServoId

RF_LEG = Leg("RF", RF_HIP, RF_KNEE)
LF_LEG = Leg("LF", LF_HIP, LF_KNEE)
RR_LEG = Leg("RR", RR_HIP, RR_KNEE)
LR_LEG = Leg("LR", LR_HIP, LR_KNEE)

ALL_LEGS: Tuple[Leg, ...] = (RF_LEG, LF_LEG, RR_LEG, LR_LEG)

DIAGONAL_PAIR_A = (RF_LEG, LR_LEG)
DIAGONAL_PAIR_B = (LF_LEG, RR_LEG)


# ──────────────────────────────────────────────────────────────
# キャリブレーション値（PCA9685 生カウント値、50Hz / 4096段階）
# 実機Arduinoスケッチから移植。
# CALIB_CENTER: 中立位置
# CALIB_P1    : pulse_us < 1500 方向の最大値（前方/持ち上げ）
# CALIB_P2    : pulse_us > 1500 方向の最大値（後方/接地）
# 0x41 (首部) は基板未接続のため未定義 → コントローラ側で線形変換にフォールバック
# ──────────────────────────────────────────────────────────────
CALIB_CENTER: Dict[str, int] = {
    "RF_hip": 344, "LF_hip": 330, "RR_hip": 362, "LR_hip": 356,
    "RF_knee": 362, "LF_knee": 358, "RR_knee": 340, "LR_knee": 360,
}
CALIB_P1: Dict[str, int] = {
    "RF_hip": 456, "LF_hip": 436, "RR_hip": 456, "LR_hip": 452,
    "RF_knee": 452, "LF_knee": 444, "RR_knee": 418, "LR_knee": 464,
}
CALIB_P2: Dict[str, int] = {
    "RF_hip": 268, "LF_hip": 246, "RR_hip": 268, "LR_hip": 252,
    "RF_knee": 268, "LF_knee": 290, "RR_knee": 300, "LR_knee": 286,
}
