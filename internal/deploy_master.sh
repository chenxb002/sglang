#!/usr/bin/env bash
# ============================================================================
# SGLang MLU CI — 内网 Master 自动部署脚本
#
# 用法:
#   ./deploy_master.sh                  # 全量部署
#   ./deploy_master.sh --restart        # 重启 master
#   ./deploy_master.sh --stop           # 停止 master
#   ./deploy_master.sh --status         # 查看状态
#   ./deploy_master.sh --once           # 单次运行（调试用）
#   ./deploy_master.sh --help           # 查看帮助
#
# 必需环境变量:
#   SGLANG_JENKINS_USER     Jenkins 用户名
#   SGLANG_JENKINS_TOKEN    Jenkins API Token
#
# 可选环境变量:
#   SLAVE_HOST              VPS slave 地址，默认 8.222.226.16
#   SLAVE_PORT              VPS slave 端口，默认 14548
#   SGLANG_JENKINS_PATH     Jenkins job 路径
#   POLL_INTERVAL           轮询间隔（秒），默认 10
# ============================================================================

set -euo pipefail

SLAVE_HOST="${SLAVE_HOST:-8.222.226.16}"
SLAVE_PORT="${SLAVE_PORT:-14548}"
SLAVE_URL="http://${SLAVE_HOST}:${SLAVE_PORT}"
POLL_INTERVAL="${POLL_INTERVAL:-10}"
JENKINS_PATH="${SGLANG_JENKINS_PATH:-jenkins.svc.cambricon.com/dist/job/SGLANG/job/DEBUG/job/sglang_ci/}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="${SGLANG_CI_INTERNAL_DEPLOY_DIR:-${SCRIPT_DIR}/deploy}"
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
SGLang MLU CI — 内网 Master 自动部署脚本

用法:
  ./deploy_master.sh [选项]

选项:
  deploy, 无参数      全量部署：检查依赖、复制运行文件、生成配置并启动 master
  --restart           重新复制运行文件并重启 master
  --stop              停止 master
  --status            查看 master、VPS slave 和 Jenkins 连接状态
  --once              单次运行 master，适合调试
  -h, --help          显示本帮助

必需环境变量:
  SGLANG_JENKINS_USER   Jenkins 用户名
  SGLANG_JENKINS_TOKEN  Jenkins API Token

常用环境变量:
  SLAVE_HOST            VPS slave 地址，默认: ${SLAVE_HOST}
  SLAVE_PORT            VPS slave 端口，默认: ${SLAVE_PORT}
  SGLANG_JENKINS_PATH   Jenkins job 路径，默认: ${JENKINS_PATH}
  POLL_INTERVAL         master 轮询间隔秒数，默认: ${POLL_INTERVAL}
  SGLANG_CI_INTERNAL_DEPLOY_DIR
                        内网部署目录，默认: ${DEPLOY_DIR}

示例:
  export SGLANG_JENKINS_USER='<jenkins-user>'
  export SGLANG_JENKINS_TOKEN='<jenkins-api-token>'
  ./deploy_master.sh --status
  ./deploy_master.sh --restart
EOF
}

check_prerequisites() {
    step "1/5  前置检查"
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
        error "python3 requests 未安装，请执行: pip3 install requests"
        missing=1
    fi

    if [[ -z "${SGLANG_JENKINS_USER:-}" ]]; then
        error "SGLANG_JENKINS_USER 未设置"
        missing=1
    fi
    if [[ -z "${SGLANG_JENKINS_TOKEN:-}" ]]; then
        error "SGLANG_JENKINS_TOKEN 未设置"
        missing=1
    fi

    if [[ "${missing}" -eq 1 ]]; then
        echo ""
        echo "示例："
        echo "  export SGLANG_JENKINS_USER='<username>'"
        echo "  export SGLANG_JENKINS_TOKEN='<api-token>'"
        echo "  ./deploy_master.sh"
        exit 1
    fi

    info "检查 VPS slave 连通性: ${SLAVE_URL}"
    if curl -s --max-time 5 "${SLAVE_URL}/source=master&aiming=get_data" >/dev/null 2>&1; then
        info "VPS slave 连通"
    else
        warn "VPS slave 无法连通，请确认 ${SLAVE_HOST}:${SLAVE_PORT} 已开放给内网 master"
    fi

    info "检查 Jenkins job API"
    local jenkins_url="http://${JENKINS_PATH}"
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
        -u "${SGLANG_JENKINS_USER}:${SGLANG_JENKINS_TOKEN}" \
        "${jenkins_url}api/json" 2>/dev/null || echo fail)
    if [[ "${http_code}" == "200" ]]; then
        info "Jenkins 连通"
    else
        warn "Jenkins 返回 HTTP ${http_code}，请确认 job 路径、user/token 与 Read/Build 权限"
    fi
}

prepare_deploy_dir() {
    step "2/5  准备部署目录"

    if [[ ! -f "${SCRIPT_DIR}/master.py" || ! -f "${SCRIPT_DIR}/common.py" ]]; then
        error "未找到 internal 运行文件，请在脚本仓的 internal 目录中运行"
        exit 1
    fi

    mkdir -p "${DEPLOY_DIR}" "${LOG_DIR}"
    cp -f "${SCRIPT_DIR}/master.py" "${SCRIPT_DIR}/common.py" "${DEPLOY_DIR}/"
    info "运行文件已复制到 ${DEPLOY_DIR}"
}

generate_config() {
    step "3/5  生成配置文件"

    local conf="${DEPLOY_DIR}/internal_master.conf"
    cat > "${conf}" <<EOF_CONF
[SlaveServer]
# VPS slave 地址；master 主动轮询该地址获取任务
host="${SLAVE_HOST}"
port="${SLAVE_PORT}"

[MasterServer]
# Jenkins job 路径（不含 build 编号）
jenkins_path="${JENKINS_PATH}"
# 凭据通过环境变量注入，不写入配置文件
jenkins_user=""
jenkins_token=""

# 传给 Jenkins 的参数列表，必须与 Jenkins job 参数名一致
jenkins_params="repo;timestamp;pr_id;task_id;trigger_type;repo_url;git_ref;commit_sha"
EOF_CONF

    info "配置文件已生成: ${conf}"
}

start_master() {
    step "4/5  启动 Master"

    local conf="${DEPLOY_DIR}/internal_master.conf"
    pkill -f "${DEPLOY_DIR}/master.py" 2>/dev/null || true
    sleep 1

    nohup setsid python3 -u "${DEPLOY_DIR}/master.py" "${conf}" \
        --interval "${POLL_INTERVAL}" \
        >"${LOG_DIR}/master.log" 2>&1 &

    local pid=$!
    info "master.py 已启动: pid=${pid}"
    info "日志: tail -f ${LOG_DIR}/master.log"
    sleep 2

    if kill -0 "${pid}" 2>/dev/null; then
        info "进程存活"
    else
        error "master.py 已退出，最近日志如下："
        tail -20 "${LOG_DIR}/master.log" 2>/dev/null || true
        return 1
    fi
}

stop_master() {
    step "停止 Master"
    pkill -f "${DEPLOY_DIR}/master.py" 2>/dev/null || true
    info "master.py 已停止（如有运行）"
}

run_once() {
    step "单次运行 Master"
    python3 -u "${DEPLOY_DIR}/master.py" "${DEPLOY_DIR}/internal_master.conf" --once --interval "${POLL_INTERVAL}"
}

show_status() {
    echo ""
    echo "══════════════════════════════════════════════════════════════════"
    echo "  SGLang MLU CI — 内网 Master 状态"
    echo "══════════════════════════════════════════════════════════════════"
    echo ""

    local pid
    pid=$(pgrep -f "${DEPLOY_DIR}/master.py" 2>/dev/null | head -1 || true)
    if [[ -n "${pid}" ]]; then
        echo -e "  ${GREEN}●${NC} master.py  pid=${pid}"
    else
        echo -e "  ${RED}○${NC} master.py  未运行"
    fi

    echo ""
    echo "── 连接状态 ──"
    local slave_resp
    slave_resp=$(curl -s --max-time 5 "${SLAVE_URL}/source=master&aiming=get_data" 2>/dev/null || echo "")
    if [[ "${slave_resp}" == *"tasks"* ]]; then
        local count
        count=$(echo "${slave_resp}" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('tasks', [])))" 2>/dev/null || echo '?')
        echo -e "  ${GREEN}●${NC} VPS slave  → ${SLAVE_URL} (${count} active tasks)"
    else
        echo -e "  ${RED}○${NC} VPS slave  → ${SLAVE_URL} 无法连接"
    fi

    local jenkins_url="http://${JENKINS_PATH}"
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
        -u "${SGLANG_JENKINS_USER:-}:${SGLANG_JENKINS_TOKEN:-}" \
        "${jenkins_url}api/json" 2>/dev/null || echo fail)
    if [[ "${http_code}" == "200" ]]; then
        echo -e "  ${GREEN}●${NC} Jenkins    → ${jenkins_url}"
    else
        echo -e "  ${RED}○${NC} Jenkins    → ${jenkins_url} (HTTP ${http_code})"
    fi

    echo ""
    echo "── 配置 ──"
    echo "  Jenkins User:  ${SGLANG_JENKINS_USER:-<未设置>}"
    echo "  Slave:         ${SLAVE_URL}"
    echo "  Poll Interval: ${POLL_INTERVAL}s"
    echo "  Deploy Dir:    ${DEPLOY_DIR}"
    echo "  Log:           ${LOG_DIR}/master.log"
    echo ""

    if [[ -f "${LOG_DIR}/master.log" ]]; then
        echo "── 最近日志 (最后 10 行) ──"
        tail -10 "${LOG_DIR}/master.log" | sed 's/^/  /'
        echo ""
    fi
}

main() {
    local mode="${1:-deploy}"
    case "${mode}" in
        -h|--help|help)
            show_help
            ;;
        --stop)
            stop_master
            ;;
        --status)
            show_status
            ;;
        --restart)
            prepare_deploy_dir
            generate_config
            stop_master
            sleep 1
            start_master
            show_status
            ;;
        --once)
            prepare_deploy_dir
            generate_config
            run_once
            ;;
        deploy)
            echo ""
            echo "╔══════════════════════════════════════════════════════════════╗"
            echo "║  SGLang MLU CI — 内网 Master 部署                           ║"
            echo "║  slave: ${SLAVE_HOST}:${SLAVE_PORT}  jenkins: ${JENKINS_PATH} ║"
            echo "╚══════════════════════════════════════════════════════════════╝"
            echo ""
            check_prerequisites
            prepare_deploy_dir
            generate_config
            start_master
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
