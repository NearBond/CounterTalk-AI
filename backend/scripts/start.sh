#!/usr/bin/env bash
# Starts the service: a database check, the migrations, then the server and the worker.
#
# - A database that can't be reached, or a failed migration, exits here, before anything serves.
# - If any process exits, the others are stopped and the script exits non-zero, so the host
#   restarts the service.
# - SIGTERM and SIGINT are passed on, so a redeploy stops the server and the worker cleanly.
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

# Runs one step to the end, in the background, so a stop during it reaches it: in the
# foreground, bash (PID 1 in a container) would wait for the step to finish before handling the
# signal. `wait` returns the step's status, so a failure exits here (set -e).
run_stoppable() {
  "$@" &
  pids=("$!")
  wait "${pids[0]}"
  pids=()
}

# The database first: a wrong DATABASE_URL stops here with one plain line ("the password was
# refused"), never the URL's password or the server's own message.
run_stoppable python scripts/check_setup.py --database

# A failed migration rolls back whole, and the server never starts on a half-migrated database.
run_stoppable alembic upgrade head

# Hosts terminate HTTPS in front of the app, so trust their X-Forwarded-* headers.
uvicorn countertalk.main:app \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips='*' &
pids+=("$!")

# The worker runs the background jobs. SIGTERM lets its current job finish (worker.py).
python -m countertalk.worker &
pids+=("$!")

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
