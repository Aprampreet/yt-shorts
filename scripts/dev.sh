#!/usr/bin/env bash
# Start the whole studio locally: Redis, API, two Celery workers, beat and the Vite front end.
# Usage: scripts/dev.sh        (Ctrl-C stops everything it started)
# Logs:  backend/.local/logs/*.log
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
CELERY="$ROOT/.venv/bin/celery"
LOGS="$ROOT/backend/.local/logs"
mkdir -p "$LOGS"

# Homebrew's libraries (fribidi) must be visible to Pillow for correct Hindi/Devanagari text.
export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
export DJANGO_DEBUG="${DJANGO_DEBUG:-true}"

[ -x "$PY" ] || { echo "Missing .venv - run: python3.13 -m venv .venv && .venv/bin/pip install -r backend/requirements.lock"; exit 1; }
[ -f "$ROOT/.env" ] || { echo "Missing .env - run: cp .env.example .env"; exit 1; }

PIDS=()
cleanup() {
  echo; echo "Stopping studio..."
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
  wait 2>/dev/null
}
trap cleanup EXIT INT TERM

start() { # name, command...
  local name="$1"; shift
  "$@" >"$LOGS/$name.log" 2>&1 &
  PIDS+=($!)
  echo "started $name (pid $!) -> backend/.local/logs/$name.log"
}

if redis-cli -h 127.0.0.1 ping >/dev/null 2>&1; then
  echo "Redis already running"
else
  # Own data dir: a stray dump.rdb in the project root (e.g. from a newer Redis) would stop Redis starting.
  mkdir -p "$ROOT/backend/.local/redis"
  start redis redis-server --bind 127.0.0.1 --dir "$ROOT/backend/.local/redis"
  sleep 1
fi

"$PY" backend/manage.py migrate --noinput >"$LOGS/migrate.log" 2>&1 || { echo "migrate failed, see $LOGS/migrate.log"; exit 1; }

start api "$PY" backend/manage.py runserver 127.0.0.1:8000
cd "$ROOT/backend"
start worker-media "$CELERY" -A backend worker -Q media -n media@%h --pool=solo --concurrency=1 --prefetch-multiplier=1 --loglevel=INFO
start worker-ai "$CELERY" -A backend worker -Q ai,publish,celery -n ai@%h --concurrency=2 --prefetch-multiplier=1 --loglevel=INFO
start beat "$CELERY" -A backend beat --loglevel=INFO --schedule "$ROOT/backend/.local/celerybeat-schedule"
cd "$ROOT"
start web npm --prefix frontend run dev -- --port 5173 --strictPort

echo
echo "Studio:  http://localhost:5173"
echo "API:     http://localhost:8000/api/docs"
echo "Ctrl-C to stop everything."
wait
