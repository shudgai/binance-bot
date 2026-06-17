#!/usr/bin/env bash
set -euo pipefail

STATE_FILE="paperstate.json"
BACKUP_FILE="paperstate.json.bak.$(date +%Y%m%d%H%M%S)"

if [ -f "$STATE_FILE" ]; then
  cp "$STATE_FILE" "$BACKUP_FILE"
  echo "Backup saved: $BACKUP_FILE"
fi

cat > "$STATE_FILE" <<'JSON'
{
  "balanceusdt": 150.0,
  "realized_pnl": 0.0,
  "positions": {},
  "trades": []
}
JSON

echo "Reset done."
