#!/bin/bash
# Unified market-data-service entrypoint.
#
# Starts IB Gateway + IBC in the background (X_SCRIPTS hook), waits for
# the paper API socket on 127.0.0.1:4004, and then execs uvicorn so the
# FastAPI process inherits PID 1.

set -Eeo pipefail

IB_API_PORT="${IB_API_PORT:-4004}"
IB_API_READY_TIMEOUT="${IB_API_READY_TIMEOUT:-180}"

echo "==> Starting IB Gateway + IBC in background"
bash /home/ibgateway/scripts/run.sh &
IBGW_PID=$!
trap 'echo "==> Stopping IB Gateway"; kill -TERM ${IBGW_PID} 2>/dev/null || true; wait' TERM INT

echo "==> Waiting for IB Gateway API on 127.0.0.1:${IB_API_PORT} (timeout=${IB_API_READY_TIMEOUT}s)"
deadline=$((SECONDS + IB_API_READY_TIMEOUT))
while (( SECONDS < deadline )); do
    if bash -c "</dev/tcp/127.0.0.1/${IB_API_PORT}" >/dev/null 2>&1; then
        echo "==> IB Gateway API reachable"
        break
    fi
    sleep 1
done

if ! bash -c "</dev/tcp/127.0.0.1/${IB_API_PORT}" >/dev/null 2>&1; then
    echo "!! IB Gateway API did not become ready in ${IB_API_READY_TIMEOUT}s" >&2
    exit 1
fi

echo "==> Launching market-data-service"
exec "$@"