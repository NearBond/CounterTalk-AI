#!/usr/bin/env bash
# Starts the service: migrations first, then the server (batch 4 adds the worker).
#
# - A failed migration exits here, before anything serves a half-migrated database.
# - If any process exits, the others are stopped and the script exits non-zero, so the host
#   restarts the service.
# - SIGTERM and SIGINT are passed on, so a redeploy shuts the server down cleanly.
#
# Everything comes from the environment (DATABASE_URL, PORT, ...); nothing here is host-specific.
set -euo pipefail

cd "$(dirname "$0")/.."

pids=()

stop_all() {
  for pid in "${pids[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  wait || true
}

on_signal() {
  trap - TERM INT
  stop_all
  exit 143
}
trap on_signal TERM INT

# In the background, so a stop during the migrations reaches them: run in the foreground,
# bash (PID 1 in a container) would wait for them to finish before handling the signal.
# A failed migration rolls back whole, and `wait` returns its status, so set -e exits here.
alembic upgrade head &
pids=("$!")
wait "${pids[0]}"
pids=()

# Hosts terminate HTTPS in front of the app, so trust their X-Forwarded-* headers.
uvicorn countertalk.main:app \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips='*' &
pids+=("$!")

# TODO(batch 4): start the worker here, and add its PID to pids.

# Whichever process exits first, stop the rest and exit with an error.
set +e
wait -n
status=$?
set -e
trap - TERM INT
stop_all
if [ "$status" -eq 0 ]; then
  status=1
fi
exit "$status"
