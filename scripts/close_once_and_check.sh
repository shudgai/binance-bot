#!/usr/bin/env bash
set -u

# One-shot close helper:
# 1) call local API market-sell once per symbol
# 2) immediately query position and print remaining qty
#
# Usage:
#   ./scripts/close_once_and_check.sh ENAUSDT ZECUSDT
#   BASE_URL=http://127.0.0.1:8005 ./scripts/close_once_and_check.sh ENAUSDT

BASE_URL="${BASE_URL:-http://127.0.0.1:8005}"
TOLERANCE="${TOLERANCE:-0.000001}"

if [[ $# -eq 0 ]]; then
  SYMBOLS=("ENAUSDT" "ZECUSDT")
else
  SYMBOLS=("$@")
fi

normalize_symbol() {
  local s="$1"
  s="${s^^}"
  s="${s//\//}"
  s="${s//:/}"
  echo "$s"
}

post_close_once() {
  local sym="$1"
  local url="$BASE_URL/api/order/market-sell/$sym"
  echo "[close] POST $url"
  local body
  body="$(curl -sS -X POST "$url")"
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "[close] $sym request failed (curl rc=$rc)"
    return 1
  fi
  echo "[close] $sym response: $body"
  return 0
}

get_position_qty() {
  local sym="$1"
  local url="$BASE_URL/api/position/$sym"
  local body
  body="$(curl -sS "$url")"
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "[check] $sym position query failed (curl rc=$rc)"
    return 2
  fi

  local qty
  qty="$(printf '%s' "$body" | python3 -c 'import sys, json
raw = sys.stdin.read().strip() or "{}"
try:
    data = json.loads(raw)
except Exception:
    print("NaN")
    raise SystemExit(0)
q = data.get("qty", data.get("positionAmt", "NaN"))
print(q)
')"

  if [[ "$qty" == "NaN" ]]; then
    echo "[check] $sym response parse failed: $body"
    return 3
  fi

  echo "$qty"
  return 0
}

is_closed() {
  local qty="$1"
  local tol="$2"
  python3 - "$qty" "$tol" <<'PY'
import sys
try:
    qty = abs(float(sys.argv[1]))
    tol = float(sys.argv[2])
except Exception:
    print("0")
    raise SystemExit(0)
print("1" if qty <= tol else "0")
PY
}

echo "[info] BASE_URL=$BASE_URL"
echo "[info] SYMBOLS=${SYMBOLS[*]}"
echo "[info] TOLERANCE=$TOLERANCE"

declare -i open_count=0

echo
for raw in "${SYMBOLS[@]}"; do
  sym="$(normalize_symbol "$raw")"
  echo "========== $sym =========="

  if ! post_close_once "$sym"; then
    echo "[result] $sym close call failed"
    ((open_count+=1))
    echo
    continue
  fi

  qty="$(get_position_qty "$sym")"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "[result] $sym cannot confirm position (rc=$rc)"
    ((open_count+=1))
    echo
    continue
  fi

  closed="$(is_closed "$qty" "$TOLERANCE")"
  if [[ "$closed" == "1" ]]; then
    echo "[result] $sym CLOSED (qty=$qty)"
  else
    echo "[result] $sym STILL OPEN (qty=$qty)"
    ((open_count+=1))
  fi
  echo
done

if [[ $open_count -eq 0 ]]; then
  echo "[summary] all symbols are closed within tolerance"
  exit 0
else
  echo "[summary] $open_count symbol(s) still open or unconfirmed"
  exit 1
fi
