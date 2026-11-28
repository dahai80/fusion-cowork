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
# issue #644: pid/lock paths overridable so lifecycle tests can isolate in tmp dirs
PID_FILE="${FUSION_PID_FILE:-${PROJ_DIR}/.fusion-cowork.pid}"
WEB_PID_FILE="${FUSION_WEB_PID_FILE:-${PROJ_DIR}/.fusion-cowork-web.pid}"
START_LOCK="${FUSION_START_LOCK:-${PROJ_DIR}/.start.lock}"
SOCK_FILE="/tmp/fusion-cowork.sock"
# issue #644: operator web plane (fusion-cowork web serve) lifecycle-managed
WEB_PORT="${FUSION_WEB_PORT:-11461}"
# default ON: start.sh now manages the web plane like desk-rpc; FUSION_COWORK_START_WEB=0 opts out
START_WEB="${FUSION_COWORK_START_WEB:-1}"
WEB_KEY_FILE="${FUSION_WEB_KEY_FILE:-${PROJ_DIR}/.fusion-cowork.web.key}"
# issue #189: ready-set 波次并发 — 服务进程默认 4 路并行 (代码默认 1 = 旧串行),
# 运维可用同名环境变量覆盖, 设为 1 即完全退回串行调度。
export FUSION_COWORK_MAX_CONCURRENCY="${FUSION_COWORK_MAX_CONCURRENCY:-4}"

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
# issue #644: a dead or pid-reused pid file is reported and cleaned instead
# of being trusted (status previously printed "Running" for a gone process).
is_running() {
    if [[ ! -f "${PID_FILE}" ]]; then
        return 1
    fi
    local pid
    pid=$(cat "${PID_FILE}" 2>/dev/null || echo "")
    if [[ -z "${pid}" ]]; then
        log_warn "Stale PID file (empty) — removing ${PID_FILE}"
        rm -f "${PID_FILE}"
        return 1
    fi
    if ! kill -0 "${pid}" 2>/dev/null; then
        log_warn "Stale PID file (pid ${pid} not running) — removing ${PID_FILE}"
        rm -f "${PID_FILE}"
        return 1
    fi
    # pid alive but reused by an unrelated process — treat as stale
    local cmd
    cmd=$(ps -p "${pid}" -o command= 2>/dev/null || echo "")
    if [[ "${cmd}" != *fusion_cowork* && "${cmd}" != *fusion-cowork* ]]; then
        log_warn "Stale PID file (pid ${pid} reused by unrelated process: ${cmd:-unknown}) — removing ${PID_FILE}"
        rm -f "${PID_FILE}"
        return 1
    fi
    return 0
}

get_pid() {
    if [[ -f "${PID_FILE}" ]]; then
        cat "${PID_FILE}" 2>/dev/null
    fi
}

# web plane equivalent (issue #644): same staleness rules, different pid file
web_running() {
    if [[ ! -f "${WEB_PID_FILE}" ]]; then
        return 1
    fi
    local pid
    pid=$(cat "${WEB_PID_FILE}" 2>/dev/null || echo "")
    if [[ -z "${pid}" ]] || ! kill -0 "${pid}" 2>/dev/null; then
        rm -f "${WEB_PID_FILE}"
        return 1
    fi
    local cmd
    cmd=$(ps -p "${pid}" -o command= 2>/dev/null || echo "")
    if [[ "${cmd}" != *fusion_cowork* && "${cmd}" != *fusion-cowork* ]]; then
        log_warn "Stale web PID file (pid ${pid} reused: ${cmd:-unknown}) — removing ${WEB_PID_FILE}"
        rm -f "${WEB_PID_FILE}"
        return 1
    fi
    return 0
}

get_web_pid() {
    if [[ -f "${WEB_PID_FILE}" ]]; then
        cat "${WEB_PID_FILE}" 2>/dev/null
    fi
}

port_in_use() {
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

# ── Single-instance start guard (issue #644) ────────────────────────
# Atomic mkdir lock: two near-simultaneous starts raced before, the loser
# half-booted and died on Errno 48 after registering scheduler tasks.
acquire_start_lock() {
    if mkdir "${START_LOCK}" 2>/dev/null; then
        echo "$$" > "${START_LOCK}/owner"
        return 0
    fi
    # stale-lock recovery: recorded holder is gone and the lock is older than 60s
    local owner="" mtime=0 age=0
    owner=$(cat "${START_LOCK}/owner" 2>/dev/null || echo "")
    mtime=$(stat -f %m "${START_LOCK}" 2>/dev/null || stat -c %Y "${START_LOCK}" 2>/dev/null || echo 0)
    age=$(( $(date +%s) - mtime ))
    if [[ -n "${owner}" ]] && ! kill -0 "${owner}" 2>/dev/null && (( age > 60 )); then
        log_warn "Removing stale start lock (owner pid ${owner} gone, ${age}s old)"
        rm -rf "${START_LOCK}"
        if mkdir "${START_LOCK}" 2>/dev/null; then
            echo "$$" > "${START_LOCK}/owner"
            return 0
        fi
    fi
    log_error "Another start.sh invocation holds the start lock (${START_LOCK}, owner pid ${owner:-unknown}) — refusing concurrent start"
    return 1
}

release_start_lock() {
    if [[ -f "${START_LOCK}/owner" ]] && [[ "$(cat "${START_LOCK}/owner" 2>/dev/null)" == "$$" ]]; then
        rm -rf "${START_LOCK}" 2>/dev/null || true
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

# ── Port-registry self-check (issue #219, config Phase 3.3) ─────────
# 启动前核对自身 port-registry.yaml 登记: 未登记端口在 start 即失败,
# 而不是事后被 CI check-ports 抓到。缺登记文件 = warn 放行 (独立部署);
# FUSION_SKIP_REGISTRY_CHECK=1 可显式跳过。
registry_selfcheck() {
    local service="${1:-fusion-cowork}"
    local port="${2:-${FUSION_PORT:-11438}}"
    local registry="${FUSION_REGISTRY:-$HOME/fusion/architecture/port-registry.yaml}"
    if [[ ! -f "${registry}" ]]; then
        log_warn "port-registry.yaml not found at ${registry} — registry self-check skipped"
        return 0
    fi
    if grep -A3 "^  ${service}:" "${registry}" 2>/dev/null | grep -q "port: ${port}$"; then
        log_info "Registry self-check OK (${service} port ${port} registered)"
        return 0
    fi
    if [[ "${FUSION_SKIP_REGISTRY_CHECK:-}" == "1" ]]; then
        log_warn "registry check bypassed (FUSION_SKIP_REGISTRY_CHECK=1)"
        return 0
    fi
    log_error "port ${port} for ${service} not registered in ${registry} — refusing to start"
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

    # issue #219: 端口登记自检 — 未登记拒绝启动
    if ! registry_selfcheck; then
        return 1
    fi

    log_info "Preflight OK (socket=${SOCK_FILE})"
    return 0
}

# ── start ───────────────────────────────────────────────────────────
do_start() {
    if is_running; then
        log_warn "Server already running (PID $(get_pid)) — no-op (issue #644 single-instance)"
        wait_healthy 5
        start_web_if_enabled
        return 0
    fi

    if ! acquire_start_lock; then
        exit 1
    fi
    trap release_start_lock EXIT

    # Re-check after acquiring the lock (the racing winner may have started it)
    if is_running; then
        log_warn "Server already running (PID $(get_pid)) — no-op"
        release_start_lock
        trap - EXIT
        start_web_if_enabled
        return 0
    fi

    if ! preflight; then
        release_start_lock
        trap - EXIT
        return 0
    fi

    # Foreign process holding the HTTP port would half-boot the server and
    # die on Errno 48 after registering scheduler tasks — refuse up front
    local port="${FUSION_PORT:-11438}"
    if port_in_use "${port}"; then
        local holder
        holder=$(lsof -nP -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null | head -1 || echo "?")
        log_error "HTTP port ${port} already held by pid ${holder} — refusing to half-boot; stop that process or set FUSION_PORT"
        release_start_lock
        trap - EXIT
        return 1
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
        release_start_lock
        trap - EXIT
        start_web_if_enabled
        show_status
    else
        log_error "Start failed. Check logs: ${LOG_DIR}/stderr.log"
        tail -20 "${LOG_DIR}/stderr.log" 2>/dev/null || true
        rm -f "${PID_FILE}"
        release_start_lock
        trap - EXIT
        exit 1
    fi
}

# ── web plane (issue #644) ──────────────────────────────────────────
ensure_web_key() {
    # web serve without a key is open on localhost only; still, generate and
    # persist one so the lifecycle-managed plane is authenticated by default
    if [[ -n "${FUSION_COWORK_WEB_API_KEY:-}" ]]; then
        return 0
    fi
    if [[ -s "${WEB_KEY_FILE}" ]]; then
        FUSION_COWORK_WEB_API_KEY="$(cat "${WEB_KEY_FILE}")"
        export FUSION_COWORK_WEB_API_KEY
        return 0
    fi
    FUSION_COWORK_WEB_API_KEY="$(openssl rand -hex 24 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(24))")"
    umask 077
    printf "%s" "${FUSION_COWORK_WEB_API_KEY}" > "${WEB_KEY_FILE}"
    umask 022
    export FUSION_COWORK_WEB_API_KEY
    log_info "Generated web API key: ${WEB_KEY_FILE} (0600)"
}

start_web_if_enabled() {
    if [[ "${START_WEB}" != "1" ]]; then
        return 0
    fi
    if web_running; then
        return 0
    fi
    log_step "Starting fusion-cowork web plane (:${WEB_PORT})"
    if port_in_use "${WEB_PORT}"; then
        local holder
        holder=$(lsof -nP -tiTCP:"${WEB_PORT}" -sTCP:LISTEN 2>/dev/null | head -1 || echo "?")
        log_warn "Web port ${WEB_PORT} already held by pid ${holder} — skipping web start"
        return 1
    fi
    ensure_web_key
    if ! registry_selfcheck fusion-cowork-web "${WEB_PORT}"; then
        log_error "web plane port ${WEB_PORT} not registered — skipping web start (FUSION_SKIP_REGISTRY_CHECK=1 bypasses)"
        return 1
    fi
    local python_bin="${VENV}/bin/python"
    nohup "${python_bin}" -m fusion_cowork web serve --port "${WEB_PORT}" \
        >> "${LOG_DIR}/webserve.log" \
        2>> "${LOG_DIR}/webserve.err.log" &
    local pid=$!
    echo "${pid}" > "${WEB_PID_FILE}"
    log_info "Web API PID: ${pid}"
    local elapsed=0
    while (( elapsed < 30 )); do
        if curl -sf "http://127.0.0.1:${WEB_PORT}/health" >/dev/null 2>&1; then
            log_info "Web plane healthy (:${WEB_PORT}, took ${elapsed}s)"
            return 0
        fi
        if ! kill -0 "${pid}" 2>/dev/null; then
            log_error "Web API process died — check ${LOG_DIR}/webserve.err.log"
            rm -f "${WEB_PID_FILE}"
            return 1
        fi
        sleep 1
        (( elapsed += 1 ))
    done
    log_warn "Web plane not healthy within 30s — check ${LOG_DIR}/webserve.err.log"
    return 1
}

stop_web() {
    if ! web_running; then
        rm -f "${WEB_PID_FILE}"
        return 0
    fi
    local pid
    pid=$(get_web_pid)
    log_step "Stopping web plane (PID ${pid}) — requested by shell PID $$ (parent: $(ps -o command= -p "${PPID}" 2>/dev/null || echo unknown))"
    kill -TERM "${pid}" 2>/dev/null || true
    local waited=0
    while (( waited < 15 )); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            log_info "Web plane stopped"
            rm -f "${WEB_PID_FILE}"
            return 0
        fi
        sleep 1
        (( waited += 1 ))
    done
    log_warn "Web plane graceful shutdown timed out, force killing..."
    kill -KILL "${pid}" 2>/dev/null || true
    sleep 1
    rm -f "${WEB_PID_FILE}"
    log_info "Web plane force-stopped"
}

# ── stop ────────────────────────────────────────────────────────────
do_stop() {
    if ! is_running; then
        log_warn "Server not running"
        rm -f "${PID_FILE}" "${SOCK_FILE}"
        stop_web
        return 0
    fi

    local pid
    pid=$(get_pid)
    # issue #644: log the requester so a mystery SIGTERM can be traced
    log_step "Stopping fusion-cowork (PID ${pid}) — requested by shell PID $$ (parent: $(ps -o command= -p "${PPID}" 2>/dev/null || echo unknown))"

    # Graceful: SIGTERM
    kill -TERM "${pid}" 2>/dev/null || true
    local waited=0
    while (( waited < 15 )); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            log_info "Server stopped gracefully"
            rm -f "${PID_FILE}" "${SOCK_FILE}"
            stop_web
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
    stop_web
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

    # Web plane (issue #644)
    if web_running; then
        local web_pid
        web_pid=$(get_web_pid)
        if curl -sf "http://127.0.0.1:${WEB_PORT}/health" >/dev/null 2>&1; then
            printf "${GREEN}● Web plane${NC}  PID=%s :%s (healthy)\n" "${web_pid}" "${WEB_PORT}"
        else
            printf "${YELLOW}● Web plane${NC}  PID=%s :%s (not responding)\n" "${web_pid}" "${WEB_PORT}"
        fi
    else
        printf "${RED}● Web plane${NC}  Stopped (:%s)\n" "${WEB_PORT}"
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
    if [[ -f "${WEB_PID_FILE}" ]] && ! web_running; then
        rm -f "${WEB_PID_FILE}"
        log_info "Removed stale web PID file"
    fi
    if [[ -d "${START_LOCK}" ]] && ! kill -0 "$(cat "${START_LOCK}/owner" 2>/dev/null)" 2>/dev/null; then
        rm -rf "${START_LOCK}"
        log_info "Removed stale start lock"
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

    # Check web plane (issue #644)
    if curl -sf "http://127.0.0.1:${WEB_PORT}/health" >/dev/null 2>&1; then
        printf "${GREEN}✓${NC} Web plane: %s/health reachable (fusion-cowork-web)\n" "${WEB_PORT}"
    else
        printf "${YELLOW}!${NC} Web plane: %s/health not reachable (start.sh start 默认拉起, FUSION_COWORK_START_WEB=0 关闭)\n" "${WEB_PORT}"
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

# ── container ───────────────────────────────────────────────────────
# v0.4.0: 容器/云模式 — 前台跑 HTTP 单通道 (无 UDS), 不探 venv (容器用系统 python),
# uvicorn 原生接管 SIGTERM。Dockerfile CMD 直接跑 python 模块, 此命令供手动调试。
do_container() {
    log_step "Container mode (HTTP single channel, foreground)"
    # 容器内 PATH 已含 python (Dockerfile ENTRYPOINT); 本地手动跑用当前 python
    local python_bin
    python_bin="$(command -v python3 || command -v python)"
    if [[ -z "${python_bin}" ]]; then
        log_error "python3 not found in PATH"
        exit 1
    fi
    local bind_host="${FUSION_BIND_HOST:-127.0.0.1}"
    local port="${FUSION_PORT:-11438}"
    log_info "serve ${bind_host}:${port} (uvicorn native SIGTERM)"
    exec "${python_bin}" -m fusion_cowork serve \
        --host "${bind_host}" \
        --port "${port}"
}

# ── Usage ───────────────────────────────────────────────────────────
usage() {
    cat <<'EOF'
start.sh — fusion-cowork lifecycle manager

Usage: start.sh <command> [args]

Commands:
  start      Start fusion-cowork desk RPC daemon + web plane (:11461, default on)
  stop       Graceful stop (SIGTERM → SIGKILL fallback), desk RPC + web plane
  restart    Stop + start
  status     Show PID, socket, memory, uptime (desk RPC + web plane)
  log [N|-f] Tail server log (default 50 lines, -f to follow)
  clean      Rotate old logs, clear __pycache__, remove stale PID/socket
  doctor     Health check (venv, CLI, socket, upstream services)
  registry-check  Port-registry self-check only (issue #219; exit 1 if unregistered)
  container  容器/云模式 — 前台 HTTP 单通道 (无 UDS, 不探 venv, uvicorn 接管 SIGTERM)
  help       Show this help

Environment:
  SOCK_FILE          Unix Domain Socket path (default: /tmp/fusion-cowork.sock)
  FUSION_BIND_HOST   bind host (default: 127.0.0.1, single-machine)
  FUSION_PORT        HTTP 端口 (default: 11438; registry self-check 对此端口)
  FUSION_WEB_PORT    Web plane 端口 (default: 11461; port-registry: fusion-cowork-web)
  FUSION_COWORK_START_WEB  set to 0 to skip the web plane on start (default: 1)
  FUSION_COWORK_WEB_API_KEY  web plane auth key (generated+persisted to .fusion-cowork.web.key if unset)
  FUSION_REGISTRY    port-registry.yaml path (default: $HOME/fusion/architecture/port-registry.yaml)
  FUSION_SKIP_REGISTRY_CHECK  set to 1 to bypass the registry self-check
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
    registry-check)    registry_selfcheck ;;
    container)         do_container ;;
    help|-h|--help)    usage ;;
    *)
        log_error "Unknown command: ${cmd}"
        usage
        exit 1
        ;;
esac
