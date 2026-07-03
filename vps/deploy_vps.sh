#!/usr/bin/env bash
# ============================================================================
# SGLang MLU CI — VPS 自动部署脚本（无需 sudo）
#
# 用法:
#   ./deploy_vps.sh                    # 全量部署
#   ./deploy_vps.sh --restart          # 重启 bridge/slave
#   ./deploy_vps.sh --stop             # 停止 bridge/slave/runner
#   ./deploy_vps.sh --status           # 查看状态
#   ./deploy_vps.sh --runner-only      # 仅安装/启动 GitHub runner
#   ./deploy_vps.sh --restart-runner   # 重启 GitHub runner
#   ./deploy_vps.sh --help             # 查看帮助
#
# 环境变量:
#   GITHUB_RUNNER_TOKEN   GitHub runner 注册 token（首次安装 runner 必需）
#   GITHUB_RUNNER_REPO_URL GitHub runner 注册目标仓库 URL
#   BRIDGE_PORT           bridge 端口，默认 14547
#   SLAVE_PORT            slave 端口，默认 14548
# ============================================================================

set -euo pipefail

GITHUB_RUNNER_REPO_URL="${GITHUB_RUNNER_REPO_URL:-}"
BRIDGE_PORT="${BRIDGE_PORT:-14547}"
SLAVE_PORT="${SLAVE_PORT:-14548}"
ALLOWED_REPO="${ALLOWED_REPO:-sglang}"
GITHUB_RUNNER_TOKEN="${GITHUB_RUNNER_TOKEN:-}"
RUNNER_LABEL="${RUNNER_LABEL:-cambricon}"
RUNNER_NAME="${RUNNER_NAME:-cambricon-runner-$(hostname -s 2>/dev/null || echo vps)}"

USER_HOME="${HOME}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${SGLANG_CI_VPS_SOURCE_DIR:-${SCRIPT_DIR}}"
DEPLOY_DIR="${SGLANG_CI_DEPLOY_DIR:-${USER_HOME}/data/mlu_ci/deploy}"
DATA_DIR="${SGLANG_CI_DATA_DIR:-${USER_HOME}/data/mlu_ci}"
RUNNER_DIR="${GITHUB_RUNNER_DIR:-${USER_HOME}/actions-runner}"
LOG_DIR="${DEPLOY_DIR}/logs"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
step()  { echo -e "\n${BLUE}━━━ $* ━━━${NC}"; }

show_help() {
    cat <<EOF
SGLang MLU CI — VPS 自动部署脚本（无需 sudo）

用法:
  ./deploy_vps.sh [选项]

选项:
  deploy, 无参数      全量部署：复制运行文件、生成配置、启动 bridge/slave、启动 runner
  --restart           重新复制运行文件并重启 bridge/slave，不处理 GitHub runner
  --stop              停止 bridge/slave，并尝试停止 GitHub runner
  --status            查看 VPS 侧服务、端口和日志路径
  --runner-only       仅安装/启动 GitHub self-hosted runner
  --restart-runner    重启 GitHub self-hosted runner
  -h, --help          显示本帮助

常用环境变量:
  GITHUB_RUNNER_REPO_URL
                      GitHub runner 注册目标仓库 URL；未设置时尝试从当前 git remote 推断
  BRIDGE_PORT         bridge 端口，默认: ${BRIDGE_PORT}
  SLAVE_PORT          slave 端口，默认: ${SLAVE_PORT}
  ALLOWED_REPO        bridge 接受的 repo 名，默认: ${ALLOWED_REPO}；设为空表示不限制
  RUNNER_LABEL        GitHub runner label，默认: ${RUNNER_LABEL}
  RUNNER_NAME         GitHub runner name，默认: ${RUNNER_NAME}
  GITHUB_RUNNER_TOKEN 首次注册 runner 时需要的 GitHub token
  SGLANG_CI_VPS_SOURCE_DIR
                      bridge/slave 源文件目录，默认: ${SOURCE_DIR}
  SGLANG_CI_DEPLOY_DIR
                      bridge/slave 部署目录，默认: ${DEPLOY_DIR}
  SGLANG_CI_DATA_DIR  任务 DB 目录，默认: ${DATA_DIR}
  GITHUB_RUNNER_DIR   GitHub runner 安装目录，默认: ${RUNNER_DIR}

运行路径:
  Source dir:   ${SOURCE_DIR}
  Deploy dir:   ${DEPLOY_DIR}
  Data dir:     ${DATA_DIR}
  Runner dir:   ${RUNNER_DIR}

示例:
  ./deploy_vps.sh --status
  ./deploy_vps.sh --restart
  GITHUB_RUNNER_TOKEN='<token>' ./deploy_vps.sh --runner-only
EOF
}

check_prerequisites() {
    step "1/5  检查前置依赖"
    local missing=0

    for cmd in python3 curl; do
        if command -v "${cmd}" >/dev/null 2>&1; then
            info "${cmd}: $(command -v "${cmd}")"
        else
            error "${cmd}: 未安装"
            missing=1
        fi
    done

    if python3 -c "import requests" 2>/dev/null; then
        info "python3 requests: OK"
    else
        warn "python3 requests 未安装，尝试 pip install --user requests"
        pip3 install --user requests 2>/dev/null || {
            error "无法安装 requests，请手动执行: pip3 install --user requests"
            missing=1
        }
    fi

    if [[ "${missing}" -eq 1 ]]; then
        error "缺少必要依赖，请联系 VPS 管理员安装"
        exit 1
    fi

    for port in "${BRIDGE_PORT}" "${SLAVE_PORT}"; do
        if ss -tlnp 2>/dev/null | grep -q ":${port} " || netstat -tlnp 2>/dev/null | grep -q ":${port} "; then
            warn "端口 ${port} 已被占用；如果是旧服务，后续 restart 会尝试停止"
        else
            info "端口 ${port}: 空闲"
        fi
    done
}

infer_github_runner_repo_url() {
    if [[ -n "${GITHUB_RUNNER_REPO_URL}" ]]; then
        echo "${GITHUB_RUNNER_REPO_URL%.git}"
        return 0
    fi

    if command -v git >/dev/null 2>&1 && git -C "${SOURCE_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        local remote_url
        remote_url="$(git -C "${SOURCE_DIR}" remote get-url github 2>/dev/null || \
                      git -C "${SOURCE_DIR}" remote get-url origin 2>/dev/null || \
                      git -C "${SOURCE_DIR}" remote get-url "$(git -C "${SOURCE_DIR}" remote | head -1)" 2>/dev/null || true)"
        if [[ -n "${remote_url}" ]]; then
            case "${remote_url}" in
                git@github.com:*)
                    remote_url="https://github.com/${remote_url#git@github.com:}"
                    ;;
            esac
            if [[ "${remote_url}" == https://github.com/* ]]; then
                echo "${remote_url%.git}"
                return 0
            fi
        fi
    fi

    return 1
}

prepare_deploy_dir() {
    step "2/5  准备部署目录"

    if [[ ! -f "${SOURCE_DIR}/bridge.py" || ! -f "${SOURCE_DIR}/slave.py" || ! -f "${SOURCE_DIR}/common.py" ]]; then
        error "未找到 VPS 运行文件，请在包含 bridge.py/slave.py/common.py 的目录执行该脚本"
        error "当前源目录: ${SOURCE_DIR}"
        exit 1
    fi

    mkdir -p "${DEPLOY_DIR}" "${DATA_DIR}" "${LOG_DIR}"
    cp -f "${SOURCE_DIR}/common.py" "${SOURCE_DIR}/bridge.py" "${SOURCE_DIR}/slave.py" "${DEPLOY_DIR}/"
    cp -f "${SOURCE_DIR}/run_external_services.sh" "${SOURCE_DIR}/stop_external_services.sh" "${SOURCE_DIR}/run_external_foreground.sh" "${DEPLOY_DIR}/" 2>/dev/null || true
    chmod +x "${DEPLOY_DIR}"/*.sh 2>/dev/null || true

    info "运行文件已从 ${SOURCE_DIR} 复制到 ${DEPLOY_DIR}"
}

generate_config() {
    step "3/5  生成配置文件"

    local conf="${DEPLOY_DIR}/external_ci.conf"
    if [[ -f "${conf}" ]]; then
        info "配置文件已存在，跳过生成: ${conf}"
        info "如需重新生成，请删除该文件后重跑部署脚本"
        return
    fi

    cat > "${conf}" <<EOF_CONF
[BridgeServer]
# bridge 只给 VPS 本机 GitHub runner 访问，不直接暴露公网
host="127.0.0.1"
port="${BRIDGE_PORT}"
repo="${ALLOWED_REPO}"

[SlaveServer]
# bridge 通过 localhost 转发任务给 slave
host="127.0.0.1"
# slave 需要被内网 master 访问，因此监听 0.0.0.0；请在云安全组限制来源 IP
bind_host="0.0.0.0"
port="${SLAVE_PORT}"
db_path="${DATA_DIR}/sglang_tasks.json"
log_dir="${DATA_DIR}/logs"

terminal_retention_seconds="86400"
active_task_timeout_seconds="172800"
EOF_CONF

    info "配置文件已生成: ${conf}"
}


service_pid_file() {
    echo "${DEPLOY_DIR}/$1.pid"
}

stop_python_service() {
    local name="$1"
    local pid_file
    pid_file="$(service_pid_file "${name}")"

    if [[ -f "${pid_file}" ]]; then
        local pid
        pid="$(cat "${pid_file}" 2>/dev/null || true)"
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null || true
        fi
        rm -f "${pid_file}"
    fi

    # Stop both the current deploy path and older source-path/nohup launches.
    pkill -f "python3 .*${DEPLOY_DIR}/${name}\.py" 2>/dev/null || true
    pkill -f "python3 .*${SOURCE_DIR}/${name}\.py" 2>/dev/null || true
    pkill -f "python3 .*${name}\.py .*external_ci\.conf" 2>/dev/null || true
    pkill -f "python3 .*${name}\.py .*sglang_ci\.conf" 2>/dev/null || true
}

find_service_pid() {
    local name="$1"
    local pid_file
    pid_file="$(service_pid_file "${name}")"

    if [[ -f "${pid_file}" ]]; then
        local pid
        pid="$(cat "${pid_file}" 2>/dev/null || true)"
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            echo "${pid}"
            return 0
        fi
    fi

    pgrep -f "python3 .*${DEPLOY_DIR}/${name}\.py" 2>/dev/null | head -1 || true
}

start_external_services() {
    step "4/5  启动 bridge/slave"

    local conf="${DEPLOY_DIR}/external_ci.conf"
    local slave_log="${LOG_DIR}/slave.log"
    local bridge_log="${LOG_DIR}/bridge.log"

    stop_python_service bridge
    stop_python_service slave
    sleep 1

    nohup setsid python3 -u "${DEPLOY_DIR}/slave.py" "${conf}" >"${slave_log}" 2>&1 &
    local slave_pid=$!
    echo "${slave_pid}" >"$(service_pid_file slave)"
    info "slave.py 已启动: pid=${slave_pid}"
    sleep 1

    nohup setsid python3 -u "${DEPLOY_DIR}/bridge.py" "${conf}" >"${bridge_log}" 2>&1 &
    local bridge_pid=$!
    echo "${bridge_pid}" >"$(service_pid_file bridge)"
    info "bridge.py 已启动: pid=${bridge_pid}"
    sleep 1

    if curl -s --max-time 5 "http://127.0.0.1:${SLAVE_PORT}/source=master&aiming=get_data" | grep -q 'tasks'; then
        info "slave :${SLAVE_PORT} 响应正常"
    else
        warn "slave 可能未正常启动，检查日志: tail -f ${slave_log}"
    fi

    local bridge_code
    bridge_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:${BRIDGE_PORT}/" 2>/dev/null || echo fail)
    if [[ "${bridge_code}" =~ ^(200|400|404)$ ]]; then
        info "bridge :${BRIDGE_PORT} 响应正常（HTTP ${bridge_code}）"
    else
        warn "bridge 可能未正常启动，检查日志: tail -f ${bridge_log}"
    fi
}


find_runner_pid() {
    local pid cwd args
    for proc in /proc/[0-9]*; do
        pid="${proc#/proc/}"
        args="$(tr '\0' ' ' <"${proc}/cmdline" 2>/dev/null || true)"
        [[ "${args}" == *Runner.Listener* ]] || continue
        cwd="$(readlink "${proc}/cwd" 2>/dev/null || true)"
        if [[ "${cwd}" == "${RUNNER_DIR}" || "${args}" == *"${RUNNER_DIR}"* ]]; then
            echo "${pid}"
            return 0
        fi
    done
    return 1
}

stop_github_runner() {
    local pid
    if pid="$(find_runner_pid)"; then
        kill "${pid}" 2>/dev/null || true
        sleep 2
        if kill -0 "${pid}" 2>/dev/null; then
            kill -9 "${pid}" 2>/dev/null || true
        fi
    fi
}

setup_github_runner() {
    step "5/5  GitHub Self-hosted Runner"

    local repo_url
    if ! repo_url="$(infer_github_runner_repo_url)"; then
        repo_url=""
    fi

    if [[ -x "${RUNNER_DIR}/run.sh" ]]; then
        info "Runner 已安装: ${RUNNER_DIR}"
        if [[ -f "${RUNNER_DIR}/.runner" ]]; then
            info "Runner 已注册"
        else
            warn "Runner 未配置，请进入 ${RUNNER_DIR} 运行 config.sh"
        fi
        local runner_pid
        if runner_pid="$(find_runner_pid)"; then
            info "Runner 进程已运行: pid=${runner_pid}"
        else
            cd "${RUNNER_DIR}"
            nohup ./run.sh >"${RUNNER_DIR}/runner.log" 2>&1 &
            info "Runner 已启动: pid=$!"
        fi
        return
    fi

    if [[ -z "${GITHUB_RUNNER_TOKEN}" ]]; then
        warn "未设置 GITHUB_RUNNER_TOKEN，跳过 runner 首次安装"
        echo ""
        echo "获取 token 后执行："
        echo "  GITHUB_RUNNER_TOKEN='<token>' ./deploy_vps.sh --runner-only"
        if [[ -n "${repo_url}" ]]; then
            echo "GitHub 页面：${repo_url}/settings/actions/runners"
        else
            echo "请设置 GITHUB_RUNNER_REPO_URL，例如：https://github.com/<owner>/<repo>"
        fi
        echo ""
        return
    fi

    if [[ -z "${repo_url}" ]]; then
        error "无法推断 GitHub runner 注册目标仓库，请设置 GITHUB_RUNNER_REPO_URL"
        return 1
    fi

    mkdir -p "${RUNNER_DIR}"
    cd "${RUNNER_DIR}"

    local arch runner_version tar_file download_url
    arch=$(uname -m)
    case "${arch}" in
        x86_64) arch="x64" ;;
        aarch64) arch="arm64" ;;
        *) error "不支持的架构: ${arch}"; return ;;
    esac

    runner_version="2.334.0"
    tar_file="actions-runner-linux-${arch}-${runner_version}.tar.gz"
    download_url="https://github.com/actions/runner/releases/download/v${runner_version}/${tar_file}"

    info "下载 Runner: ${download_url}"
    curl -L -o "${tar_file}" "${download_url}"
    tar xzf "${tar_file}"
    rm -f "${tar_file}"

    ./config.sh \
        --url "${repo_url}" \
        --token "${GITHUB_RUNNER_TOKEN}" \
        --name "${RUNNER_NAME}" \
        --labels "${RUNNER_LABEL}" \
        --work "_work" \
        --unattended \
        --replace

    nohup ./run.sh >"${RUNNER_DIR}/runner.log" 2>&1 &
    info "Runner 已启动: pid=$!"
}

stop_services() {
    step "停止 VPS 服务"
    stop_python_service bridge
    stop_python_service slave
    stop_github_runner
    info "bridge/slave/runner 已停止（如有运行）"
}

show_status() {
    echo ""
    echo "══════════════════════════════════════════════════════════════════"
    echo "  SGLang MLU CI — VPS 状态"
    echo "══════════════════════════════════════════════════════════════════"
    echo ""

    echo "── 进程 ──"
    for name in bridge slave; do
        local pid
        pid="$(find_service_pid "${name}")"
        if [[ -n "${pid}" ]]; then
            echo -e "  ${GREEN}●${NC} ${name}.py  pid=${pid}"
        else
            echo -e "  ${RED}○${NC} ${name}.py  未运行"
        fi
    done
    local runner_pid
    if runner_pid="$(find_runner_pid)"; then
        echo -e "  ${GREEN}●${NC} GitHub runner  pid=${runner_pid}"
    else
        echo -e "  ${RED}○${NC} GitHub runner  未运行"
    fi

    echo ""
    echo "── 健康检查 ──"
    echo "  bridge: http://127.0.0.1:${BRIDGE_PORT}"
    local slave_resp
    slave_resp=$(curl -s --max-time 5 "http://127.0.0.1:${SLAVE_PORT}/source=master&aiming=get_data" 2>/dev/null || echo "")
    if [[ "${slave_resp}" == *"tasks"* ]]; then
        local count
        count=$(echo "${slave_resp}" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('tasks', [])))" 2>/dev/null || echo '?')
        echo -e "  slave:  http://127.0.0.1:${SLAVE_PORT} ${GREEN}OK${NC} (${count} active tasks)"
    else
        echo -e "  slave:  http://127.0.0.1:${SLAVE_PORT} ${RED}FAIL${NC}"
    fi

    echo ""
    echo "── 路径 ──"
    echo "  deploy: ${DEPLOY_DIR}"
    echo "  data:   ${DATA_DIR}/sglang_tasks.json"
    echo "  logs:   ${LOG_DIR}"
    echo "  runner: ${RUNNER_DIR}"
    echo ""
}

main() {
    local mode="${1:-deploy}"
    case "${mode}" in
        -h|--help|help)
            show_help
            ;;
        --stop)
            stop_services
            ;;
        --status)
            show_status
            ;;
        --restart)
            prepare_deploy_dir
            generate_config
            start_external_services
            show_status
            ;;
        --runner-only)
            setup_github_runner
            show_status
            ;;
        --restart-runner)
            stop_github_runner
            setup_github_runner
            show_status
            ;;
        deploy)
            echo ""
            echo "╔══════════════════════════════════════════════════════════════╗"
            echo "║  SGLang MLU CI — VPS 自动部署                               ║"
            echo "║  bridge: 127.0.0.1:${BRIDGE_PORT}  slave: 0.0.0.0:${SLAVE_PORT}              ║"
            echo "╚══════════════════════════════════════════════════════════════╝"
            echo ""
            check_prerequisites
            prepare_deploy_dir
            generate_config
            start_external_services
            setup_github_runner
            show_status
            ;;
        *)
            error "未知参数: ${mode}"
            echo ""
            show_help
            exit 2
            ;;
    esac
}

main "$@"
