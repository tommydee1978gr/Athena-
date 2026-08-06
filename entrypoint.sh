#!/usr/bin/env sh
set -eu
umask 077
mkdir -p /config /media /config/models /config/releases /config/tmp /config/cliproxy/auths /config/cliproxy/logs
python -c 'from app.cliproxy import bootstrap; bootstrap()'

/usr/local/bin/CLIProxyAPI --config /config/cliproxy/config.yaml >>/config/cliproxy/logs/stdout.log 2>&1 &
PROXY_PID=$!
APP_PID=""

stop_all() {
  if [ -n "${APP_PID}" ]; then kill "${APP_PID}" 2>/dev/null || true; fi
  kill "${PROXY_PID}" 2>/dev/null || true
  wait "${APP_PID}" 2>/dev/null || true
  wait "${PROXY_PID}" 2>/dev/null || true
}
trap stop_all INT TERM EXIT

python - <<'PY'
import socket, time, sys
for _ in range(120):
    try:
        with socket.create_connection(("127.0.0.1", 8317), timeout=1):
            sys.exit(0)
    except OSError:
        time.sleep(0.5)
sys.exit("CLIProxyAPI did not open port 8317")
PY

uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${ATHENA_PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips "${ATHENA_FORWARDED_ALLOW_IPS:-127.0.0.1}" &
APP_PID=$!

while kill -0 "${PROXY_PID}" 2>/dev/null && kill -0 "${APP_PID}" 2>/dev/null; do
  sleep 2
done

if ! kill -0 "${PROXY_PID}" 2>/dev/null; then
  echo "CLIProxyAPI exited unexpectedly" >&2
  tail -n 200 /config/cliproxy/logs/stdout.log >&2 || true
fi
if ! kill -0 "${APP_PID}" 2>/dev/null; then
  echo "ATHENA web process exited unexpectedly" >&2
fi
exit 1
