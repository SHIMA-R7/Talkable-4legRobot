#!/usr/bin/env bash
# scripts/setup_edge.sh
# ======================
# Raspberry Pi 3B (64bit Lite) セットアップスクリプト
# 実行: sudo bash scripts/setup_edge.sh

set -euo pipefail

PYTHON=python3.11
VENV_DIR="/home/pi/robot-venv"
PROJECT_DIR="/home/pi/robot"
VOSK_MODEL_URL="https://alphacephei.com/vosk/models/vosk-model-ja-0.22.zip"
VOSK_MODEL_DIR="/home/pi/models"

echo "=== [1/7] システムパッケージ ==="
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3.11-dev \
    portaudio19-dev libatlas-base-dev \
    i2c-tools libi2c-dev \
    git curl unzip \
    libcamera-dev python3-libcamera \
    libcap-dev

echo "=== [2/7] I2C / カメラ有効化 ==="
# /boot/config.txt に追記 (冪等)
grep -qxF 'dtparam=i2c_arm=on' /boot/config.txt || echo 'dtparam=i2c_arm=on' >> /boot/config.txt
grep -qxF 'dtparam=i2s=on'     /boot/config.txt || echo 'dtparam=i2s=on'     >> /boot/config.txt
grep -qxF 'dtoverlay=googlevoicehat-soundcard' /boot/config.txt \
    || echo 'dtoverlay=googlevoicehat-soundcard' >> /boot/config.txt

# I2C モジュール自動ロード
grep -qxF 'i2c-dev' /etc/modules || echo 'i2c-dev' >> /etc/modules

echo "=== [3/7] Python 仮想環境 ==="
$PYTHON -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip wheel

echo "=== [4/7] Python パッケージ ==="
pip install -r "$PROJECT_DIR/requirements-edge.txt"

echo "=== [5/7] VOSK モデルダウンロード ==="
mkdir -p "$VOSK_MODEL_DIR"
if [ ! -d "$VOSK_MODEL_DIR/vosk-model-ja-0.22" ]; then
    curl -L "$VOSK_MODEL_URL" -o /tmp/vosk-model-ja.zip
    unzip -q /tmp/vosk-model-ja.zip -d "$VOSK_MODEL_DIR"
    rm /tmp/vosk-model-ja.zip
fi

echo "=== [6/7] Tailscale インストール ==="
if ! command -v tailscale &>/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
fi
echo "Tailscale インストール済み。'sudo tailscale up' で認証してください。"

echo "=== [7/7] systemd サービス登録 ==="
cat > /etc/systemd/system/robot-edge.service << 'EOF'
[Unit]
Description=Robot Edge Service
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/robot
Environment=PYTHONPATH=/home/pi/robot
EnvironmentFile=/home/pi/robot/.env
ExecStart=/home/pi/robot-venv/bin/python -m edge.main
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable robot-edge.service
echo "サービス登録完了。起動: sudo systemctl start robot-edge"

echo ""
echo "======================================"
echo "  セットアップ完了！"
echo "  次のステップ:"
echo "  1. sudo reboot  (I2C/I2S 設定を反映)"
echo "  2. .env ファイルに GEMINI_API_KEY を設定"
echo "  3. sudo tailscale up --auth-key=<key>"
echo "  4. sudo systemctl start robot-edge"
echo "======================================"
