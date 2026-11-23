#!/usr/bin/env bash
# Install mistral-vibe-relay as a systemd user service.
# Nothing outside the user's home is touched.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/mistral-vibe-relay"
ENV_FILE="$ENV_DIR/env"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/mistral-vibe-relay.service"

mkdir -p "$ENV_DIR" "$UNIT_DIR"
chmod 700 "$ENV_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  cat > "$ENV_FILE" <<'EOF'
# mistral-vibe-relay environment. Keep this file at mode 600.
MISTRAL_API_KEY=
# RELAY_HOST=127.0.0.1
# RELAY_PORT=18999
# RELAY_SIGN=1
# RELAY_NORMALIZE=1
# RELAY_CLIENT_KEY=
EOF
  echo "created $ENV_FILE - put your key there, then enable the unit"
fi
chmod 600 "$ENV_FILE"

sed "s|__RELAY_DIR__|$SRC_DIR|g; s|__ENV_FILE__|$ENV_FILE|g" \
  "$SRC_DIR/systemd/mistral-vibe-relay.service" > "$UNIT_FILE"
chmod 644 "$UNIT_FILE"

systemctl --user daemon-reload
echo "installed $UNIT_FILE"
echo
echo "next:"
echo "  1) edit $ENV_FILE (MISTRAL_API_KEY=...)"
echo "  2) systemctl --user enable --now mistral-vibe-relay"
echo "  3) curl -s localhost:18999/healthz"
