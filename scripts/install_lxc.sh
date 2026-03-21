#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
DATA_DIR="${DATA_DIR:-/var/lib/expense-tracker}"
CONFIG_DIR="${CONFIG_DIR:-/etc/expense-tracker}"
SERVICE_NAME="${SERVICE_NAME:-expense-tracker}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn "$SERVICE_USER")}"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv}"
ENV_FILE="$CONFIG_DIR/expense-tracker.env"
AUTH_TOKEN_FILE="$CONFIG_DIR/auth.token"
OPENAI_KEY_FILE="$CONFIG_DIR/openai.key"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this script as root inside the LXC guest." >&2
    exit 1
  fi
}

require_root

echo "==> Creating runtime directories"
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$DATA_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$CONFIG_DIR"

echo "==> Creating virtualenv and installing the app"
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -e "$REPO_DIR"

if [[ ! -f "$AUTH_TOKEN_FILE" ]]; then
  echo "==> Generating auth token"
  TOKEN="$("$VENV_DIR/bin/expense-tracker" generate-auth-token --length 32 | tail -n 1)"
  install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_GROUP" /dev/null "$AUTH_TOKEN_FILE"
  printf '%s\n' "$TOKEN" >"$AUTH_TOKEN_FILE"
  echo "Generated auth token at $AUTH_TOKEN_FILE"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "==> Writing environment file"
  cat >"$ENV_FILE" <<EOF
EXPENSE_TRACKER_AUTH_TOKEN_FILE=$AUTH_TOKEN_FILE
EXPENSE_TRACKER_OPENAI_API_KEY_FILE=$OPENAI_KEY_FILE
EXPENSE_TRACKER_SECURE_COOKIES=true
EXPENSE_TRACKER_ALLOWED_HOSTS=localhost,127.0.0.1
EOF
  chmod 0600 "$ENV_FILE"
  chown "$SERVICE_USER:$SERVICE_GROUP" "$ENV_FILE"
fi

echo "==> Installing systemd unit"
cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Expense Tracker
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$REPO_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/expense-tracker --data-file $DATA_DIR/ledger.db serve --host $HOST --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo
echo "Install complete."
echo "Service: systemctl status $SERVICE_NAME"
echo "Auth token file: $AUTH_TOKEN_FILE"
echo "Environment file: $ENV_FILE"
echo "OpenAI key file (optional): $OPENAI_KEY_FILE"
echo "Remember to set EXPENSE_TRACKER_ALLOWED_HOSTS in $ENV_FILE to your real hostnames before exposing the service."
