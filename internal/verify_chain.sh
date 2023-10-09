#!/usr/bin/env bash
# ============================================================================
# SGLang MLU CI — 端到端链路验证脚本
#
# 用法:
#   ./verify_chain.sh            # 完整验证
#   ./verify_chain.sh --smoke    # 轻量验证（提交真实任务到 Jenkins）
#   ./verify_chain.sh --quick    # 仅检查各组件连通性
#
# 在能同时访问 VPS 和 Jenkins 的机器上运行
# ============================================================================

set -euo pipefail

# ── 配置 ─────────────────────────────────────────────────────────────────────
BRIDGE_HOST="${BRIDGE_HOST:-127.0.0.1}"
BRIDGE_PORT="${BRIDGE_PORT:-14547}"
SLAVE_HOST="${SLAVE_HOST:-8.222.226.16}"
SLAVE_PORT="${SLAVE_PORT:-14548}"
BRIDGE_URL="http://${BRIDGE_HOST}:${BRIDGE_PORT}"
SLAVE_URL="http://${SLAVE_HOST}:${SLAVE_PORT}"
JENKINS_PATH="${SGLANG_JENKINS_PATH:-jenkins.svc.cambricon.com/dist/job/SGLANG/job/DEBUG/job/sglang_ci/}"
JENKINS_USER="${SGLANG_JENKINS_USER:-}"
JENKINS_TOKEN="${SGLANG_JENKINS_TOKEN:-}"
VERIFY_REPO_URL="${VERIFY_REPO_URL:-https://github.com/chenxb002/sglang.git}"
VERIFY_GIT_REF="${VERIFY_GIT_REF:-ci-poc}"
VERIFY_COMMIT_SHA="${VERIFY_COMMIT_SHA:-}"

# ── 颜色 ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASS="${GREEN}PASS${NC}"
FAIL="${RED}FAIL${NC}"
WARN="${YELLOW}WARN${NC}"

pass_line() { echo -e "  ${PASS}  $1"; }
fail_line() { echo -e "  ${FAIL}  $1 — $2"; }
warn_line() { echo -e "  ${WARN}  $1 — $2"; }
section()  { echo -e "\n${BLUE}═══ $* ═══${NC}"; }

# ── 计数器 ───────────────────────────────────────────────────────────────────
TOTAL=0
PASSED=0
FAILED=0

check() {
    local desc="$1"
    local ok="$2"
    local detail="${3:-}"
    TOTAL=$((TOTAL + 1))
    if [[ "$ok" -eq 0 ]]; then
        PASSED=$((PASSED + 1))
        pass_line "$desc"
    else
        FAILED=$((FAILED + 1))
        fail_line "$desc" "$detail"
    fi
}

# ── 辅助函数 ─────────────────────────────────────────────────────────────────
check_http() {
    # 返回 HTTP status code，失败返回 "fail"
    local url="$1"
    local extra_args="${2:-}"
    curl -s -o /dev/null -w "%{http_code}" --max-time 10 ${extra_args} "${url}" 2>/dev/null || echo "fail"
}

check_json_field() {
    # 检查 JSON 响应中是否包含某字段
    local resp="$1"
    local field="$2"
    echo "${resp}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('${field}',''))" 2>/dev/null || echo ""
}

# ── 1. VPS External 服务 ─────────────────────────────────────────────────────
verify_vps_services() {
    section "1  VPS External 服务"

    echo "  bridge: ${BRIDGE_URL}"
    echo "  slave:  ${SLAVE_URL}"
    echo ""

    # 1.1 bridge 可达
    local code
    code=$(check_http "${BRIDGE_URL}/")
    if [[ "${code}" =~ ^(200|400|404)$ ]]; then
        check "bridge HTTP 可达 (${code})" 0
    else
        check "bridge HTTP 可达" 1 "HTTP ${code}"
    fi

    # 1.2 slave 可达
    local slave_resp
    slave_resp=$(curl -s --max-time 10 "${SLAVE_URL}/source=master&aiming=get_data" 2>/dev/null || echo "")
    if echo "${slave_resp}" | grep -q '"tasks"'; then
        check "slave API 正常" 0
    else
        check "slave API 正常" 1 "返回: ${slave_resp:0:100}"
    fi

    # 1.3 slave 返回有效 JSON
    local task_count
    task_count=$(echo "${slave_resp}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('tasks',[])))" 2>/dev/null || echo "err")
    if [[ "${task_count}" != "err" ]]; then
        check "slave 返回有效 JSON" 0
        echo "        active tasks: ${task_count}"
    else
        check "slave 返回有效 JSON" 1 "JSON 解析失败"
    fi
}

# ── 2. 任务提交链路 ─────────────────────────────────────────────────────────
verify_task_submit() {
    section "2  任务提交链路 (GitHub Actions → bridge → slave)"

    local timestamp
    timestamp=$(date +%s%3N)

    local payload='{
        "timestamp": "'${timestamp}'",
        "repo": "sglang",
        "pr_id": "",
        "repo_url": "'${VERIFY_REPO_URL}'",
        "git_ref": "'${VERIFY_GIT_REF}'",
        "commit_sha": "'${VERIFY_COMMIT_SHA}'",
        "trigger_type": "ci",
        "trigger_id": "verify-chain-'${timestamp}'",
        "repeat_times": "1",
        "status": "running"
    }'

    # 2.1 提交任务到 bridge
    local submit_resp
    submit_resp=$(curl -s --max-time 10 \
        -X POST "${BRIDGE_URL}" \
        -H "Content-Type: application/json" \
        -d "${payload}" 2>/dev/null || echo "")

    local task_id
    task_id=$(echo "${submit_resp}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")

    if [[ -n "${task_id}" ]]; then
        check "POST 提交任务到 bridge" 0
        echo "        task_id: ${task_id}"
    else
        check "POST 提交任务到 bridge" 1 "返回: ${submit_resp:0:100}"
        # 无法继续后续查询
        echo ""
        return
    fi

    # 2.2 查询任务状态 (bridge 代理)
    sleep 1
    local status_resp
    status_resp=$(curl -s --max-time 10 \
        "${BRIDGE_URL}/aiming=get_status&id=${task_id}" 2>/dev/null || echo "")

    local task_status
    task_status=$(echo "${status_resp}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || echo "")

    if [[ -n "${task_status}" ]]; then
        check "GET 查询任务状态 (bridge→slave)" 0
        echo "        status: ${task_status}"
    else
        check "GET 查询任务状态 (bridge→slave)" 1 "返回: ${status_resp:0:100}"
    fi

    # 2.3 直接从 slave 查询
    local slave_status_resp
    slave_status_resp=$(curl -s --max-time 10 \
        "${SLAVE_URL}/source=bridge&aiming=get_status&id=${task_id}" 2>/dev/null || echo "")

    if echo "${slave_status_resp}" | grep -q '"id"'; then
        check "slave 直接查询任务" 0
    else
        check "slave 直接查询任务" 1 "返回: ${slave_status_resp:0:100}"
    fi

    # 保存 task_id 供后续步骤使用
    export VERIFY_TASK_ID="${task_id}"
    export VERIFY_TIMESTAMP="${timestamp}"
}

# ── 3. 内网 Master → slave 链路 ──────────────────────────────────────────────
verify_master_slave() {
    section "3  内网 Master → slave 链路"

    # 3.1 master 能否获取 active tasks
    local slave_resp
    slave_resp=$(curl -s --max-time 10 "${SLAVE_URL}/source=master&aiming=get_data" 2>/dev/null || echo "")

    local task_count
    task_count=$(echo "${slave_resp}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('tasks',[])))" 2>/dev/null || echo "err")

    if [[ "${task_count}" != "err" ]]; then
        check "master 获取 active tasks" 0
        echo "        active tasks: ${task_count}"
    else
        check "master 获取 active tasks" 1 "JSON 解析失败"
    fi

    # 3.2 检查刚提交的任务是否在 active 列表中
    if [[ -n "${VERIFY_TASK_ID:-}" ]]; then
        local found
        found=$(echo "${slave_resp}" | python3 -c "
import json,sys
d = json.load(sys.stdin)
tid = '${VERIFY_TASK_ID}'
found = any(t.get('id') == tid for t in d.get('tasks',[]))
print('found' if found else 'missing')
" 2>/dev/null || echo "err")

        if [[ "${found}" == "found" ]]; then
            check "验证任务在 active 列表中" 0
        elif [[ "${found}" == "missing" ]]; then
            check "验证任务在 active 列表中" 1 "任务不在 active 列表中（可能已被 master 处理）"
        fi
    fi

    # 3.3 master 进程检查
    local master_pid
    master_pid=$(pgrep -f 'master.py' 2>/dev/null | head -1 || true)
    if [[ -n "${master_pid}" ]]; then
        check "master.py 进程运行中 (pid=${master_pid})" 0
    else
        warn_line "master.py 未检测到" "请在部署 master 的机器上检查"
    fi
}

# ── 4. Jenkins 链路 ──────────────────────────────────────────────────────────
verify_jenkins() {
    section "4  Jenkins 链路"

    if [[ -z "${JENKINS_USER}" || -z "${JENKINS_TOKEN}" ]]; then
        warn_line "Jenkins 凭据未设置" "跳过 Jenkins 验证"
        echo "        设置 SGLANG_JENKINS_USER / SGLANG_JENKINS_TOKEN 后再试"
        return
    fi

    local jenkins_url="http://${JENKINS_PATH}"

    # 4.1 Jenkins job 可达
    local code
    code=$(check_http "${jenkins_url}api/json" "-u ${JENKINS_USER}:${JENKINS_TOKEN}")
    if [[ "${code}" == "200" ]]; then
        check "Jenkins job API 可达" 0
    elif [[ "${code}" == "401" ]]; then
        check "Jenkins job API 可达" 1 "认证失败 (HTTP 401)，检查 user/token"
    elif [[ "${code}" == "403" ]]; then
        check "Jenkins job API 可达" 1 "权限不足 (HTTP 403)，检查用户权限"
    elif [[ "${code}" == "404" ]]; then
        check "Jenkins job API 可达" 1 "job 不存在 (HTTP 404)，检查 jenkins_path"
    else
        check "Jenkins job API 可达" 1 "HTTP ${code}"
    fi

    # 4.2 获取 CSRF crumb
    local root_url
    root_url=$(echo "${jenkins_url}" | python3 -c "
from urllib.parse import urlparse
import sys
u = urlparse(sys.stdin.read().strip())
root = u.path.split('/job/')[0].rstrip('/') + '/'
print(f'{u.scheme}://{u.netloc}{root}')
" 2>/dev/null || echo "")

    if [[ -n "${root_url}" ]]; then
        local crumb_code
        crumb_code=$(check_http "${root_url}crumbIssuer/api/json" "-u ${JENKINS_USER}:${JENKINS_TOKEN}")
        if [[ "${crumb_code}" == "200" ]]; then
            check "Jenkins CSRF crumb 可获取" 0
        elif [[ "${crumb_code}" == "404" ]]; then
            check "Jenkins CSRF crumb" 0
            echo "        (CSRF 未启用，可继续)"
        else
            warn_line "Jenkins CSRF crumb HTTP ${crumb_code}" "继续..."
        fi
    fi
}

# ── 5. Smoke Test（可选）─────────────────────────────────────────────────────
verify_smoke() {
    section "5  Smoke Test — 提交真实任务"

    if [[ -z "${JENKINS_USER}" || -z "${JENKINS_TOKEN}" ]]; then
        warn_line "跳过 smoke test" "需要 Jenkins 凭据"
        return
    fi

    local timestamp
    timestamp=$(date +%s%3N)

    echo "  提交 smoke 任务到 bridge，等待 master 调度 Jenkins..."
    echo ""

    local payload='{
        "timestamp": "'${timestamp}'",
        "repo": "sglang",
        "pr_id": "",
        "repo_url": "'${VERIFY_REPO_URL}'",
        "git_ref": "'${VERIFY_GIT_REF}'",
        "commit_sha": "'${VERIFY_COMMIT_SHA}'",
        "trigger_type": "ci",
        "trigger_id": "verify-smoke-'${timestamp}'",
        "repeat_times": "1",
        "status": "running"
    }'

    # 提交
    local submit_resp
    submit_resp=$(curl -s --max-time 10 \
        -X POST "${BRIDGE_URL}" \
        -H "Content-Type: application/json" \
        -d "${payload}" 2>/dev/null || echo "")

    local task_id
    task_id=$(echo "${submit_resp}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")

    if [[ -z "${task_id}" ]]; then
        fail_line "提交 smoke 任务失败" "返回: ${submit_resp:0:100}"
        return
    fi

    pass_line "任务已提交: ${task_id}"
    echo ""
    echo "  ┌─────────────────────────────────────────────────────────────┐"
    echo "  │  任务已入队，等待 master 拉取 → Jenkins 执行                  │"
    echo "  │                                                             │"
    echo "  │  轮询状态:                                                  │"
    echo "  │    curl '${BRIDGE_URL}/aiming=get_status&id=${task_id}'      │"
    echo "  │                                                             │"
    echo "  │  预期状态流转: running → working → success / *_fail         │"
    echo "  │                                                             │"
    echo "  │  当前 master 每 10s 轮询一次，任务通常 1-3 分钟内被 pickup    │"
    echo "  └─────────────────────────────────────────────────────────────┘"
    echo ""

    # 等待并轮询（最多等 3 分钟）
    echo "  等待任务状态变化..."
    local max_wait=180
    local waited=0
    local interval=10
    local last_status="running"

    while [[ $waited -lt $max_wait ]]; do
        sleep $interval
        waited=$((waited + interval))

        local status_resp
        status_resp=$(curl -s --max-time 10 \
            "${BRIDGE_URL}/aiming=get_status&id=${task_id}" 2>/dev/null || echo "")

        local current_status
        current_status=$(echo "${status_resp}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','error'))" 2>/dev/null || echo "error")

        if [[ "${current_status}" != "${last_status}" ]]; then
            echo "  [${waited}s] status: ${last_status} → ${current_status}"
            last_status="${current_status}"
        fi

        local terminal_statuses='["success","test_fail","clone_fail","lint_check_fail","build_fail","search_case_fail","internal_error","unstable","error"]'
        local is_terminal
        is_terminal=$(echo "${current_status}" | python3 -c "
import json,sys
st = sys.stdin.read().strip()
terms = ${terminal_statuses}
print('yes' if st in terms else 'no')
" 2>/dev/null || echo "no")

        if [[ "${is_terminal}" == "yes" ]]; then
            echo ""
            echo "  ── 终态 ──"
            local log
            log=$(echo "${status_resp}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('log',''))" 2>/dev/null || echo "")
            local inner_id
            inner_id=$(echo "${status_resp}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('inner_id',''))" 2>/dev/null || echo "")

            if [[ "${current_status}" == "success" ]]; then
                echo -e "  ${GREEN}✓ SUCCESS${NC}  ${log}"
            else
                echo -e "  ${RED}✗ ${current_status}${NC}  ${log}"
            fi
            [[ -n "${inner_id}" ]] && echo "  Jenkins build: ${inner_id}"

            # 清理任务
            curl -s --max-time 5 "${BRIDGE_URL}/aiming=end_job&id=${task_id}" >/dev/null 2>&1 || true
            echo "  任务已清理"

            # 退出轮询
            if [[ "${current_status}" == "success" ]]; then
                check "Smoke test 结果" 0
            else
                check "Smoke test 结果" 1 "${current_status}"
            fi
            return
        fi
    done

    warn_line "超时" "任务在 ${max_wait}s 内未到达终态，当前: ${last_status}"
    echo "        手动查询: curl '${BRIDGE_URL}/aiming=get_status&id=${task_id}'"
}

# ── 汇总 ─────────────────────────────────────────────────────────────────────
summary() {
    section "验证汇总"

    echo ""
    echo "  ┌───────────────────────────────────────┐"
    echo "  │  总计: ${TOTAL}  通过: ${PASSED}  失败: ${FAILED}  │"
    echo "  └───────────────────────────────────────┘"
    echo ""

    if [[ "${FAILED}" -eq 0 ]]; then
        echo -e "  ${GREEN}✓ 链路正常 — 所有检查通过${NC}"
    else
        echo -e "  ${RED}✗ 存在 ${FAILED} 个失败项 — 请检查上述输出${NC}"
        echo ""
        echo "  排查顺序:"
        echo "    1. VPS external 服务: ssh user2@${SLAVE_HOST} && cd ~/sglang-ci-deploy && ./status.sh"
        echo "    2. 内网 master:       cd <ci-script-dir> && ./deploy_master.sh --status"
        echo "    3. Jenkins:           curl -u user:token 'http://${JENKINS_PATH}api/json'"
    fi
    echo ""
}

# ── 主入口 ───────────────────────────────────────────────────────────────────
main() {
    local mode="${1:-full}"

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  SGLang MLU CI — 端到端链路验证                               ║"
    echo "║  bridge: ${BRIDGE_URL}                                  ║"
    echo "║  slave:  ${SLAVE_URL}                      ║"
    echo "╚══════════════════════════════════════════════════════════════╝"

    case "${mode}" in
        --quick)
            verify_vps_services
            ;;
        --smoke)
            verify_vps_services
            verify_task_submit
            verify_master_slave
            verify_jenkins
            verify_smoke
            ;;
        full|*)
            verify_vps_services
            verify_task_submit
            verify_master_slave
            verify_jenkins
            ;;
    esac

    summary
}

VERIFY_TASK_ID=""
VERIFY_TIMESTAMP=""

main "$@"
