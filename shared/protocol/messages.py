"""
shared/protocol/messages.py
===========================
エッジ ↔ サーバー間の全メッセージ型定義。
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


AUDIO_SAMPLE_RATE = 16_000
AUDIO_CHANNELS    = 1
AUDIO_SAMPLE_WIDTH = 2
AUDIO_CHUNK_MS    = 100
AUDIO_CHUNK_BYTES = AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * AUDIO_SAMPLE_WIDTH * AUDIO_CHUNK_MS // 1000

SERVO_COUNT       = 10
SERVO_MIN_PULSE   = 500
SERVO_MAX_PULSE   = 2500
SERVO_NEUTRAL     = 1500

PROTOCOL_VERSION  = "1.0"


class MessageType(str, Enum):
    AUDIO_STREAM_START = "audio_stream_start"
    AUDIO_STREAM_END   = "audio_stream_end"
    AUDIO_CHUNK        = "audio_chunk"

    CAMERA_REQUEST     = "camera_request"
    CAMERA_FRAME       = "camera_frame"

    SERVO_COMMAND      = "servo_command"
    POSE_COMMAND       = "pose_command"

    GEMINI_REQUEST     = "gemini_request"
    GEMINI_RESPONSE    = "gemini_response"
    FUNCTION_CALL      = "function_call"
    FUNCTION_RESULT    = "function_result"

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


class RobotMessage(BaseModel):
    message_id: str   = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp:  float = Field(default_factory=time.time)
    version:    str   = PROTOCOL_VERSION

    model_config = {"frozen": False, "extra": "forbid"}


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
    type:       Literal[MessageType.AUDIO_CHUNK] = MessageType.AUDIO_CHUNK
    session_id: str
    seq:        int
    size_bytes: int


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
    data_b64:   Optional[str] = None


class ServoCommand(RobotMessage):
    type:        Literal[MessageType.SERVO_COMMAND] = MessageType.SERVO_COMMAND
    channel:     int   = Field(..., ge=0, lt=16)
    pulse_us:    int   = Field(..., ge=SERVO_MIN_PULSE, le=SERVO_MAX_PULSE)
    duration_ms: int   = Field(500, ge=0, le=10_000)

    @field_validator("pulse_us")
    @classmethod
    def clamp_pulse(cls, v: int) -> int:
        return max(SERVO_MIN_PULSE, min(SERVO_MAX_PULSE, v))


class PoseCommand(RobotMessage):
    type:        Literal[MessageType.POSE_COMMAND] = MessageType.POSE_COMMAND
    pose_name:   str
    duration_ms: Optional[int] = Field(default=None, ge=0, le=10_000)
    easing:      Optional[str] = None


class FunctionCall(RobotMessage):
    type:      Literal[MessageType.FUNCTION_CALL] = MessageType.FUNCTION_CALL
    name:      str
    call_id:   str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class FunctionResult(RobotMessage):
    type:    Literal[MessageType.FUNCTION_RESULT] = MessageType.FUNCTION_RESULT
    call_id: str
    name:    str
    result:  Any = None
    error:   Optional[str] = None


class GeminiRequest(RobotMessage):
    type:       Literal[MessageType.GEMINI_REQUEST] = MessageType.GEMINI_REQUEST
    session_id: str
    text_input: Optional[str] = None
    camera_frame: Optional[CameraFrame] = None
    include_history: bool = True


class GeminiResponse(RobotMessage):
    type:           Literal[MessageType.GEMINI_RESPONSE] = MessageType.GEMINI_RESPONSE
    session_id:     str
    text:           Optional[str] = None
    emotion:        EmotionState  = EmotionState.NEUTRAL
    function_calls: List[FunctionCall]   = Field(default_factory=list)
    finish_reason:  str = "stop"
    tokens_used:    int = 0


class ComponentStatus(BaseModel):
    name:    str
    ok:      bool
    detail:  str = ""


class SystemStatus(RobotMessage):
    type:       Literal[MessageType.SYSTEM_STATUS] = MessageType.SYSTEM_STATUS
    node:       str
    components: List[ComponentStatus] = Field(default_factory=list)
    cpu_pct:    float = 0.0
    mem_pct:    float = 0.0
    temp_c:     Optional[float] = None


class Heartbeat(RobotMessage):
    type: Literal[MessageType.HEARTBEAT] = MessageType.HEARTBEAT
    node: str


class ErrorMessage(RobotMessage):
    type:      Literal[MessageType.ERROR] = MessageType.ERROR
    code:      str
    message:   str
    context:   Dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


_Payload = Union[
    AudioStreamStart, AudioStreamEnd, AudioChunk,
    CameraRequest, CameraFrame,
    ServoCommand, PoseCommand,
    GeminiRequest, GeminiResponse,
    FunctionCall, FunctionResult,
    SystemStatus, Heartbeat, ErrorMessage,
]

class MessageEnvelope(BaseModel):
    payload: _Payload = Field(..., discriminator="type")

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str | bytes) -> "MessageEnvelope":
        return cls.model_validate_json(raw)

    model_config = {"frozen": False}
