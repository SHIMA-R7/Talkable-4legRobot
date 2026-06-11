"""
tests/test_stage1_protocol.py
=============================
Stage 1 検証テスト。

テスト対象:
  1. MessageEnvelope のシリアライズ / デシリアライズ
  2. PoseCommand のバリデーション (パルス値範囲・要素数)
  3. 設定ファイルのロード
  4. Function スキーマの構造確認
  5. WebSocket 接続 (ローカルループバック)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# プロジェクトルートを sys.path に追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.protocol.messages import (
    MessageEnvelope, PoseCommand, ServoCommand,
    AudioStreamStart, GeminiResponse, EmotionState,
    SERVO_MIN_PULSE, SERVO_MAX_PULSE, SERVO_COUNT,
)
from shared.functions.definitions import get_tools_schema, POSE_NAMES, ROBOT_FUNCTIONS


# ---------------------------------------------------------------------------
# 1. プロトコルシリアライズ
# ---------------------------------------------------------------------------
class TestSerialization:

    def test_pose_command_roundtrip(self):
        pose = PoseCommand(
            pose_name="happy_wiggle",
            pulses=[1500, 1200, 1800, None, None, 1500, 1500, 1400],
            duration_ms=600,
        )
        env = MessageEnvelope(payload=pose)
        raw = env.to_json()
        restored = MessageEnvelope.from_json(raw)

        assert restored.payload.pose_name == "happy_wiggle"
        assert restored.payload.pulses[1] == 1200
        assert restored.payload.pulses[3] is None

    def test_servo_command_clamp(self):
        # 範囲外はバリデーションエラー
        with pytest.raises(Exception):
            ServoCommand(channel=0, pulse_us=300)   # 500 未満
        with pytest.raises(Exception):
            ServoCommand(channel=0, pulse_us=3000)  # 2500 超過

    def test_pose_pulses_wrong_length(self):
        with pytest.raises(Exception):
            PoseCommand(pose_name="neutral", pulses=[1500, 1500])  # 2要素 (8必要)

    def test_audio_stream_start_roundtrip(self):
        msg = AudioStreamStart(session_id="abc123")
        env = MessageEnvelope(payload=msg)
        raw = env.to_json()
        restored = MessageEnvelope.from_json(raw)
        assert restored.payload.session_id == "abc123"
        assert restored.payload.sample_rate == 16000

    def test_gemini_response_emotion(self):
        resp = GeminiResponse(
            session_id="s1",
            text="こんにちは！",
            emotion=EmotionState.HAPPY,
        )
        env = MessageEnvelope(payload=resp)
        data = json.loads(env.to_json())
        assert data["payload"]["emotion"] == "happy"


# ---------------------------------------------------------------------------
# 2. Function スキーマ
# ---------------------------------------------------------------------------
class TestFunctionSchema:

    def test_all_functions_have_required_fields(self):
        for fn in ROBOT_FUNCTIONS:
            assert "name" in fn, f"missing 'name' in {fn}"
            assert "description" in fn
            assert "parameters" in fn
            assert fn["parameters"]["type"] == "object"

    def test_execute_pose_has_all_pose_names(self):
        schema = get_tools_schema()
        fn_decls = schema[0]["function_declarations"]
        execute_pose = next(f for f in fn_decls if f["name"] == "execute_pose")
        enum_vals = execute_pose["parameters"]["properties"]["pose_name"]["enum"]
        for pose in POSE_NAMES:
            assert pose in enum_vals, f"{pose} missing from execute_pose enum"

    def test_set_servo_pulse_range(self):
        schema = get_tools_schema()
        fn_decls = schema[0]["function_declarations"]
        set_servo = next(f for f in fn_decls if f["name"] == "set_servo_pulse")
        pulse_schema = set_servo["parameters"]["properties"]["pulse_us"]
        assert pulse_schema["minimum"] == SERVO_MIN_PULSE
        assert pulse_schema["maximum"] == SERVO_MAX_PULSE

    def test_function_count(self):
        assert len(ROBOT_FUNCTIONS) == 8


# ---------------------------------------------------------------------------
# 3. 設定ロード
# ---------------------------------------------------------------------------
class TestConfig:

    def test_default_config_loads(self):
        from shared.protocol.config import load_config
        load_config.cache_clear()
        c = load_config()
        assert c.servo.pwm_frequency == 50
        assert c.audio.sample_rate == 16000
        assert c.servo.channels.head_pan == 0

    def test_servo_channel_map(self):
        from shared.protocol.config import cfg
        ch = cfg.servo.channels
        # 全チャンネルが 0-7 の範囲
        for name, val in ch.model_dump().items():
            assert 0 <= val <= 7, f"{name}={val} out of range"
        # チャンネル番号に重複がない
        vals = list(ch.model_dump().values())
        assert len(vals) == len(set(vals)), "duplicate servo channel assignments"


# ---------------------------------------------------------------------------
# 4. メッセージエンベロープ型識別
# ---------------------------------------------------------------------------
class TestDiscriminator:

    def test_discriminated_union(self):
        """payload の type フィールドで正しいクラスが選ばれるか"""
        raw = json.dumps({
            "payload": {
                "type": "pose_command",
                "pose_name": "nod",
                "pulses": [None] * 8,
                "duration_ms": 800,
                "easing": "linear",
                "message_id": "x",
                "timestamp": 0.0,
                "version": "1.0",
            }
        })
        env = MessageEnvelope.from_json(raw)
        assert isinstance(env.payload, PoseCommand)
        assert env.payload.pose_name == "nod"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
