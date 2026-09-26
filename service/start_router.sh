#!/usr/bin/env bash
# Idempotent launcher for the dsh-router-laya judge service -- the bash twin of the package's
# start_router.ps1 (which mirrors routing/start_router.ps1's behaviour: skip when /health already
# answers, wait up to 60s for the model load, log the child's output to temp files).
#
#   bash service/start_router.sh [--port 8765] [--device cpu] [--dry-run]
#
# Environment:
#   LAYA_PYTHON         python executable to use (default: resolved below)
#   LAYA_MODEL          checkpoint directory (default: resolved below)
#   LAYA_DEVICE         "cpu" | "cuda" | "mps" (auto-detected when unset)
#   LAYA_START_TIMEOUT  health-wait seconds (default 60; CPU cold loads can take ~70s)
#
# Resolution order:
#   python: $LAYA_PYTHON -> <package>/.venv-router/{Scripts/python.exe,bin/python} -> python3/python on PATH
#   model:  $LAYA_MODEL -> <package>/weights/model -> nearest training/laya_router_finetuned (dev checkout)
set -u

PORT=8765
DEVICE=""
DRY_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --device) DEVICE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "[start_router] unknown argument: $1 (usage: [--port N] [--device cpu] [--dry-run])" >&2; exit 2 ;;
    esac
done

PKG="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$PKG/service/laya_router.py"
HEALTH_URL="http://127.0.0.1:$PORT/health"
TIMEOUT="${LAYA_START_TIMEOUT:-60}"
LOG_DIR="${TMPDIR:-/tmp}"
OUT_LOG="$LOG_DIR/laya-router-service.out.log"
ERR_LOG="$LOG_DIR/laya-router-service.err.log"

# native_path: MSYS/Git Bash paths (/e/...) mean nothing to Windows Python; convert when possible.
native_path() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -m "$1"
    else
        realpath "$1" 2>/dev/null || printf '%s' "$1"
    fi
}

health() {
    if ! command -v curl >/dev/null 2>&1; then
        echo "[start_router] curl is required for the /health probe (or set LAYA_PYTHON and use start_router.ps1)" >&2
        exit 2
    fi
    curl -s -m 2 "$HEALTH_URL" 2>/dev/null
}

# ── idempotence: already up (and speaking the finetuned protocol) -> nothing to do ──────────
HEALTH_JSON="$(health || true)"
if printf '%s' "$HEALTH_JSON" | grep -q '"protocol": *"finetuned"'; then
    echo "[start_router] already running on :$PORT (protocol=finetuned) -- skip"
    exit 0
fi
if [ -n "$HEALTH_JSON" ]; then
    echo "[start_router] WARNING: something answers :$PORT but not with protocol=finetuned ($HEALTH_JSON)"
fi

# ── resolve python ───────────────────────────────────────────────────────────────────────────
# Each candidate is probed with a real import AND the >= 3.10 floor (the WindowsApps `python3`
# alias and older PATH pythons are executable but cannot run this service).
PY=""
probe_python() { "$1" -c "import sys; assert sys.version_info >= (3, 10)" >/dev/null 2>&1; }
if [ -n "${LAYA_PYTHON:-}" ] && [ -x "${LAYA_PYTHON:-}" ] && probe_python "$LAYA_PYTHON"; then
    PY="$LAYA_PYTHON"
else
    for cand in "$PKG/.venv-router/Scripts/python.exe" "$PKG/.venv-router/bin/python" \
                "$(command -v python3 2>/dev/null)" "$(command -v python 2>/dev/null)"; do
        if [ -n "$cand" ] && [ -x "$cand" ] && probe_python "$cand"; then PY="$cand"; break; fi
    done
fi
if [ -z "$PY" ]; then
    echo "[start_router] no python found -- run 'node bin/setup.mjs' first, or set LAYA_PYTHON" >&2
    exit 2
fi

# ── resolve the checkpoint and the service script ────────────────────────────────────────────
if [ -n "${LAYA_MODEL:-}" ]; then
    MODEL="$LAYA_MODEL"
elif [ -f "$PKG/weights/model/model.safetensors" ]; then
    MODEL="$(native_path "$PKG/weights/model")"
else
    # Dev-checkout fallback: walk up to the source repo whose training/ dir holds the checkpoint.
    MODEL=""
    dir="$PKG"
    for _ in 1 2 3 4 5 6; do
        if [ -f "$dir/training/laya_router_finetuned/model.safetensors" ]; then
            MODEL="$(native_path "$dir/training/laya_router_finetuned")"
            break
        fi
        parent="$(dirname "$dir")"
        [ "$parent" = "$dir" ] && break
        dir="$parent"
    done
fi
if [ -z "$MODEL" ]; then
    echo "[start_router] no checkpoint found: run 'node weights/fetch.mjs' to fill $PKG/weights/model," >&2
    echo "[start_router] or set LAYA_MODEL to an existing laya_router_finetuned directory." >&2
    exit 2
fi
if [ ! -f "$SCRIPT" ]; then
    echo "[start_router] service script missing: $SCRIPT" >&2
    exit 2
fi

if [ "$DRY_RUN" = "1" ]; then
    echo "[start_router] dry-run: would launch on :$PORT"
    echo "  python : $PY"
    echo "  script : $SCRIPT"
    echo "  model  : $MODEL"
    echo "  device : ${DEVICE:-${LAYA_DEVICE:-auto}}"
    echo "  health : $HEALTH_URL (wait up to ${TIMEOUT}s)"
    echo "  logs   : $OUT_LOG / $ERR_LOG"
    exit 0
fi

export PYTHONIOENCODING=utf-8
[ -n "$DEVICE" ] && export LAYA_DEVICE="$DEVICE"

echo "[start_router] launching service/laya_router.py --http on :$PORT (model: $MODEL)"
cd "$PKG" || exit 2
nohup "$PY" "$SCRIPT" --http --port "$PORT" >"$OUT_LOG" 2>"$ERR_LOG" &
PID=$!
disown "$PID" 2>/dev/null || true

# ── wait for /health (model load: seconds on GPU, ~70s on CPU) ───────────────────────────────
deadline=$(( $(date +%s) + TIMEOUT ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 0.5
    HEALTH_JSON="$(health || true)"
    if printf '%s' "$HEALTH_JSON" | grep -q '"protocol": *"finetuned"'; then
        echo "[start_router] ready on :$PORT (protocol=finetuned, pid=$PID)"
        exit 0
    fi
done
echo "[start_router] service did not become healthy in ${TIMEOUT}s -- see $ERR_LOG" >&2
exit 1
