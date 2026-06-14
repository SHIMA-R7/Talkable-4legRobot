"""
edge/servo/hardware.py
======================
サーボハードウェア定数・ピンアサイン定義。

PCA9685 #0 (I2C 0x40) — 脚部
  CH 14: RF (右前) 根本 水平回転
  CH 15: LF (左前) 根本 水平回転
  CH 12: RR (右後) 根本 水平回転
  CH 13: LR (左後) 根本 水平回転
  CH 10: RF (右前) 関節 上下
  CH 11: LF (左前) 関節 上下
  CH  8: RR (右後) 関節 上下
  CH  9: LR (左後) 関節 上下

PCA9685 #1 (I2C 0x41) — 首部
  CH 15: 首 上下 (tilt)
  CH 14: 首 左右 (pan)
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Tuple

# ──────────────────────────────────────
# パルス幅定数 (PTK7465W)
# ──────────────────────────────────────
PULSE_MIN_US  = 500
PULSE_MAX_US  = 2500
PULSE_NEUT_US = 1500
PWM_FREQ_HZ   = 50

# ──────────────────────────────────────
# I2Cアドレス
# ──────────────────────────────────────
ADDR_LEG  = 0x40   # 脚部 PCA9685
ADDR_HEAD = 0x41   # 首部 PCA9685

# ──────────────────────────────────────
# サーボID定義
# 命名規則: <部位>_<脚/軸>_<役割>
# ──────────────────────────────────────
@dataclass(frozen=True)
class ServoId:
    name:    str
    addr:    int   # PCA9685 I2Cアドレス
    channel: int   # PCA9685 チャンネル番号

# --- 脚部 根本 (水平回転) ---
RF_HIP = ServoId("RF_hip", ADDR_LEG, 14)   # 右前 根本
LF_HIP = ServoId("LF_hip", ADDR_LEG, 15)   # 左前 根本
RR_HIP = ServoId("RR_hip", ADDR_LEG, 12)   # 右後 根本
LR_HIP = ServoId("LR_hip", ADDR_LEG, 13)   # 左後 根本

# --- 脚部 関節 (上下) ---
RF_KNEE = ServoId("RF_knee", ADDR_LEG, 10)  # 右前 関節
LF_KNEE = ServoId("LF_knee", ADDR_LEG, 11)  # 左前 関節
RR_KNEE = ServoId("RR_knee", ADDR_LEG,  8)  # 右後 関節
LR_KNEE = ServoId("LR_knee", ADDR_LEG,  9)  # 左後 関節

# --- 首部 ---
HEAD_TILT = ServoId("head_tilt", ADDR_HEAD, 15)  # 首 上下
HEAD_PAN  = ServoId("head_pan",  ADDR_HEAD, 14)  # 首 左右

# 全サーボのリスト (初期化・シャットダウン等で使用)
ALL_SERVOS: Tuple[ServoId, ...] = (
    RF_HIP, LF_HIP, RR_HIP, LR_HIP,
    RF_KNEE, LF_KNEE, RR_KNEE, LR_KNEE,
    HEAD_TILT, HEAD_PAN,
)

# 名前 → ServoId の引き当て辞書
SERVO_BY_NAME: Dict[str, ServoId] = {s.name: s for s in ALL_SERVOS}

# ──────────────────────────────────────
# 脚グループ定義 (ポーズ計算で便利)
# ──────────────────────────────────────
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

# 対角ペア (トロット歩容で同時に動く脚)
DIAGONAL_PAIR_A = (RF_LEG, LR_LEG)   # 右前 + 左後
DIAGONAL_PAIR_B = (LF_LEG, RR_LEG)   # 左前 + 右後
