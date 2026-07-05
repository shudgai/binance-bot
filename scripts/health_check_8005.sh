#!/usr/bin/env bash
set -u

PORT="${PORT:-8005}"
BASE_URL="${BASE_URL:-http://127.0.0.1:${PORT}}"
STATUS_URL="${STATUS_URL:-${BASE_URL}/api/bot-status}"

ok=1

echo "[info] PORT=${PORT}"
echo "[info] BASE_URL=${BASE_URL}"

# 1) port listener check
listener_count=0
if command -v ss >/dev/null 2>&1; then
  listener_count="$(ss -ltnp | grep -c ":${PORT} ")"
elif command -v lsof >/dev/null 2>&1; then
  listener_count="$(lsof -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null | wc -l)"
else
  listener_count=0
fi

if [[ "${listener_count}" -gt 0 ]]; then
  echo "[ok] port ${PORT} listener found (${listener_count})"
else
  echo "[fail] no listener on port ${PORT}"
  ok=0
fi

# 2) API status endpoint check
http_code="$(curl -sS -o /tmp/binance_api_status_8005.json -w '%{http_code}' "${STATUS_URL}" 2>/dev/null || true)"
if [[ "${http_code}" == "200" ]]; then
  echo "[ok] ${STATUS_URL} -> 200"
else
  echo "[fail] ${STATUS_URL} -> ${http_code:-N/A}"
  ok=0
fi

# 3) duplicate process check (strictly for api:app 8005)
proc_count="$(pgrep -af "uvicorn services.api:app --host 0.0.0.0 --port ${PORT}" | wc -l | tr -d ' ')"
if [[ "${proc_count}" == "1" ]]; then
  echo "[ok] single uvicorn process (${proc_count})"
else
  echo "[warn] uvicorn process count on ${PORT}: ${proc_count}"
  if [[ "${proc_count}" -eq 0 ]]; then
    ok=0
  fi
fi

# 4) quick payload summary
if [[ -f /tmp/binance_api_status_8005.json ]]; then
  python3 - <<'PY'
import json
p='/tmp/binance_api_status_8005.json'
try:
    data=json.load(open(p,'r',encoding='utf-8'))
except Exception:
    print('[info] bot-status payload unreadable')
    raise SystemExit(0)
print('[info] is_running=', data.get('is_running'))
print('[info] environment=', data.get('environment'))
print('[info] active_symbols=', len(data.get('active_symbols') or []))
PY
fi

if [[ "${ok}" -eq 1 ]]; then
  echo "[summary] HEALTHY"
  exit 0
else
  echo "[summary] UNHEALTHY"
  exit 1
fi
