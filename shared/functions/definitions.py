"""
shared/functions/definitions.py
================================
Gemini Function Calling 用のスキーマ定義（4脚ロボット版）。
"""

from __future__ import annotations
from typing import Any, Dict, List

# poses.py の POSE_CATALOG と同期させること
POSE_NAMES: List[str] = [
    "neutral", "stand", "sit", "low_crouch",
    "head_nod", "head_up", "head_left", "head_right", "look_around",
    "happy_wag", "sad_droop", "alert", "shake_head", "thinking", "sleep",
    "trot_a", "trot_b", "turn_left_a", "turn_right_a",
]

EMOTION_VALUES: List[str] = [
    "neutral", "happy", "sad", "surprised",
    "thinking", "excited", "sleepy", "angry",
]


ROBOT_FUNCTIONS: List[Dict[str, Any]] = [
    {
        "name": "execute_pose",
        "description": (
            "ロボットに指定したポーズを取らせる。"
            "感情表現・動作応答に使用する。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pose_name": {
                    "type": "string",
                    "enum": POSE_NAMES,
                    "description": "実行するポーズ名",
                },
                "duration_ms": {
                    "type": "integer",
                    "description": "ポーズ保持時間 (ミリ秒)。省略時はポーズごとのデフォルト。",
                    "minimum": 100,
                    "maximum": 10000,
                },
            },
            "required": ["pose_name"],
        },
    },
    {
        "name": "set_emotion",
        "description": (
            "ロボットの感情状態を設定する。"
            "感情は対応するポーズに自動マッピングされる。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "emotion": {
                    "type": "string",
                    "enum": EMOTION_VALUES,
                    "description": "設定する感情",
                },
                "intensity": {
                    "type": "number",
                    "description": "感情の強度 0.0〜1.0。省略時は 0.7。",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
            },
            "required": ["emotion"],
        },
    },
    {
        "name": "walk_forward",
        "description": "前進歩行する。トロット歩容で指定ステップ数だけ歩く。",
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "integer",
                    "description": "歩数（サイクル数）。デフォルト2。",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": [],
        },
    },
    {
        "name": "speak",
        "description": "指定したテキストをスピーカーで読み上げる。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "読み上げるテキスト",
                    "maxLength": 500,
                },
                "language": {
                    "type": "string",
                    "enum": ["ja-JP", "en-US"],
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "get_system_status",
        "description": "Raspberry Pi のシステム状態 (CPU温度・負荷) を取得する。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


def get_tools_schema() -> List[Dict[str, Any]]:
    return [{"function_declarations": ROBOT_FUNCTIONS}]


def get_function_names() -> List[str]:
    return [f["name"] for f in ROBOT_FUNCTIONS]
