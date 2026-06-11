"""
shared/protocol/messages.py
===========================
エッジ ↔ サーバー間の全メッセージ型定義。

転送形式:
  - JSON制御メッセージ  : MessageEnvelope を JSON シリアライズ
  - 音声バイナリストリーム: WebSocket バイナリフレーム
                          先頭 4 byte = セッション ID (uint32 BE)
                          残り       = PCM16 LE, 16 kHz, mono

エンドポイント一覧 (server側):
  WS  /ws/audio          音声ストリーミング受信
  WS  /ws/control        双方向 JSON 制御チャネル
  POST /api/camera/frame カメラフレーム受信 (JPEG multipart)
  GET  /api/status       システム状態照会
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
AUDIO_SAMPLE_RATE = 16_000   # Hz
AUDIO_CHANNELS    = 1
AUDIO_SAMPLE_WIDTH = 2       # bytes (PCM16)
AUDIO_CHUNK_MS    = 100      # 1チャンクあたり ms
AUDIO_CHUNK_BYTES = AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * AUDIO_SAMPLE_WIDTH * AUDIO_CHUNK_MS // 1000

SERVO_COUNT       = 8
SERVO_MIN_PULSE   = 500      # μs  (PTK7465W)
SERVO_MAX_PULSE   = 2500     # μs
SERVO_NEUTRAL     = 1500     # μs

PROTOCOL_VERSION  = "1.0"


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------
class MessageType(str, Enum):
    # 音声
    AUDIO_STREAM_START = "audio_stream_start"
    AUDIO_STREAM_END   = "audio_stream_end"
    AUDIO_CHUNK        = "audio_chunk"      # バイナリ転送のため通常不使用

    # カメラ
    CAMERA_REQUEST     = "camera_request"
    CAMERA_FRAME       = "camera_frame"

    # サーボ / ポーズ
    SERVO_COMMAND      = "servo_command"
    POSE_COMMAND       = "pose_command"

    # Gemini
    GEMINI_REQUEST     = "gemini_request"
    GEMINI_RESPONSE    = "gemini_response"
    FUNCTION_CALL      = "function_call"
    FUNCTION_RESULT    = "function_result"

    # システム
    SYSTEM_STATUS      = "system_status"
    HEARTBEAT          = "heartbeat"
    ERROR              = "error"


class EmotionState(str, Enum):
    NEUTRAL    = "neutral"
    HAPPY      = "happy"
    SAD        = "sad"
    SURPRISED  = "surprised"
    THINKING   = "thinking"
    EXCITED    = "excited"
    SLEEPY     = "sleepy"
    ANGRY      = "angry"


class CameraMode(str, Enum):
    SNAPSHOT   = "snapshot"
    STREAM     = "stream"
    STOP       = "stop"


# ---------------------------------------------------------------------------
# ベースモデル
# ---------------------------------------------------------------------------
class RobotMessage(BaseModel):
    """全メッセージの共通フィールド"""
    message_id: str   = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp:  float = Field(default_factory=time.time)
    version:    str   = PROTOCOL_VERSION

    model_config = {"frozen": False, "extra": "forbid"}


# ---------------------------------------------------------------------------
# 音声メッセージ
# ---------------------------------------------------------------------------
class AudioStreamStart(RobotMessage):
    type:        Literal[MessageType.AUDIO_STREAM_START] = MessageType.AUDIO_STREAM_START
    session_id:  str
    sample_rate: int = AUDIO_SAMPLE_RATE
    channels:    int = AUDIO_CHANNELS
    encoding:    str = "pcm16le"
    source:      str = "aiy_voice_hat"


class AudioStreamEnd(RobotMessage):
    type:        Literal[MessageType.AUDIO_STREAM_END] = MessageType.AUDIO_STREAM_END
    session_id:  str
    duration_ms: int = 0
    total_bytes: int = 0


class AudioChunk(RobotMessage):
    """WebSocket バイナリ転送のメタデータ (実データは別フレーム)"""
    type:       Literal[MessageType.AUDIO_CHUNK] = MessageType.AUDIO_CHUNK
    session_id: str
    seq:        int   # シーケンス番号 (欠損検知用)
    size_bytes: int


# ---------------------------------------------------------------------------
# カメラメッセージ
# ---------------------------------------------------------------------------
class CameraRequest(RobotMessage):
    type:       Literal[MessageType.CAMERA_REQUEST] = MessageType.CAMERA_REQUEST
    mode:       CameraMode = CameraMode.SNAPSHOT
    width:      int = 640
    height:     int = 480
    quality:    int = Field(85, ge=1, le=100)
    session_id: Optional[str] = None


class CameraFrame(RobotMessage):
    type:       Literal[MessageType.CAMERA_FRAME] = MessageType.CAMERA_FRAME
    session_id: Optional[str] = None
    width:      int
    height:     int
    format:     str = "jpeg"
    size_bytes: int
    # base64エンコード済み画像 (API転送時のみ使用; WebSocket では別フレーム)
    data_b64:   Optional[str] = None


# ---------------------------------------------------------------------------
# サーボ / ポーズメッセージ
# ---------------------------------------------------------------------------
class ServoCommand(RobotMessage):
    """単一サーボ指定"""
    type:       Literal[MessageType.SERVO_COMMAND] = MessageType.SERVO_COMMAND
    channel:    int   = Field(..., ge=0, lt=SERVO_COUNT)
    pulse_us:   int   = Field(..., ge=SERVO_MIN_PULSE, le=SERVO_MAX_PULSE)
    duration_ms: int  = Field(500, ge=0, le=10_000)

    @field_validator("pulse_us")
    @classmethod
    def clamp_pulse(cls, v: int) -> int:
        return max(SERVO_MIN_PULSE, min(SERVO_MAX_PULSE, v))


class PoseCommand(RobotMessage):
    """8サーボ一括ポーズ指定"""
    type:        Literal[MessageType.POSE_COMMAND] = MessageType.POSE_COMMAND
    pose_name:   str
    # channel 0-7 のパルス幅 [μs]; None = 現在値を維持
    pulses:      List[Optional[int]] = Field(default_factory=lambda: [None] * SERVO_COUNT)
    duration_ms: int = Field(800, ge=0, le=10_000)
    easing:      str = "linear"   # linear | ease_in | ease_out | ease_in_out

    @field_validator("pulses")
    @classmethod
    def validate_pulses(cls, v: List[Optional[int]]) -> List[Optional[int]]:
        if len(v) != SERVO_COUNT:
            raise ValueError(f"pulses must have exactly {SERVO_COUNT} elements")
        for p in v:
            if p is not None and not (SERVO_MIN_PULSE <= p <= SERVO_MAX_PULSE):
                raise ValueError(f"pulse {p} out of range [{SERVO_MIN_PULSE}, {SERVO_MAX_PULSE}]")
        return v


# ---------------------------------------------------------------------------
# Gemini 関連メッセージ
# ---------------------------------------------------------------------------
class GeminiRequest(RobotMessage):
    type:       Literal[MessageType.GEMINI_REQUEST] = MessageType.GEMINI_REQUEST
    session_id: str
    # 音声データは別WebSocketバイナリフレームで転送済み
    # テキスト補助入力 (システムプロンプト上書き等)
    text_input: Optional[str] = None
    # カメラフレームを含める場合は CameraFrame を埋め込む
    camera_frame: Optional[CameraFrame] = None
    include_history: bool = True


class FunctionCall(BaseModel):
    name:      str
    call_id:   str
    arguments: Dict[str, Any]


class FunctionResult(BaseModel):
    call_id: str
    name:    str
    result:  Any
    error:   Optional[str] = None


class GeminiResponse(RobotMessage):
    type:           Literal[MessageType.GEMINI_RESPONSE] = MessageType.GEMINI_RESPONSE
    session_id:     str
    text:           Optional[str] = None
    emotion:        EmotionState  = EmotionState.NEUTRAL
    function_calls: List[FunctionCall]   = Field(default_factory=list)
    finish_reason:  str = "stop"
    tokens_used:    int = 0


# ---------------------------------------------------------------------------
# システムメッセージ
# ---------------------------------------------------------------------------
class ComponentStatus(BaseModel):
    name:    str
    ok:      bool
    detail:  str = ""


class SystemStatus(RobotMessage):
    type:       Literal[MessageType.SYSTEM_STATUS] = MessageType.SYSTEM_STATUS
    node:       str   # "edge" | "server"
    components: List[ComponentStatus] = Field(default_factory=list)
    cpu_pct:    float = 0.0
    mem_pct:    float = 0.0
    temp_c:     Optional[float] = None   # CPU温度 (Piのみ)


class Heartbeat(RobotMessage):
    type: Literal[MessageType.HEARTBEAT] = MessageType.HEARTBEAT
    node: str


class ErrorMessage(RobotMessage):
    type:      Literal[MessageType.ERROR] = MessageType.ERROR
    code:      str
    message:   str
    context:   Dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


# ---------------------------------------------------------------------------
# エンベロープ (WebSocket JSON フレーム共通ラッパー)
# ---------------------------------------------------------------------------
_Payload = Union[
    AudioStreamStart, AudioStreamEnd, AudioChunk,
    CameraRequest, CameraFrame,
    ServoCommand, PoseCommand,
    GeminiRequest, GeminiResponse,
    SystemStatus, Heartbeat, ErrorMessage,
]

class MessageEnvelope(BaseModel):
    """WebSocket JSON チャネルで実際に送受信するルートオブジェクト"""
    payload: _Payload = Field(..., discriminator="type")

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str | bytes) -> "MessageEnvelope":
        return cls.model_validate_json(raw)

    model_config = {"frozen": False}
