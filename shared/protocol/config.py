"""
shared/protocol/config.py
==========================
YAML + 環境変数によるコンフィグ管理。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, SecretStr


class NetworkConfig(BaseModel):
    server_host:             str   = "100.x.x.x"
    server_port:             int   = 8000
    ws_audio_path:           str   = "/ws/audio"
    ws_control_path:         str   = "/ws/control"
    connect_timeout_sec:     int   = 10
    reconnect_interval_sec:  int   = 5
    max_reconnect_attempts:  int   = 0
    heartbeat_interval_sec:  int   = 15


class GoogleSttConfig(BaseModel):
    language_code: str = "ja-JP"


class VoicevoxConfig(BaseModel):
    enabled:     bool  = True
    host:        str   = "127.0.0.1"
    port:        int   = 50021
    speaker_id:  int   = 2
    timeout_sec: float = 10.0


class AudioConfig(BaseModel):
    device_name: str = "AIY-voice-hat-Sound-Card"
    wake_words:  List[str] = Field(default_factory=lambda: ["ねえロボット"])


class ServoSubConfig(BaseModel):
    i2c_address:   int = 0x40
    pwm_frequency: int = 50
    channels:      Dict[str, int] = Field(default_factory=dict)


class ServoConfig(BaseModel):
    leg:  ServoSubConfig = Field(default_factory=lambda: ServoSubConfig(i2c_address=0x40))
    head: ServoSubConfig = Field(default_factory=lambda: ServoSubConfig(i2c_address=0x41))
    min_pulse_us:     int = 500
    max_pulse_us:     int = 2500
    neutral_pulse_us: int = 1500


class DisplayConfig(BaseModel):
    enabled:            bool = True
    driver:             str  = "st7789"
    width:              int  = 240
    height:             int  = 240
    spi_bus:            int  = 0
    spi_device:         int  = 0
    spi_speed_hz:       int  = 40000000
    pin_dc:             int  = 6
    pin_reset:          int  = 26
    pin_backlight:      int  = 13
    default_brightness: float = 1.0


class CameraConfig(BaseModel):
    device:          int  = 0
    default_width:   int  = 640
    default_height:  int  = 480
    default_quality: int  = 85
    fps:             int  = 10
    rotation:        int  = 0


class AutonomousLoopConfig(BaseModel):
    enabled:               bool = True
    idle_trigger_sec:      int  = 30
    max_autonomous_tokens: int  = 256


class ToolConfig(BaseModel):
    mode: str = "AUTO"


class GeminiConfig(BaseModel):
    model:           str   = "gemini-2.5-flash-lite"
    max_tokens:      int   = 1024
    temperature:     float = 0.85
    tool_config:     ToolConfig = Field(default_factory=ToolConfig)
    autonomous_loop: AutonomousLoopConfig = Field(default_factory=AutonomousLoopConfig)


class ServerConfig(BaseModel):
    host:    str = "0.0.0.0"
    port:    int = 8000
    workers: int = 1


class SafetyConfig(BaseModel):
    cpu_temp_max_c:         float = 80.0
    servo_stall_timeout_ms: int   = 2000
    watchdog_timeout_sec:   int   = 30


class SystemConfig(BaseModel):
    name:      str = "RobotChan"
    log_level: str = "INFO"
    log_file:  str = "logs/robot.log"


class SecretsConfig(BaseModel):
    gemini_api_key: Optional[SecretStr] = None
    tailscale_auth_key: Optional[SecretStr] = None


class RobotConfig(BaseModel):
    system:  SystemConfig  = Field(default_factory=SystemConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    audio:   AudioConfig   = Field(default_factory=AudioConfig)
    google_stt: GoogleSttConfig = Field(default_factory=GoogleSttConfig)
    voicevox: VoicevoxConfig = Field(default_factory=VoicevoxConfig)
    servo:   ServoConfig   = Field(default_factory=ServoConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    camera:  CameraConfig  = Field(default_factory=CameraConfig)
    gemini:  GeminiConfig  = Field(default_factory=GeminiConfig)
    server:  ServerConfig  = Field(default_factory=ServerConfig)
    safety:  SafetyConfig  = Field(default_factory=SafetyConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def load_config(config_path: str | None = None) -> RobotConfig:
    root = Path(__file__).parent.parent.parent
    yaml_path = Path(config_path) if config_path else root / "config" / "settings.yaml"

    data = _load_yaml(yaml_path)

    secrets: Dict[str, Any] = {}
    if api_key := os.environ.get("GEMINI_API_KEY"):
        secrets["gemini_api_key"] = api_key
    if ts_key := os.environ.get("TAILSCALE_AUTH_KEY"):
        secrets["tailscale_auth_key"] = ts_key
    data["secrets"] = secrets

    return RobotConfig.model_validate(data)


cfg: RobotConfig = load_config()
