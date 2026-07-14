#!/usr/bin/env bash
# xbow CTF platform start/stop script
# Usage: ./platformctl.sh start|stop|status|restart

ACTION="${1:-status}"
PLATFORM_DIR="$HOME/xbow-platform"
VENV_DIR="$HOME/platform_venv"
PID_FILE="$HOME/platform.pid"
LOG_FILE="$HOME/platform.log"
PORT=4444

start() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
        echo "Platform already running (PID $(cat $PID_FILE))"
        exit 0
    fi
    echo "Starting xbow CTF platform on port $PORT..."
    cd "$PLATFORM_DIR"
    export XBEN_REPO_DIR="$HOME/validation-benchmarks"
    export XBEN_RUNS_DIR="$HOME/runs"
    export XBEN_DATA_DIR="$HOME/data"
    export XBEN_FLAG_MANIFEST="$PLATFORM_DIR/flag_manifest.json"
    export XBEN_ADMIN_USER=admin
    export XBEN_ADMIN_PASSWORD=LXW@123
    export XBEN_PUBLIC_BASE_URL="http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
    export XBEN_CHALLENGE_HOST=127.0.0.1
    export XBEN_REGISTRATION_CODE=ROOTLAB@123
    export XBEN_SECRET_KEY="xbow-prod-$(openssl rand -hex 16)"
    mkdir -p "$HOME/runs" "$HOME/data"
    nohup "$VENV_DIR/bin/uvicorn" app.main:app --host 0.0.0.0 --port $PORT > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 4
    if curl -sS --max-time 5 "http://127.0.0.1:$PORT/health" | grep -q ok; then
        echo "✅ Platform started (PID $(cat $PID_FILE))"
        echo "   URL: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
        echo "   Admin: admin / LXW@123"
    else
        echo "❌ Platform failed to start, check $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

stop() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "Stopping platform (PID $PID)..."
            kill "$PID"
            sleep 2
            kill -0 "$PID" 2>/dev/null && kill -9 "$PID"
            echo "✅ Platform stopped (data preserved)"
        else
            echo "Process not running, cleaning PID file"
        fi
        rm -f "$PID_FILE"
    else
        # fallback: find by name
        pkill -f 'uvicorn.*app.main' 2>/dev/null && echo "✅ Platform stopped" || echo "Platform not running"
    fi
    # also stop any running challenge containers
    RUNNING=$(docker ps --filter "name=xben_" -q 2>/dev/null)
    if [ -n "$RUNNING" ]; then
        echo "Stopping $(echo "$RUNNING" | wc -l) running challenge containers..."
        echo "$RUNNING" | xargs docker rm -f 2>/dev/null
    fi
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
        echo "✅ Running (PID $(cat $PID_FILE))"
        curl -sS --max-time 5 "http://127.0.0.1:$PORT/health" 2>/dev/null && echo
    else
        echo "❌ Not running"
    fi
}

case "$ACTION" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)  status ;;
    *) echo "Usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac
