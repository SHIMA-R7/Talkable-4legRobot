"""
shared/functions/definitions.py
================================
Gemini Function Calling 用のスキーマ定義。

Gemini 1.5 Flash の tools パラメータに渡す辞書リストとして定義する。
Stage 3 の GeminiClient がそのままインポートして使用する。

設計方針:
  - サーボは「ポーズ名」で抽象化。生パルス値は Gemini に見せない。
  - 感情状態は EmotionState Enum を文字列 enum として渡す。
  - カメラ・センサー取得もすべて Function として定義し、
    Gemini が自律的に呼び出せるようにする。
"""

from __future__ import annotations
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# ポーズカタログ (Stage 2 HW層と同期が必要)
# ---------------------------------------------------------------------------
POSE_NAMES: List[str] = [
    "neutral",        # 全サーボ中立
    "attention",      # 背筋を伸ばした注目姿勢
    "nod",            # 頷き (1回)
    "shake_head",     # 首振り (1回)
    "tilt_left",      # 首を左に傾ける
    "tilt_right",     # 首を右に傾ける
    "bow",            # 礼
    "shrug",          # 肩をすくめる
    "wave",           # 手を振る
    "happy_wiggle",   # 喜びの揺れ
    "sad_droop",      # 悲しみ (うなだれ)
    "excited_jump",   # 興奮 (上下動)
    "thinking_tilt",  # 考え中 (首を傾けて視線を上に)
    "sleep",          # 眠り (頭を下げる)
]

EMOTION_VALUES: List[str] = [
    "neutral", "happy", "sad", "surprised",
    "thinking", "excited", "sleepy", "angry",
]


# ---------------------------------------------------------------------------
# Function スキーマ定義
# ---------------------------------------------------------------------------
ROBOT_FUNCTIONS: List[Dict[str, Any]] = [

    # ------------------------------------------------------------------
    # 1. ポーズ制御
    # ------------------------------------------------------------------
    {
        "name": "execute_pose",
        "description": (
            "ロボットに指定したポーズを取らせる。"
            "感情表現・動作応答に使用する。"
            "複数のポーズを連続して呼び出すことでアニメーションを作れる。"
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
                    "description": "ポーズ保持時間 (ミリ秒)。省略時は 800ms。",
                    "minimum": 100,
                    "maximum": 10000,
                },
                "easing": {
                    "type": "string",
                    "enum": ["linear", "ease_in", "ease_out", "ease_in_out"],
                    "description": "サーボ補間方式。省略時は ease_out。",
                },
            },
            "required": ["pose_name"],
        },
    },

    # ------------------------------------------------------------------
    # 2. 感情状態の設定
    # ------------------------------------------------------------------
    {
        "name": "set_emotion",
        "description": (
            "ロボットの感情状態を設定する。"
            "感情は対応するポーズに自動マッピングされる。"
            "音声合成のトーンにも影響する。"
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

    # ------------------------------------------------------------------
    # 3. カメラスナップショット取得
    # ------------------------------------------------------------------
    {
        "name": "capture_image",
        "description": (
            "Pi Camera でスナップショットを撮影し、base64 JPEG として返す。"
            "視覚情報が必要なとき (人物確認・環境認識) に呼び出す。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "width": {
                    "type": "integer",
                    "description": "画像幅 (px)。デフォルト 640。",
                    "enum": [320, 640, 1280],
                },
                "height": {
                    "type": "integer",
                    "description": "画像高 (px)。デフォルト 480。",
                    "enum": [240, 480, 720],
                },
                "reason": {
                    "type": "string",
                    "description": "撮影理由 (ログ用)。",
                },
            },
            "required": [],
        },
    },

    # ------------------------------------------------------------------
    # 4. システム状態の取得
    # ------------------------------------------------------------------
    {
        "name": "get_system_status",
        "description": (
            "Raspberry Pi のシステム状態 (CPU温度・負荷・メモリ) を取得する。"
            "過熱や過負荷を自律的に検知して動作を制限するために使う。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "include_servo_state": {
                    "type": "boolean",
                    "description": "現在の全サーボパルス値を含めるか。デフォルト false。",
                },
            },
            "required": [],
        },
    },

    # ------------------------------------------------------------------
    # 5. テキスト読み上げ
    # ------------------------------------------------------------------
    {
        "name": "speak",
        "description": (
            "指定したテキストを AIY Voice HAT のスピーカーで読み上げる。"
            "Gemini の回答テキストとは別に、追加の発話が必要な場合に使用する。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "読み上げるテキスト (最大 500 文字)。",
                    "maxLength": 500,
                },
                "language": {
                    "type": "string",
                    "enum": ["ja-JP", "en-US"],
                    "description": "言語コード。デフォルト ja-JP。",
                },
                "speed": {
                    "type": "number",
                    "description": "読み上げ速度倍率 0.5〜2.0。デフォルト 1.0。",
                    "minimum": 0.5,
                    "maximum": 2.0,
                },
            },
            "required": ["text"],
        },
    },

    # ------------------------------------------------------------------
    # 6. 単一サーボの直接制御 (上級者向け)
    # ------------------------------------------------------------------
    {
        "name": "set_servo_pulse",
        "description": (
            "特定チャンネルのサーボをマイクロ秒単位で直接制御する。"
            "通常は execute_pose を使うこと。"
            "細かいチューニングや未定義ポーズのプロトタイプに使用する。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "integer",
                    "description": "PCA9685 チャンネル番号 0〜7。",
                    "minimum": 0,
                    "maximum": 7,
                },
                "pulse_us": {
                    "type": "integer",
                    "description": "パルス幅 (μs)。500〜2500。中立は 1500。",
                    "minimum": 500,
                    "maximum": 2500,
                },
                "duration_ms": {
                    "type": "integer",
                    "description": "移動時間 (ms)。",
                    "minimum": 100,
                    "maximum": 5000,
                },
            },
            "required": ["channel", "pulse_us"],
        },
    },

    # ------------------------------------------------------------------
    # 7. 自律思考ループの制御
    # ------------------------------------------------------------------
    {
        "name": "schedule_thought",
        "description": (
            "指定秒後に Gemini 自身への自律思考プロンプトをスケジュールする。"
            "会話が途切れたときに自発的な発言や動作をトリガーするために使う。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "delay_seconds": {
                    "type": "integer",
                    "description": "思考を発火するまでの待機秒数。",
                    "minimum": 5,
                    "maximum": 300,
                },
                "prompt_hint": {
                    "type": "string",
                    "description": "自律思考に使うヒント文 (省略可)。",
                },
            },
            "required": ["delay_seconds"],
        },
    },

    # ------------------------------------------------------------------
    # 8. LEDインジケータ (将来拡張用)
    # ------------------------------------------------------------------
    {
        "name": "set_led",
        "description": (
            "AIY Voice HAT のボタンLEDを制御する (将来拡張用)。"
            "現在はログ出力のみ。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["on", "off", "blink", "pulse"],
                    "description": "LED状態。",
                },
                "color": {
                    "type": "string",
                    "enum": ["white", "red", "green", "blue"],
                    "description": "LED色 (ハードウェアが対応している場合)。",
                },
            },
            "required": ["state"],
        },
    },
]


def get_tools_schema() -> List[Dict[str, Any]]:
    """Gemini API の tools パラメータ用スキーマを返す"""
    return [
        {
            "function_declarations": ROBOT_FUNCTIONS,
        }
    ]


def get_function_names() -> List[str]:
    return [f["name"] for f in ROBOT_FUNCTIONS]
