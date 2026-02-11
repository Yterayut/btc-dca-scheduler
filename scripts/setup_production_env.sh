#!/usr/bin/env bash

set -euo pipefail

ENV_PATH="${1:-.env.production}"

if [[ -e "$ENV_PATH" ]]; then
  echo "Refusing to overwrite existing $ENV_PATH" >&2
  exit 1
fi

cat <<'EOF' > "$ENV_PATH"
# === BTC DCA Production Environment Template ===

# ---- Core database ----
DB_HOST=
DB_USER=
DB_PASSWORD=
DB_NAME=

# ---- Binance API (spot) ----
BINANCE_API_KEY=
BINANCE_API_SECRET=
USE_BINANCE_TESTNET=0
BINANCE_TESTNET=0

# ---- OKX API (spot) ----
OKX_API_KEY=
OKX_API_SECRET=
OKX_PASSPHRASE=
OKX_TESTNET=0
OKX_LIVE_ENABLED=1

# ---- Strategy Switches ----
DRY_RUN=0
STRATEGY_DRY_RUN=0
FEATURE_S4_ENABLED=1

# ---- Secrets / Security ----
ADMIN_TOKEN=
APP_ENCRYPTION_KEY=

# ---- Optional tuning ----
LIQUIDITY_MAX_SPREAD_PCT=0.60
DEPTH_GUARD_MIN_NOTIONAL_USDT=1000000
TWAP_GUARD_MAX_DEVIATION_PCT=1.5

# Export LINE credentials for notifications
LINE_CHANNEL_ACCESS_TOKEN=
LINE_USER_ID=
LINE_NOTIFY_TOKEN=
EOF

echo "Created $ENV_PATH with production defaults. Fill in secrets before starting services."
