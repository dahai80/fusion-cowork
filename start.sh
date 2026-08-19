#!/usr/bin/env bash
# start.sh — fusion-cowork lifecycle manager (start/stop/restart/status/log/doctor)

set -euo pipefail

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
# Monorepo 根 .venv 是 27 个 fusion-* 子项目的共享环境 (见 fusion/CLAUDE.md),
# fusion-cowork + fusion-plugins-ecosystem 均安装于此。优先使用根 venv, 仅在
# 独立部署 (无上层 monorepo) 时回退到本目录 .venv。历史 bug: 硬编码本地 .venv
# 导致服务跑在 stale editable install (fusion_cowork-0.1.3) 且缺 plugins, /rpc
# plugins.* 一律 -32603。
ROOT_VENV="$(cd "${PROJ_DIR}/.." 2>/dev/null && pwd)/.venv"
if [[ -f "${ROOT_VENV}/bin/activate" ]]; then
    VENV="${ROOT_VENV}"
else
    VENV="${PROJ_DIR}/.venv"
fi
ACTIVATE="${VENV}/bin/activate"
LOG_DIR="${PROJ_DIR}/logs"
PID_FILE="${PROJ_DIR}/.fusion-cowork.pid"
SOCK_FILE="/tmp/fusion-cowork.sock"

# ── Colors ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

log_info()  { printf "${GREEN}[INFO]${NC}  %s\n" "$*"; }
log_warn()  { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
log_error() { printf "${RED}[ERROR]${NC} %s\n" "$*"; }
log_step()  { printf "${CYAN}[STEP]${NC}  %s\n" "$*"; }

# ── Activate venv ───────────────────────────────────────────────────
ensure_venv() {
    if [[ ! -f "${ACTIVATE}" ]]; then
        log_error "Virtualenv not found at ${VENV}"
        exit 1
    fi
    source "${ACTIVATE}"
}

# ── Check if server is running ──────────────────────────────────────
is_running() {
    if [[ ! -f "${PID_FILE}" ]]; then
        return 1
    fi
    local pid
    pid=$(cat "${PID_FILE}" 2>/dev/null || echo "")
    if [[ -z "${pid}" ]]; then
        return 1
    fi
    kill -0 "${pid}" 2>/dev/null
}

get_pid() {
    if [[ -f "${PID_FILE}" ]]; then
        cat "${PID_FILE}" 2>/dev/null
    fi
}

# ── Wait for healthy ────────────────────────────────────────────────
wait_healthy() {
    local timeout="${1:-30}"
    local elapsed=0
    while (( elapsed < timeout )); do
        if [[ -S "${SOCK_FILE}" ]]; then
            log_info "Server is healthy (socket ready, took ${elapsed}s)"
            return 0
        fi
        sleep 1
        (( elapsed += 1 ))
    done
    log_error "Server socket not ready within ${timeout}s"
    return 1
}

# ── Preflight checks ────────────────────────────────────────────────
preflight() {
    log_step "Preflight checks"
    ensure_venv

    # Clean stale socket
    if [[ -S "${SOCK_FILE}" ]]; then
        if is_running; then
            log_warn "Server already running (PID $(get_pid))"
            return 1
        fi
        log_warn "Stale socket found at ${SOCK_FILE}, removing"
        rm -f "${SOCK_FILE}"
    fi

    # Ensure log directory
    mkdir -p "${LOG_DIR}"

    log_info "Preflight OK (socket=${SOCK_FILE})"
    return 0
}

# ── start ───────────────────────────────────────────────────────────
do_start() {
    if is_running; then
        log_warn "Server already running (PID $(get_pid))"
        wait_healthy 5
        return 0
    fi

    if ! preflight; then
        return 0
    fi

    log_step "Starting fusion-cowork desk RPC"

    local python_bin="${VENV}/bin/python"
    nohup "${python_bin}" -m fusion_cowork desk rpc --sock "${SOCK_FILE}" \
        >> "${LOG_DIR}/stdout.log" \
        2>> "${LOG_DIR}/stderr.log" &

    local pid=$!
    echo "${pid}" > "${PID_FILE}"
    log_info "Server PID: ${pid}"

    if wait_healthy 30; then
        log_info "Fusion-Cowork started successfully"
        show_status
    else
        log_error "Start failed. Check logs: ${LOG_DIR}/stderr.log"
        tail -20 "${LOG_DIR}/stderr.log" 2>/dev/null || true
        rm -f "${PID_FILE}"
        exit 1
    fi
}

# ── stop ────────────────────────────────────────────────────────────
do_stop() {
    if ! is_running; then
        log_warn "Server not running"
        rm -f "${PID_FILE}" "${SOCK_FILE}"
        return 0
    fi

    local pid
    pid=$(get_pid)
    log_step "Stopping fusion-cowork (PID ${pid})"

    # Graceful: SIGTERM
    kill -TERM "${pid}" 2>/dev/null || true
    local waited=0
    while (( waited < 15 )); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            log_info "Server stopped gracefully"
            rm -f "${PID_FILE}" "${SOCK_FILE}"
            return 0
        fi
        sleep 1
        (( waited += 1 ))
    done

    # Force: SIGKILL
    log_warn "Graceful shutdown timed out, force killing..."
    kill -KILL "${pid}" 2>/dev/null || true
    sleep 1
    rm -f "${PID_FILE}" "${SOCK_FILE}"
    log_info "Server force-stopped"
}

# ── restart ─────────────────────────────────────────────────────────
do_restart() {
    log_step "Restarting fusion-cowork"
    do_stop
    sleep 2
    do_start
}

# ── status ──────────────────────────────────────────────────────────
show_status() {
    echo ""
    printf "${BLUE}━━━ Fusion-Cowork Status ━━━${NC}\n"
    echo ""

    if is_running; then
        local pid
        pid=$(get_pid)
        printf "${GREEN}● Running${NC}  PID=%s\n" "${pid}"

        # Socket status
        if [[ -S "${SOCK_FILE}" ]]; then
            printf "  Socket: %s (ready)\n" "${SOCK_FILE}"
        else
            printf "  Socket: %s (not ready)\n" "${SOCK_FILE}"
        fi

        # Memory usage
        local rss
        rss=$(ps -o rss= -p "${pid}" 2>/dev/null | awk '{printf "%.1f MB", $1/1024}')
        printf "  Memory: %s\n" "${rss:-unknown}"

        # Uptime
        local uptime
        uptime=$(ps -o etime= -p "${pid}" 2>/dev/null | xargs || echo "unknown")
        printf "  Uptime: %s\n" "${uptime}"
    else
        printf "${RED}● Stopped${NC}\n"
    fi

    # Disk usage
    local log_size
    log_size=$(du -sh "${LOG_DIR}" 2>/dev/null | awk '{print $1}' || echo "N/A")
    printf "\n  Logs: %s\n" "${log_size}"
    echo ""
}

# ── log ─────────────────────────────────────────────────────────────
show_log() {
    local logfile="${LOG_DIR}/stderr.log"
    if [[ ! -f "${logfile}" ]]; then
        logfile="${LOG_DIR}/stdout.log"
    fi
    if [[ ! -f "${logfile}" ]]; then
        log_error "No log files found in ${LOG_DIR}"
        return 1
    fi
    local lines="${1:-50}"
    if [[ "${lines}" == "-f" ]]; then
        tail -f "${logfile}"
    else
        tail -n "${lines}" "${logfile}"
    fi
}

# ── clean ───────────────────────────────────────────────────────────
do_clean() {
    log_step "Cleaning up"

    # Rotate logs older than 7 days
    if [[ -d "${LOG_DIR}" ]]; then
        local count
        count=$(find "${LOG_DIR}" -name "*.log.*" -mtime +7 -delete -print 2>/dev/null | wc -l | tr -d ' ')
        log_info "Deleted ${count} old log files (7+ days)"
    fi

    # Clear __pycache__
    find "${PROJ_DIR}/fusion_cowork" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    log_info "Cleared __pycache__"

    # Remove stale PID and socket
    if [[ -f "${PID_FILE}" ]] && ! is_running; then
        rm -f "${PID_FILE}"
        log_info "Removed stale PID file"
    fi
    if [[ -S "${SOCK_FILE}" ]] && ! is_running; then
        rm -f "${SOCK_FILE}"
        log_info "Removed stale socket"
    fi

    log_info "Clean done"
}

# ── doctor ──────────────────────────────────────────────────────────
do_doctor() {
    ensure_venv
    echo ""
    printf "${BLUE}━━━ Fusion-Cowork Doctor ━━━${NC}\n"
    echo ""

    # Check venv
    if [[ -f "${ACTIVATE}" ]]; then
        printf "${GREEN}✓${NC} Virtualenv: %s\n" "${VENV}"
    else
        printf "${RED}✗${NC} Virtualenv missing: %s\n" "${VENV}"
    fi

    # Check CLI entry point
    if command -v fusion-cowork &>/dev/null; then
        local version
        version=$(fusion-cowork system info 2>/dev/null | head -1 || echo "unknown")
        printf "${GREEN}✓${NC} CLI: fusion-cowork available (%s)\n" "${version}"
    else
        printf "${RED}✗${NC} CLI: fusion-cowork not found (run: pip install -e .)\n"
    fi

    # Check socket
    if [[ -S "${SOCK_FILE}" ]]; then
        printf "${GREEN}✓${NC} Socket: %s (active)\n" "${SOCK_FILE}"
    else
        printf "${YELLOW}!${NC} Socket: %s (not active)\n" "${SOCK_FILE}"
    fi

    # Check fusion-gateway (netlayer, 反代 fusion-mlx)
    if curl -sf http://127.0.0.1:11432/v1/models >/dev/null 2>&1; then
        printf "${GREEN}✓${NC} Fusion-Gateway: reachable on port 11432 (netlayer → fusion-mlx)\n"
    else
        printf "${YELLOW}!${NC} Fusion-Gateway: not reachable on port 11432 (netlayer → fusion-mlx)\n"
    fi

    # Check fusion-rag
    if curl -sf http://127.0.0.1:11436/health >/dev/null 2>&1; then
        printf "${GREEN}✓${NC} Fusion-RAG: reachable on port 11436\n"
    else
        printf "${YELLOW}!${NC} Fusion-RAG: not reachable on port 11436\n"
    fi

    # Check HTTP channel (desk rpc 默认并发 HTTP :11438, 承载 /rpc plugins/* 集成面板)
    local http_ver
    http_ver=$(curl -sf http://127.0.0.1:11438/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','?'))" 2>/dev/null || echo "")
    if [[ -n "${http_ver}" && "${http_ver}" != "?" ]]; then
        printf "${GREEN}✓${NC} HTTP channel: 11438/health (v%s, /rpc /mcp /sse)\n" "${http_ver}"
    else
        printf "${YELLOW}!${NC} HTTP channel: 11438/health not reachable (desk rpc 需 [web] 依赖)\n"
    fi

    # Check plugins runtime (fusion-plugins-ecosystem, /rpc plugins.* 前置依赖)
    if "${VENV}/bin/python" -c "import fusion_plugins_ecosystem" 2>/dev/null; then
        local pver
        pver=$("${VENV}/bin/python" -c "import fusion_plugins_ecosystem as m; print(getattr(m,'__version__','?'))" 2>/dev/null || echo "?")
        printf "${GREEN}✓${NC} Plugins runtime: fusion-plugins-ecosystem %s\n" "${pver}"
    else
        printf "${RED}✗${NC} Plugins runtime: fusion-plugins-ecosystem 未安装 (/rpc plugins.* 将 -32603; pip install -e ../fusion-plugins-ecosystem)\n"
    fi

    # Check disk
    local log_size
    log_size=$(du -sh "${LOG_DIR}" 2>/dev/null | awk '{print $1}' || echo "0B")
    printf "  Logs: %s\n" "${log_size}"

    echo ""
}

# ── Usage ───────────────────────────────────────────────────────────
usage() {
    cat <<'EOF'
start.sh — fusion-cowork lifecycle manager

Usage: start.sh <command> [args]

Commands:
  start      Start fusion-cowork desk RPC daemon
  stop       Graceful stop (SIGTERM → SIGKILL fallback)
  restart    Stop + start
  status     Show PID, socket, memory, uptime
  log [N|-f] Tail server log (default 50 lines, -f to follow)
  clean      Rotate old logs, clear __pycache__, remove stale PID/socket
  doctor     Health check (venv, CLI, socket, upstream services)
  help       Show this help

Environment:
  SOCK_FILE  Unix Domain Socket path (default: /tmp/fusion-cowork.sock)
EOF
}

# ── Main ────────────────────────────────────────────────────────────
cmd="${1:-help}"
shift || true

case "${cmd}" in
    start)             do_start   ;;
    stop)              do_stop    ;;
    restart)           do_restart ;;
    status)            show_status ;;
    log)               show_log "${1:-}" ;;
    clean)             do_clean   ;;
    doctor)            do_doctor  ;;
    help|-h|--help)    usage ;;
    *)
        log_error "Unknown command: ${cmd}"
        usage
        exit 1
        ;;
esac
