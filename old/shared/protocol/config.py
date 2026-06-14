"""
shared/protocol/config.py
==========================
YAML + 環境変数によるコンフィグ管理。

優先順位:
  1. 環境変数 (ROBOT_ プレフィックス)
  2. config/settings.yaml
  3. ハードコードデフォルト

使用例:
    from shared.protocol.config import cfg
    host = cfg.network.server_host
    api_key = cfg.secrets.gemini_api_key
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, SecretStr


# ---------------------------------------------------------------------------
# 設定モデル群
# ---------------------------------------------------------------------------
class NetworkConfig(BaseModel):
    server_host:             str   = "100.x.x.x"
    server_port:             int   = 8000
    ws_audio_path:           str   = "/ws/audio"
    ws_control_path:         str   = "/ws/control"
    funnel_port:             int   = 443
    webui_path:              str   = "/ui"
    connect_timeout_sec:     int   = 10
    reconnect_interval_sec:  int   = 5
    max_reconnect_attempts:  int   = 0
    heartbeat_interval_sec:  int   = 15


class AudioConfig(BaseModel):
    device_name:          str   = "AIY-voice-hat-Sound-Card"
    sample_rate:          int   = 16000
    channels:             int   = 1
    chunk_ms:             int   = 100
    vosk_model_path:      str   = "/home/pi/models/vosk-model-ja-0.22"
    wake_words:           List[str] = Field(default_factory=lambda: ["ねえロボット"])
    wake_word_threshold:  float = 0.7
    silence_threshold_ms: int   = 1200
    silence_amplitude:    int   = 500


class ServoChannelMap(BaseModel):
    head_pan:        int = 0
    head_tilt:       int = 1
    head_roll:       int = 2
    arm_l_shoulder:  int = 3
    arm_l_elbow:     int = 4
    arm_r_shoulder:  int = 5
    arm_r_elbow:     int = 6
    body_tilt:       int = 7


class ServoConfig(BaseModel):
    i2c_bus:          int  = 1
    i2c_address:      int  = 0x40
    pwm_frequency:    int  = 50
    min_pulse_us:     int  = 500
    max_pulse_us:     int  = 2500
    neutral_pulse_us: int  = 1500
    channels:         ServoChannelMap = Field(default_factory=ServoChannelMap)
    max_step_ms:      int  = 20
    init_wait_ms:     int  = 500


class CameraConfig(BaseModel):
    device:          int  = 0
    default_width:   int  = 640
    default_height:  int  = 480
    default_quality: int  = 85
    fps:             int  = 10
    auto_wb:         bool = True
    rotation:        int  = 0


class AutonomousLoopConfig(BaseModel):
    enabled:                bool = True
    idle_trigger_sec:       int  = 30
    max_autonomous_tokens:  int  = 256


class GeminiConfig(BaseModel):
    model:             str   = "gemini-1.5-flash"
    max_tokens:        int   = 1024
    temperature:       float = 0.85
    system_prompt_key: str   = "default_robot"
    autonomous_loop:   AutonomousLoopConfig = Field(default_factory=AutonomousLoopConfig)


class ServerConfig(BaseModel):
    host:         str       = "0.0.0.0"
    port:         int       = 8000
    workers:      int       = 1
    reload:       bool      = False
    cors_origins: List[str] = Field(default_factory=list)


class SafetyConfig(BaseModel):
    cpu_temp_max_c:       float = 80.0
    cpu_load_max_pct:     float = 90.0
    servo_stall_timeout_ms: int = 2000
    watchdog_timeout_sec: int   = 30


class SystemConfig(BaseModel):
    name:      str = "RobotChan"
    log_level: str = "INFO"
    log_file:  str = "logs/robot.log"


class SecretsConfig(BaseModel):
    """環境変数からのみ読み込む機密情報"""
    gemini_api_key: Optional[SecretStr] = None
    tailscale_auth_key: Optional[SecretStr] = None


class RobotConfig(BaseModel):
    system:  SystemConfig  = Field(default_factory=SystemConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    audio:   AudioConfig   = Field(default_factory=AudioConfig)
    servo:   ServoConfig   = Field(default_factory=ServoConfig)
    camera:  CameraConfig  = Field(default_factory=CameraConfig)
    gemini:  GeminiConfig  = Field(default_factory=GeminiConfig)
    server:  ServerConfig  = Field(default_factory=ServerConfig)
    safety:  SafetyConfig  = Field(default_factory=SafetyConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)


# ---------------------------------------------------------------------------
# ローダー
# ---------------------------------------------------------------------------
def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """再帰的に辞書をマージ (override 優先)"""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _apply_env_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    ROBOT_<SECTION>_<KEY>=value 形式の環境変数を適用する。
    例: ROBOT_NETWORK_SERVER_HOST=100.1.2.3
        ROBOT_GEMINI_TEMPERATURE=0.9
    """
    for key, val in os.environ.items():
        if not key.startswith("ROBOT_"):
            continue
        parts = key.lower().split("_")[1:]   # ROBOT_ を除去
        if len(parts) < 2:
            continue
        section, *rest = parts
        field = "_".join(rest)
        if section not in data:
            data[section] = {}
        # 型推論: YAML 側で数値・真偽値が設定されていれば合わせる
        existing = data[section].get(field)
        if isinstance(existing, bool):
            data[section][field] = val.lower() in ("1", "true", "yes")
        elif isinstance(existing, float):
            try:
                data[section][field] = float(val)
            except ValueError:
                pass
        elif isinstance(existing, int):
            try:
                data[section][field] = int(val)
            except ValueError:
                pass
        else:
            data[section][field] = val
    return data


@lru_cache(maxsize=1)
def load_config(config_path: str | None = None) -> RobotConfig:
    """設定をロードしてキャッシュする (プロセス中1回のみ読み込み)"""
    root = Path(__file__).parent.parent.parent  # プロジェクトルート
    yaml_path = Path(config_path) if config_path else root / "config" / "settings.yaml"

    data = _load_yaml(yaml_path)
    data = _apply_env_overrides(data)

    # 機密情報は環境変数から直接読む (YAML には書かない)
    secrets: Dict[str, Any] = {}
    if api_key := os.environ.get("GEMINI_API_KEY"):
        secrets["gemini_api_key"] = api_key
    if ts_key := os.environ.get("TAILSCALE_AUTH_KEY"):
        secrets["tailscale_auth_key"] = ts_key
    data["secrets"] = secrets

    return RobotConfig.model_validate(data)


# シングルトンアクセス
cfg: RobotConfig = load_config()
