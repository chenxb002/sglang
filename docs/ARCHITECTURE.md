# SGLang MLU CI — 架构与交互设计

## 总览

SGLang MLU CI 通过**外网提交、内网拉取**的 bridge 模式将 GitHub Actions
接入内部 Jenkins/MLU 集群。外网任何时候都无法直接访问 Jenkins 或 MLU 资源，
内网组件主动向外轮询拉取任务。

```
┌──────────────────────────────────────────────────────────────────┐
│                         GitHub.com                               │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │  GitHub Actions Workflow (pr-test-mlu / nightly-test-mlu) │   │
│  │  runs-on: [cambricon]   ← VPS 上的 self-hosted runner     │   │
│  └──────────────────────────┬────────────────────────────────┘   │
└─────────────────────────────┼────────────────────────────────────┘
                              │ POST / GET (HTTP)
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                    VPS / DMZ (8.222.226.16)                      │
│                                                                  │
│  ┌─────────────┐     ┌─────────────┐     ┌──────────────────┐   │
│  │  bridge.py  │────▶│  slave.py   │     │ GitHub Runner    │   │
│  │  :14547     │     │  :14548     │     │ (actions-runner) │   │
│  │  localhost  │     │  0.0.0.0    │     └──────────────────┘   │
│  └─────────────┘     └──────┬──────┘                            │
│                             │                                    │
│              Task DB: ~/data/mlu_ci/sglang_tasks.json            │
│              日志:    ~/data/mlu_ci/logs/<task_id>.log           │
└─────────────────────────────┼────────────────────────────────────┘
                              │ GET（内网轮询）
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                   内网                                           │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  master.py                                               │    │
│  │  - 轮询 slave.py 获取活跃任务                             │    │
│  │  - 触发 Jenkins buildWithParameters                      │    │
│  │  - 增量同步 Jenkins console log 到 slave                  │    │
│  │  - 回传前对设备信息脱敏                                    │    │
│  └───────────────────────┬──────────────────────────────────┘    │
│                          │ HTTP (Jenkins API)                    │
│                          ▼                                       │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  Jenkins (jenkins.svc.cambricon.com)                     │    │
│  │  Job: SGLANG/DEBUG/sglang_ci                             │    │
│  │  Pipeline: jenkins_sglang.pipeline                       │    │
│  └───────────────────────┬──────────────────────────────────┘    │
│                          │ K8s / cnpipe                          │
│                          ▼                                       │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  MLU Pod                                                  │    │
│  │  - Clone 仓库 → 安装 sglang → 执行 MLU 测试套件           │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

## 模块

### 1. VPS — 外网网关（`vps/`）

部署在外网 VPS 或 DMZ 主机上，与 GitHub self-hosted runner 同机。
此模块**不持有** Jenkins 凭据，**不访问**任何内网资源。

| 组件 | 端口 | 绑定地址 | 职责 |
|---|---|---|---|
| `bridge.py` | 14547 | `127.0.0.1` | GitHub Actions 的唯一入口。校验仓库名，转发任务到 slave。 |
| `slave.py` | 14548 | `0.0.0.0` | 任务状态存储 + 日志仓库。被内网 master 轮询。 |
| `common.py` | — | — | 公共工具：配置加载、task_id 生成、HTTP 响应。 |

#### 1.1 bridge.py — HTTP API

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/` | 提交新 CI 任务。校验 `repo` 是否在允许列表中。 |
| `GET` | `/aiming=get_status&id=<id>` | 获取任务轻量状态（status、最近日志、Jenkins build id）。 |
| `GET` | `/aiming=get_log&id=<id>[&start=<N>][&tail=<N>]` | 下载 Jenkins console log。`start` 从指定字节偏移读取；`tail` 返回最后 N 行。 |
| `GET` | `/aiming=end_job&id=<id>` | 删除任务及日志文件（artifact 上传后清理）。 |

所有端点均代理到 `slave.py`——bridge 不直接操作任务存储。

#### 1.2 slave.py — 任务存储

`slave.py` 维护任务数据库和 Jenkins 日志文件，是双方可见任务状态的唯一数据源。

**任务状态机：**

```
                    ┌──────────┐
                    │  running  │  ← GitHub Actions 提交
                    └────┬─────┘
                         │ master 拉取，触发 Jenkins
                    ┌────▼─────┐
                    │  working  │  ← Jenkins build 执行中
                    └────┬─────┘
                         │ Jenkins 完成
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
     ┌─────────┐   ┌──────────┐   ┌──────────┐
     │ success │   │ *_fail   │   │  error   │
     └────┬────┘   └────┬─────┘   └────┬─────┘
          │              │              │
          └──────────────┴──────────────┘
                         │ GitHub Actions 调用 end_job()
                         ▼
                    (已删除)
```

**终态列表：** `success`、`test_fail`、`clone_fail`、`lint_check_fail`、
`build_fail`、`search_case_fail`、`internal_error`、`unstable`、`error`。

**垃圾回收：**
- 活跃任务超过 `active_task_timeout_seconds`（默认 48 小时）→ 标记为 `error`
- 终态任务超过 `terminal_retention_seconds`（默认 24 小时）→ 删除任务及日志文件

**数据持久化：**

| 路径 | 内容 |
|---|---|
| `<data_dir>/sglang_tasks.json` | 任务元数据（通过 tmp+rename 原子写入） |
| `<data_dir>/logs/<task_id>.log` | 完整 Jenkins console log（增量追加） |

#### 1.3 部署

```bash
./deploy_vps.sh                 # 全量部署
./deploy_vps.sh --restart       # 重新部署并重启 bridge/slave
./deploy_vps.sh --status        # 健康检查
./deploy_vps.sh --stop          # 停止所有服务
./deploy_vps.sh --runner-only   # 仅安装/启动 GitHub runner
```

默认路径（可通过环境变量覆盖）：

| 变量 | 默认值 |
|---|---|
| `SGLANG_CI_DEPLOY_DIR` | `~/data/mlu_ci/deploy` |
| `SGLANG_CI_DATA_DIR` | `~/data/mlu_ci` |

---

### 2. Internal — 内网调度器（`internal/`）

部署在内网主机上，需同时能访问 VPS slave 和 Jenkins。

| 组件 | 职责 |
|---|---|
| `master.py` | 轮询 slave 获取活跃任务、触发 Jenkins、增量同步日志回 slave。 |
| `sanitize.py` | 对日志做脱敏处理，防止设备标识和内部主机名泄露到外网。 |
| `common.py` | 公共工具：配置读取、URL 规范化、环境变量解析。 |
| `verify_chain.sh` | 端到端连通性检查和 smoke 测试脚本。 |

#### 2.1 master.py — 轮询循环

```
┌──────────┐     ┌──────────────┐     ┌──────────┐     ┌───────────┐
│  slave   │────▶│  有活跃      │────▶│ Jenkins  │────▶│  Jenkins  │
│  GET     │     │  任务?       │     │ build    │     │  poll     │
│  /data   │     │              │     │ WithPar  │     │  build    │
└──────────┘     └──────┬───────┘     └──────────┘     └─────┬─────┘
                        │ 无                                  │
                        ▼                                     ▼
                 ┌──────────┐                         ┌──────────────┐
                 │ sleep N  │                         │ sync/drain   │
                 │ 秒       │                         │ log → slave  │
                 └──────────┘                         └──────┬───────┘
                                                             │
                                  ┌──────────────────────────┘
                                  ▼
                         ┌──────────────┐
                         │ post_update  │
                         │ 终态状态     │
                         └──────────────┘
```

**关键方法：**

| 方法 | 职责 |
|---|---|
| `process_once()` | 单次迭代：拉取任务 → 触发 → 同步 → 完成。 |
| `sync_jenkins_log()` | Build 运行中的增量日志同步。 |
| `drain_jenkins_log()` | Build 结束后排空剩余日志再发布终态。 |
| `post_update()` | JSON 更新 slave 状态（status、inner_id、log_status）。 |
| `post_log_chunk()` | text/plain 追加日志文件到 slave（带 offset 校验幂等）。 |
| `classify_failure()` | 扫描 console 文本中的 stage 标记，将 Jenkins FAILURE 映射为具体 CI 状态。 |

#### 2.2 sanitize.py — 日志脱敏

`master.py` 在每段日志写入 VPS 前调用。防止设备序列号、内部主机名、
Kubernetes pod 名等敏感信息离开内网。

| 类别 | 匹配模式 | 示例 → 脱敏后 |
|---|---|---|
| 凭据 | `Authorization:`、`token=`、`password=`、`secret=` | `Authorization: ***` |
| 硬件标识 | `SN :`、`UUID :`、`Firmware :` | `SN : ***` |
| MLU 型号 | `MLU\d{3}(?:-\w+)?` | `MLU590-M9DK` → `MLU********`（等长） |
| 集群 DNS | `*.svc.cluster.local` | `***` |
| 节点名 | `cam-test-ai\d+` | `***` |
| Pod 名 | `cncl/<base>-xxxxx-xxxxx` | `cncl/***` |

#### 2.3 部署

```bash
export SGLANG_JENKINS_USER='<用户名>'
export SGLANG_JENKINS_TOKEN='<token>'
./deploy_master.sh                  # 全量部署
./deploy_master.sh --restart        # 重新部署并重启
./deploy_master.sh --status         # 健康检查
./deploy_master.sh --once           # 单次轮询（调试用）
```

---

### 3. Jenkins — 执行器（`jenkins/`）

Pipeline 文件 `jenkins_sglang.pipeline` 是 Jenkins 侧的定义，
使用 `cambricon-pipe-lib@master` 共享库（`cnpipe` DSL）。

#### 3.1 参数映射

`master.py` 通过 `buildWithParameters` 传入：

| Jenkins 参数 | 任务字段 |
|---|---|
| `repo` | `repo` |
| `timestamp` | `timestamp` |
| `pr_id` | `pr_id` |
| `task_id` | `id` |
| `trigger_type` | `trigger_type` |
| `repo_url` | `repo_url` |
| `git_ref` | `git_ref` |
| `commit_sha` | `commit_sha` |

#### 3.2 trigger_type → 测试套件

```
ci / pr / pull_request / push  ──▶ pr-test-1-mlu (1 卡 MLU)
                                   pr-test-2-mlu (2 卡 MLU)

nightly  ────────────────────────▶ nightly-test-mlu (2 卡 MLU,
                                   --continue-on-error)
```

平台选择走 **auto-discovery**（pipeline 中不设 `SGLANG_PLATFORM` 环境变量）。
MLU 容器中 `torch.mlu.is_available() == True` 且 `torch.cuda.is_available() == False`，
in-tree 平台解析器会自动选到 `MluSRTPlatform`。

#### 3.3 Pipeline 阶段

```
stage0_clone_sglang_task
  │
  ├─ clone 仓库
  ├─ checkout 指定 commit / branch / PR ref
  └─ stash 源码
       │
       ▼
  pr-test-1-mlu                    pr-test-2-mlu
  reqMlus: 1                       reqMlus: 2
  timeout: 480 min                 timeout: 480 min
       │                                │
       ├─ pip install sglang[dev_mlu]   ├─ pip install sglang[dev_mlu]
       ├─ cnmon info                   ├─ cnmon info
       └─ run_suite.py --suite         └─ run_suite.py --suite
          pr-test-1-mlu                   pr-test-2-mlu
```

---

### 4. GitHub Actions 客户端（`run_mlu_ci.py`）

位于 **SGLang 源码仓库** `scripts/ci/mlu/`，运行在 VPS GitHub runner 上。

#### 4.1 执行流程

```
invoke_mlu_ci.sh
  │
  ├─ 收集 GitHub event 上下文（ref、sha、pr、repo_url）
  └─ python3 scripts/ci/mlu/run_mlu_ci.py
       │
       ├─ POST bridge /          → 提交任务 → 获得 task_id
       │
       ├─ 轮询（每 poll_interval 秒）：
       │    ├─ GET get_status    → status、log_size、log_status
       │    └─ GET get_log&start= → 拉取增量日志并打印
       │
       └─ 终态：
            ├─ GET get_log       → 下载完整日志 → artifact
            ├─ 从本地文件打印最后一段增量（保证与 artifact 一致）
            └─ GET end_job       → 清理远端任务
```

#### 4.2 日志打印的一致性保证

- **轮询期间：** `get_log&start=<last_fetched>` 仅获取新字节。
  `last_fetched` 以**实际收到的字节数**推进，而非 `get_status` 报告的文件大小，
  避免文件尚未落盘时跳过中间段。
- **终态/超时时：** 下载完整日志后直接按 `full[last_fetched:]` 切片打印剩余部分，
  Actions 页面输出与 artifact 内容严格一致。
- **下载重试：** 3 次带退避的重试。全部失败时不调用 `end_job`，保留任务等待 retention 自动清理。

#### 4.3 Workflow 中的 artifact 上传

```yaml
- name: Submit MLU CI task to Cambricon Runner
  env:
    SGLANG_MLU_CI_BRIDGE_URL: http://127.0.0.1:14547
    SGLANG_MLU_CI_LOG_DIR: mlu-ci-logs
  run: bash scripts/ci/mlu/invoke_mlu_ci.sh

- name: Upload MLU Jenkins log
  if: always()
  uses: actions/upload-artifact@v4
  with:
    name: mlu-jenkins-log-${{ github.run_id }}-${{ github.run_attempt }}
    path: mlu-ci-logs/
```

---

## 消息流

整个链路分为四个阶段：**任务提交**、**调度入队**、**执行与轮询**、**终态收敛**。
下面按阶段详细列出每一步涉及的接口、请求/响应格式、可能的结果及处理方式。

---

### 阶段一：任务提交

GitHub Actions workflow 触发后，`invoke_mlu_ci.sh` 收集上下文，
交给 `run_mlu_ci.py` 通过本地 bridge 提交任务。

#### 步骤 1：GitHub Actions → bridge.py（提交任务）

| 项目 | 内容 |
|---|---|
| 调用方 | `run_mlu_ci.py`（VPS 上 `127.0.0.1`） |
| 接口 | `POST http://127.0.0.1:14547/` |
| Content-Type | `application/json` |

请求体：

```json
{
    "timestamp": "1783220259048",
    "repo": "sglang",
    "pr_id": "",
    "repo_url": "https://github.com/<owner>/sglang.git",
    "git_ref": "mlu_backend",
    "commit_sha": "c202dadd...",
    "trigger_type": "ci",
    "trigger_id": "chenxiaobing",
    "repeat_times": "1",
    "status": "running"
}
```

| 字段 | 来源 | 说明 |
|---|---|---|
| `timestamp` | `date +%s%3N` | 毫秒时间戳，保证 task_id 唯一 |
| `repo` | `SGLANG_MLU_CI_REPO_NAME` | 仓库名，bridge 会校验白名单 |
| `pr_id` | GitHub event | PR 编号，非 PR 流为空 |
| `repo_url` | GitHub event / `GITHUB_REPOSITORY` | 要 clone 的仓库 URL |
| `git_ref` | `GITHUB_REF_NAME` | 分支或 tag |
| `commit_sha` | `GITHUB_SHA` | 精确 commit |
| `trigger_type` | workflow input | `ci`/`pr`/`pull_request`/`push`/`nightly` |
| `trigger_id` | `GITHUB_ACTOR` | 触发者用户名 |
| `repeat_times` | 固定 `"1"` | 保留字段 |

bridge.py 处理逻辑：
1. 校验 `repo` 是否匹配 `allowed_repo`（默认 `sglang`）
2. 校验必填字段：`timestamp`、`repo`、`trigger_type`、`trigger_id`
3. 补充 `source=bridge`，转发给 slave

可能的返回：

| HTTP | 场景 | `run_mlu_ci.py` 行为 |
|---|---|---|
| 200 `{"status": "200", "id": "<task_id>"}` | 成功 | 进入轮询阶段 |
| 400 `{"status": "error", "error": "unsupported repo: xxx"}` | 仓库名不在白名单 | 打印错误，exit 1 |
| 400 `{"status": "error", "error": "missing required fields: ..."}` | 必填字段缺失 | 打印错误，exit 1 |
| 500 / 连接失败 | bridge 不可用 | 打印错误，exit 1 |

#### 步骤 2：bridge.py → slave.py（转发任务）

| 项目 | 内容 |
|---|---|
| 接口 | `POST http://127.0.0.1:14548/` |
| Content-Type | `application/json` |

请求体与步骤 1 相同，额外增加 `"source": "bridge"`。

slave.py 处理逻辑：
1. `Task.from_dict(data)` 构造 Task 对象
2. 如果 `id` 为空，由 `make_task_id()` 根据身份字段生成 MD5 哈希
3. `store.upsert(task)`：若已存在同 id 任务且状态为 `waiting`/`working`，保留现场（不覆盖 status/log/inner_id）；否则写入新任务
4. 原子写入 `sglang_tasks.json`（tmp + rename）

返回：

| HTTP | 场景 |
|---|---|
| 200 `{"status": "success", "id": "<task_id>"}` | 成功创建或更新任务 |

bridge 收到后原样返回给 `run_mlu_ci.py`。

---

### 阶段二：调度入队

`master.py` 运行在内网，以固定间隔（默认 5 秒）轮询 slave。

#### 步骤 3：master.py → slave.py（拉取活跃任务）

| 项目 | 内容 |
|---|---|
| 接口 | `GET http://<slave_host>:14548/?source=master&aiming=get_data` |
| 频率 | 每 `--interval` 秒（默认 5） |

slave.py 返回所有 `status` 不为终态的任务：

```json
{
    "tasks": [
        {
            "id": "abc123...",
            "timestamp": "1783220259048",
            "repo": "sglang",
            "repo_url": "https://github.com/.../sglang.git",
            "git_ref": "mlu_backend",
            "commit_sha": "c202dadd...",
            "trigger_type": "ci",
            "trigger_id": "chenxiaobing",
            "repeat_times": "1",
            "status": "running",
            "inner_id": "",
            "log": "",
            "log_offset": "0",
            "log_status": "pending",
            "log_error": ""
        }
    ]
}
```

master.py 处理逻辑（`process_once`）：

```
对每个 task：
  ├─ status=running 且 inner_id 为空 → 步骤 4，触发 Jenkins
  ├─ status=running/waiting 且 inner_id 为空 → 尝试从 Jenkins 反查 build id（步骤 4b）
  ├─ status=working 且有 inner_id → 步骤 7，轮询 Jenkins
  └─ 无活跃任务 → sleep，等待下一轮
```

#### 步骤 4：master.py → Jenkins（触发 build）

| 项目 | 内容 |
|---|---|
| 接口 | `POST http://jenkins.svc.cambricon.com/.../sglang_ci/buildWithParameters` |
| 认证 | Basic Auth（`SGLANG_JENKINS_USER` + `SGLANG_JENKINS_TOKEN`） |
| CSRF | 先 `GET /crumbIssuer/api/json` 获取 crumb（若 404 则跳过） |

请求参数（来自 `jenkins_params` 映射）：

| 参数名 | 来源 Task 字段 |
|---|---|
| `repo` | `repo` |
| `timestamp` | `timestamp` |
| `pr_id` | `pr_id` |
| `task_id` | `id` |
| `trigger_type` | `trigger_type` |
| `repo_url` | `repo_url` |
| `git_ref` | `git_ref` |
| `commit_sha` | `commit_sha` |

Jenkins 返回 `302` 重定向到 queue item URL。master 调用 `wait_for_queue_executable()`：

```
GET <queue_url>/api/json
  ├─ cancelled=true → RuntimeError，标记任务 error
  ├─ executable.number 存在 → 获得 build id
  └─ 超时（120 秒）→ 返回 None
```

可能的结果：

| 结果 | master 行为 |
|---|---|
| 获得 build id（如 `42`） | `post_update(slave, task_id, status="working", inner_id="42")` |
| `wait_for_queue_executable` 返回 None | `post_update(slave, task_id, status="waiting")`，下次轮询重新反查 |
| HTTP 401/403 | `post_update(slave, task_id, status="error")`，跳过此任务 |
| 其他 HTTP 错误 | 抛异常，`process_once` 捕获后打印日志，下一轮重试 |

#### 步骤 4b：反查 build id（队列超时后的恢复路径）

若步骤 4 中超时未拿到 build id，任务状态为 `waiting`。
下一轮 `process_once` 走反查路径：

| 项目 | 内容 |
|---|---|
| 接口 | `GET http://jenkins.../sglang_ci/api/json?tree=builds[url,result,id]` |
| 逻辑 | 遍历最近 build，对每个 build 调用 `api/json` 检查 `actions[*].parameters` 中是否有 `task_id` 匹配 |

找到后：`post_update(slave, task_id, status="working", inner_id="<found_id>")`

#### 步骤 5：状态更新 — running → working

`post_update` 调用 slave API：

| 项目 | 内容 |
|---|---|
| 接口 | `POST http://<slave>:14548/` |
| Content-Type | `application/json` |
| 请求体 | `{"source": "master", "id": "<task_id>", "status": "working", "inner_id": "42"}` |

slave 更新 `task.status` 和 `task.inner_id`，写入 JSON DB。

重试：最多 10 次，间隔 3 秒。全部失败后抛 `RuntimeError`，
`process_once` 打印 `[master] loop failed`，下一轮重新处理。

---

### 阶段三：执行与并行轮询

此阶段两条链路同时进行：
- **GitHub Actions** 周期性查询 `get_status`，有新日志时通过 `get_log&start=` 拉取增量并打印
- **master.py** 周期性查询 Jenkins build 状态，增量同步 console log 到 slave

#### 步骤 6：GitHub Actions 轮询状态

| 项目 | 内容 |
|---|---|
| 调用方 | `run_mlu_ci.py` |
| 接口 | `GET http://127.0.0.1:14547/aiming=get_status&id=<task_id>` |
| 频率 | 每 `poll_interval` 秒（默认 10） |
| 代理 | bridge → `GET http://127.0.0.1:14548/?source=bridge&aiming=get_status&id=<id>` |

slave 返回：

```json
{
    "id": "abc123...",
    "status": "working",
    "log": "最近 50 行 Jenkins console...",
    "inner_id": "42",
    "log_offset": "170507",
    "log_status": "syncing",
    "log_error": "",
    "log_size": 58449
}
```

`run_mlu_ci.py` 处理：
1. 若 `status` 变化 → 打印新状态
2. 若 `log_size > last_fetched` → 调用 `get_log&start=<last_fetched>` 拉取增量日志并打印
3. `last_fetched` 以**实际收到的字节数**推进（见步骤 8）
4. 若 `status` 为终态 → 进入阶段四

可能的异常：

| 场景 | 行为 |
|---|---|
| `get_status` 网络超时 / JSON 解析失败 | 打印 stderr，sleep 后重试，不更新 `last_fetched` |
| `get_status` 返回 404（任务被清理） | `status` 为 `"error"`，进入终态处理 |

#### 步骤 7：master.py 轮询 Jenkins

| 项目 | 内容 |
|---|---|
| 接口 | `GET http://jenkins.../sglang_ci/<build_id>/api/json` |
| 频率 | 每轮 `process_once`（5 秒间隔） |

返回包含 `result`（`SUCCESS`/`FAILURE`/`UNSTABLE`/`ABORTED`）和 `building`（布尔值）。

master.py 处理逻辑：

```
若 building=true 或 result 为非终态：
  → sync_jenkins_log(slave, task, jenkins, build_id)
      ├─ GET ../<build_id>/logText/progressiveText?start=<log_offset>
      ├─ 收到 chunk + X-Text-Size + X-More-Data
      ├─ redact_log(chunk) 脱敏
      ├─ post_log_chunk(slave, task_id, chunk, offset, next_offset)
      │    └─ POST /?source=master&aiming=append_log&id=<id>
      │       &log_start_offset=<offset>&log_offset=<next_offset>
      │       Content-Type: text/plain; charset=utf-8
      │       Body: <脱敏后的日志增量>
      └─ post_update(slave, task_id, status="working", log_status="syncing")

若 result 为终态（SUCCESS/FAILURE/UNSTABLE/ABORTED）：
  → drain_jenkins_log(slave, task, jenkins, build_id)
      └─ 排空剩余日志（最多 LOG_DRAIN_MAX_CHUNKS=100 轮）
  → 按 result 分类为 CI 状态（见下表）
  → post_update(slave, task_id, status=<ci_status>, log_status="complete")
```

#### 步骤 7a：sync_jenkins_log — 增量日志同步

| 项目 | 内容 |
|---|---|
| 接口 | `GET http://jenkins.../<build_id>/logText/progressiveText?start=<offset>` |
| 响应头 | `X-Text-Size: <next_offset>`、`X-More-Data: true/false` |
| 响应体 | 从 `offset` 开始的新增 console 文本 |

master.py 处理：
1. `redact_log(chunk)` 脱敏（sanitize.py）
2. `post_log_chunk(slave_url, task_id, chunk, offset, next_offset)`
   - slave 校验 `log_offset == start_offset`（offset 不匹配返回 409，防重复写入）
   - slave 追加到 `<task_id>.log` 文件
   - slave 将 `task.log` 截断到最近 `LOG_TAIL_LINES`（50）行
3. `post_update` 更新 `log_status="syncing"`

失败处理：

| 场景 | 行为 |
|---|---|
| Jenkins progressiveText 请求失败 | 记录 `log_error`，`log_status="failed"`，下一次轮询重试 |
| `post_log_chunk` 返回 409（offset 冲突） | slave 拒绝写入，master 打印日志继续 |
| `post_log_chunk` 10 次重试均失败 | 抛异常，下一轮 `process_once` 重试 |

#### 步骤 7b：drain_jenkins_log — 排空最终日志

Build 完成后，排空剩余日志（最多 100 轮），确保 slave 上的日志文件完整。
逻辑与 `sync_jenkins_log` 相同，但会持续拉取直到 `X-More-Data: false`。
排空完成后 `log_status` 设为 `"complete"`。

失败时：`log_status` 设为 `"failed"`，但仍继续发布终态。
**CI 状态与日志同步状态解耦**——日志同步失败不影响 CI 结果回传。

#### 步骤 8：GitHub Actions 增量拉取日志

| 项目 | 内容 |
|---|---|
| 调用方 | `run_mlu_ci.py`（`_print_log_progress`） |
| 接口 | `GET http://127.0.0.1:14547/aiming=get_log&id=<id>&start=<last_fetched>` |
| 代理 | bridge → `GET http://127.0.0.1:14548/?source=bridge&aiming=get_log&id=<id>&start=<N>` |

slave 处理：
1. `read_bytes()` 读取完整日志文件
2. `raw[start:]` 按字节偏移切片
3. `decode("utf-8")` 返回纯文本

`run_mlu_ci.py` 处理：
1. 打印 chunk（不带 `::group::` 折叠）
2. `last_fetched = last_fetched + len(chunk.encode("utf-8"))`
   ——以实际收到的字节数推进，而非 `get_status` 的 `log_size`

| 场景 | 行为 |
|---|---|
| `log_size <= last_fetched` | 无需拉取，直接 sleep |
| `get_log&start=` 网络失败 | 打印 stderr，`last_fetched` 不推进，下一轮重试 |
| `get_log&start=` 返回空文本 | `last_fetched` 不推进（文件尚未落盘） |

#### 步骤 9：Jenkins 结果分类

`classify_failure()` 扫描 console 文本中的 stage 标记：

| Jenkins result | 匹配规则 | CI 状态 |
|---|---|---|
| `SUCCESS` | — | `success` |
| `UNSTABLE` | — | `unstable` |
| `ABORTED` | — | `error` |
| `FAILURE` | 包含 `stage0` / `clone_sglang_task` | `clone_fail` |
| `FAILURE` | 包含 `stage1` | `lint_check_fail` |
| `FAILURE` | 包含 `stage2` | `build_fail` |
| `FAILURE` | 包含 `stage3` | `search_case_fail` |
| `FAILURE` | 包含 `stage4` / `pr-test-` / `nightly-test-mlu` | `test_fail` |
| `FAILURE` | 无法识别 | `internal_error` |

---

### 阶段四：终态收敛

#### 步骤 10：GitHub Actions 检测终态 → 下载完整日志

`run_mlu_ci.py` 在 `get_status` 返回终态后：

1. **下载完整日志**（artifact）：

| 项目 | 内容 |
|---|---|
| 接口 | `GET http://127.0.0.1:14547/aiming=get_log&id=<id>`（不带 start 参数） |
| 重试 | 3 次，间隔 3 秒 |

成功：保存到 `<log_dir>/jenkins-<task_id>.log`

失败：打印 stderr，不调用 `end_job`（保留远端任务等待 retention 清理）

2. **打印最后一段增量**（保证 Actions 页面与 artifact 一致）：

```python
full = log_path.read_text()
if len(full) > last_fetched:
    print(full[last_fetched:])  # 直接打印，不带 ::group:: 折叠
```

3. **清理远端任务**：

| 条件 | 行为 |
|---|---|
| 日志下载成功 且 `log_status` 为 `""` 或 `"complete"` | `GET /aiming=end_job&id=<id>` → slave 删除任务 + 日志文件 |
| 日志下载失败 或 `log_status="failed"` | 跳过 `end_job`，任务保留到 retention 自动清理 |

4. **退出码**：

| status 包含 | 退出码 |
|---|---|
| `"success"` | 0 |
| 其他终态 | 1 |
| 超时 | 1 |

#### 步骤 11：超时处理

`run_mlu_ci.py` 的轮询循环有总超时（默认 `SGLANG_MLU_CI_TIMEOUT_SECONDS`）：

| 触发条件 | `time.monotonic() - start > timeout_seconds` |
|---|---|
| 行为 | 下载完整日志 → 打印最后一段增量 → exit 1 |
| 与终态的区别 | 不调用 `end_job`（任务可能仍在执行中） |

#### 完整终态决策表

| 条件 | 下载日志 | 打印增量 | end_job | exit |
|---|---|---|---|---|
| status=success, log_status=complete, 下载成功 | ✓ | ✓ | ✓ | 0 |
| status=success, log_status=failed | ✓ | ✓ | ✗ | 0 |
| status=success, 下载失败 | ✗ | ✗ | ✗ | 0 |
| status=*_fail/error/unstable | ✓ | ✓ | 条件* | 1 |
| 超时 | ✓ | ✓ | ✗ | 1 |

\* `end_job` 条件：日志下载成功且 `log_status` 为 `""` 或 `"complete"`。

---

### 日志数据流（详细）

```
Jenkins                          master.py                       slave.py                   run_mlu_ci.py
  │                                  │                              │                            │
  │ ① GET progressiveText?start=0    │                              │                            │
  │◀─────────────────────────────────│                              │                            │
  │  <chunk_1>                       │                              │                            │
  │  X-Text-Size: 5000               │                              │                            │
  │  X-More-Data: true               │                              │                            │
  │─────────────────────────────────▶│                              │                            │
  │                                  │                              │                            │
  │                                  │ ② redact_log(chunk_1)        │                            │
  │                                  │    脱敏：SN/UUID/MLU型号/    │                            │
  │                                  │    主机名/Pod名/集群DNS       │                            │
  │                                  │                              │                            │
  │                                  │ ③ POST /?source=master&      │                            │
  │                                  │    aiming=append_log&id=<id>  │                            │
  │                                  │    &log_start_offset=0        │                            │
  │                                  │    &log_offset=5000           │                            │
  │                                  │    Content-Type: text/plain   │                            │
  │                                  │    Body: <脱敏后的 chunk_1>   │                            │
  │                                  │─────────────────────────────▶│                            │
  │                                  │                              │                            │
  │                                  │                              │ ④ 校验 offset：           │
  │                                  │                              │    task.log_offset == 0?   │
  │                                  │                              │    ✓ → 追加到 <id>.log     │
  │                                  │                              │    task.log_offset = 5000   │
  │                                  │                              │    task.log = tail(50行)   │
  │                                  │                              │    保存 sglang_tasks.json  │
  │                                  │                              │                            │
  │                                  │ ⑤ {"status":"success",       │                            │
  │                                  │    "log_offset":"5000"}      │                            │
  │                                  │◀─────────────────────────────│                            │
  │                                  │                              │                            │
  │                                  │                              │ ⑥ GET get_log&start=0      │
  │                                  │                              │◀───────────────────────────│
  │                                  │                              │                            │
  │                                  │                              │ ⑦ read_bytes()             │
  │                                  │                              │    raw[0:] → decode        │
  │                                  │                              │───────────────────────────▶│
  │                                  │                              │    <chunk_1 增量文本>      │
  │                                  │                              │                            │
  │                                  │                              │ ⑧ last_fetched = 5000      │
  │                                  │                              │    print(chunk_1)           │
  │                                  │                              │                            │
  │  ... 循环 ①-⑧，每次 Jenkins      │                              │                            │
  │  产生新日志，master 拉取并脱敏    │                              │                            │
  │  回传，GitHub Actions 增量打印 ...│                              │                            │
  │                                  │                              │                            │
  │ ⑨ GET build_info                 │                              │                            │
  │◀─────────────────────────────────│                              │                            │
  │  result=SUCCESS, building=false   │                              │                            │
  │─────────────────────────────────▶│                              │                            │
  │                                  │                              │                            │
  │                                  │ ⑩ drain 剩余日志             │                            │
  │                                  │    log_status="complete"     │                            │
  │                                  │    post_update status=success│                            │
  │                                  │─────────────────────────────▶│                            │
  │                                  │                              │                            │
  │                                  │                              │ ⑪ GET get_status           │
  │                                  │                              │◀───────────────────────────│
  │                                  │                              │    status=success          │
  │                                  │                              │    log_size=328370         │
  │                                  │                              │───────────────────────────▶│
  │                                  │                              │                            │
  │                                  │                              │ ⑫ GET get_log（全量）      │
  │                                  │                              │◀───────────────────────────│
  │                                  │                              │    read_bytes() 全量       │
  │                                  │                              │───────────────────────────▶│
  │                                  │                              │    <完整日志 328KB>        │
  │                                  │                              │                            │
  │                                  │                              │ ⑬ 保存 artifact            │
  │                                  │                              │    print(full[last:])      │
  │                                  │                              │    → Actions 页面          │
  │                                  │                              │                            │
  │                                  │                              │ ⑭ GET end_job              │
  │                                  │                              │◀───────────────────────────│
  │                                  │                              │    删除任务 + 日志文件     │
  ▼                                  ▼                              ▼                            ▼
```

#### offset 幂等校验

`post_log_chunk` 传递 `log_start_offset` 和 `log_offset`，slave 校验 `task.log_offset == log_start_offset`：

| 情况 | slave 行为 | 目的 |
|---|---|---|
| offset 匹配 | 追加 chunk，更新 `log_offset = next_offset` | 正常增量写入 |
| offset 不匹配 | 返回 409 Conflict | 防止 master 重试导致日志重复 |
| `next_offset` 为空 | 只更新 `log_status`，不推进 offset | 仅状态同步，不写日志 |

#### get_log start 参数

| 参数 | slave `read_log` 行为 | 用途 |
|---|---|---|
| 无 `start` | `read_bytes()` 全量返回 | artifact 下载 |
| `start=N` | `raw[N:]` 字节切片后返回 | 增量打印（`run_mlu_ci.py`） |
| `tail=N` | `splitlines()[-N:]` | 终态尾行打印 |

#### 脱敏执行点

```
Jenkins console text
  → master.py sync_jenkins_log / drain_jenkins_log
    → redact_log(chunk)  ← 唯一脱敏点，在内网侧
      → post_log_chunk(slave)
        → slave <id>.log  ← 已脱敏
          → bridge get_log → GitHub Actions  ← 已脱敏
```

---

## 安全边界

```
允许                                       禁止
──────────────────────────────────────     ──────────────────────────────────
GitHub Actions → bridge.py (127.0.0.1)     GitHub Actions → Jenkins
bridge.py → slave.py (localhost)           bridge.py 持有凭据
master.py → slave.py（内网 → VPS）         外网 → Jenkins
master.py → Jenkins（仅内网）              外网 → MLU K8s
Jenkins → GitHub（clone）                  slave.py 直接暴露公网
Jenkins → 内部镜像仓库 / MLU K8s
```

- **Jenkins 凭据**（`SGLANG_JENKINS_USER` / `SGLANG_JENKINS_TOKEN`）仅在内网 master
  主机上通过环境变量注入，不出现在 git 仓库、配置文件或 VPS 上。
- **日志脱敏**（`sanitize.py`）在内网侧执行。设备序列号、UUID、固件版本、MLU 型号、
  内部主机名、Kubernetes pod 标识在到达 VPS 之前已被替换。
- **bridge.py** 校验 `repo` 字段是否在允许列表中（默认仅允许 `sglang`）。

---

## 网络端口与访问

| 端口 | 主机 | 服务 | 允许访问来源 |
|---|---|---|---|
| 14547 | VPS | bridge.py | 仅 VPS localhost（GitHub runner 同机） |
| 14548 | VPS | slave.py | 内网 master（通过云安全组限制来源 IP） |
| — | 内网 | master.py | 出站：VPS:14548 和 Jenkins |

---

## 配置参考

### VPS（`external_ci.conf`）

```ini
[BridgeServer]
host = "127.0.0.1"
port = "14547"
repo = "sglang"            # 允许的仓库名；空 = 不限制

[SlaveServer]
host = "127.0.0.1"         # bridge 连接此地址
bind_host = "0.0.0.0"      # slave 监听所有网卡
port = "14548"
db_path = "~/data/mlu_ci/sglang_tasks.json"
log_dir = "~/data/mlu_ci/logs"
terminal_retention_seconds = "86400"
active_task_timeout_seconds = "172800"
```

### 内网 Master（`internal_master.conf`）

```ini
[SlaveServer]
host = "8.222.226.16"
port = "14548"

[MasterServer]
jenkins_path = "jenkins.svc.cambricon.com/dist/job/SGLANG/job/DEBUG/job/sglang_ci/"
jenkins_user = ""           # 建议通过环境变量 SGLANG_JENKINS_USER 注入
jenkins_token = ""          # 建议通过环境变量 SGLANG_JENKINS_TOKEN 注入
jenkins_params = "repo;timestamp;pr_id;task_id;trigger_type;repo_url;git_ref;commit_sha"
```

---

## 任务数据模型

```python
@dataclass
class Task:
    # 身份标识（输入 make_task_id → MD5 哈希）
    timestamp: str          # 毫秒时间戳
    repo: str               # 仓库名
    pr_id: str              # GitHub PR 编号
    repo_url: str           # 仓库 URL
    git_ref: str            # 分支或 tag
    commit_sha: str         # 精确 commit
    trigger_type: str       # ci / pr / nightly 等
    trigger_id: str         # GitHub actor 或 workflow 名

    # 状态
    id: str                 # 身份字段的 MD5 哈希
    status: str             # running | waiting | working | success | *_fail | error | unstable
    inner_id: str           # Jenkins build 编号
    repeat_times: str

    # 日志簿记
    log: str                # 最近 N 行 Jenkins console（轻量预览）
    log_offset: str         # Jenkins progressiveText 游标
    log_status: str         # pending | syncing | complete | failed
    log_error: str          # 日志同步失败时的错误信息

    # 时间戳
    created_at: float
    updated_at: float
```

---

## GitHub Actions Workflow

### `pr-test-mlu.yml`

| 触发方式 | 行为 |
|---|---|
| `push` 到 `main` | 直接运行 CI |
| `pull_request` 到 `main` | 仅当 MLU 相关文件变化时运行 |
| `workflow_dispatch` | 手动触发，可指定 `ref`、`run_all_tests`、`trigger_type` |

**文件变更过滤：** `python/sglang/**`、`python/pyproject_mlu.toml`、
`test/**`、`scripts/ci/mlu/**`、`.github/workflows/pr-test-mlu.yml`

### `nightly-test-mlu.yml`

| 触发方式 | 行为 |
|---|---|
| `schedule`（cron `0 18 * * *`） | 每天北京时间 02:00 运行 |
| `pull_request`（仅 `nightly-test-mlu.yml` 本身变化） | 验证 workflow 改动 |
| `workflow_dispatch` | 手动触发，可指定 `ref`、`trigger_type` |

---

## 可调参数

| 常量 | 位置 | 默认值 | 用途 |
|---|---|---|---|
| `LOG_TAIL_LINES` | `vps/slave.py`、`internal/master.py` | 50 | `get_status` 中 `log` 字段保留的行数 |
| `LOG_DRAIN_MAX_CHUNKS` | `internal/master.py` | 100 | 排空 Jenkins 最终日志的最大迭代次数 |
| `LOG_DOWNLOAD_RETRIES` | `scripts/ci/mlu/run_mlu_ci.py` | 3 | 完整日志下载重试次数 |
| `POLL_INTERVAL` | `internal/master.py`、`run_mlu_ci.py` | 5–10 秒 | 轮询 slave / Jenkins 的间隔 |
