#!/bin/bash
set -e

PROXY_BIND="${PROXY_BIND:-127.0.0.1:8086}"
CACHE_DIR="${WARP_PLUS_CACHE_DIR:-/app/.warp-plus-cache}"
USE_PROXY="${USE_PROXY:-false}"

echo "[entrypoint] cache dir: ${CACHE_DIR}"
mkdir -p "${CACHE_DIR}" || true
echo "[entrypoint] USE_PROXY=${USE_PROXY}"

WARP_PID=""
BOT_PID=""

if [ "${USE_PROXY}" = "true" ] && command -v warp-plus >/dev/null 2>&1; then
    echo "[entrypoint] starting warp-plus (SOCKS5 on ${PROXY_BIND}) ..."
    # -c オプションを外してデフォルトで自動登録・起動させます
    warp-plus -b "${PROXY_BIND}" &
    WARP_PID=$!
else
    if [ "${USE_PROXY}" = "true" ]; then
        echo "[entrypoint] USE_PROXY=true but warp-plus not installed or not in PATH; skipping warp-plus startup."
    else
        echo "[entrypoint] USE_PROXY is not true; skipping warp-plus startup."
    fi
fi

echo "[entrypoint] starting Discord bot (health check server opens immediately)..."
python3 main.py &
BOT_PID=$!

# どちらかが終了したらもう片方も止めて終了する
_term() {
    echo "[entrypoint] terminating..."
    if [ -n "${WARP_PID}" ]; then
        kill -TERM "${WARP_PID}" 2>/dev/null || true
    fi
    if [ -n "${BOT_PID}" ]; then
        kill -TERM "${BOT_PID}" 2>/dev/null || true
    fi
}
trap _term TERM INT

# wait for whichever process exits first (only wait on pids that exist)
if [ -n "${WARP_PID}" ] && [ -n "${BOT_PID}" ]; then
    wait -n "${WARP_PID}" "${BOT_PID}"
elif [ -n "${BOT_PID}" ]; then
    wait "${BOT_PID}"
elif [ -n "${WARP_PID}" ]; then
    wait "${WARP_PID}"
else
    echo "[entrypoint] neither warp-plus nor bot started; exiting."
    exit 1
fi

EXIT_CODE=$?
_term
exit "${EXIT_CODE}"
