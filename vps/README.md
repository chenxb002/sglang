# VPS 模块部署说明

该目录部署在外网 VPS 或 DMZ 机器上，负责 GitHub Actions runner、任务入口 `bridge.py` 和任务状态服务 `slave.py`。

## 组件

- `bridge.py`：接收 GitHub Actions 提交的 CI 任务，按配置校验 repo 名，转发给 `slave.py`。
- `slave.py`：维护任务 JSON DB，供 GitHub Actions 查询状态，也供内网 master 拉取 active task。
- `common.py`：公共配置、JSON 响应、task id 生成等工具函数。
- `external_ci.conf`：bridge/slave 默认配置模板。
- `deploy_vps.sh`：无 sudo 场景的一键部署脚本。

## 默认端口

- `14547`：bridge，仅监听 `127.0.0.1`，给 VPS 本机 GitHub runner 访问。
- `14548`：slave，监听 `0.0.0.0`，给内网 master 访问；需要通过云安全组限制来源 IP。

## 无 sudo 自动部署

登录 VPS：

```bash
ssh <user>@<vps-ip>
```

把脚本仓拉到任意目录，然后进入当前目录执行即可。脚本不要求固定仓库目录名，也不要求目录必须叫 `vps`；运行文件以 `deploy_vps.sh` 所在目录为准。

```bash
cd <ci-script-dir>
./deploy_vps.sh
```

如果 bridge/slave 源文件不在脚本所在目录，可以显式指定：

```bash
SGLANG_CI_VPS_SOURCE_DIR='<path-containing-bridge-slave-common>' ./deploy_vps.sh
```

如果是首次注册 GitHub runner，需要先在 GitHub 页面获取 runner token，然后执行：

```bash
cd <ci-script-dir>
GITHUB_RUNNER_REPO_URL='https://github.com/<owner>/<repo>' \
GITHUB_RUNNER_TOKEN='<github-runner-token>' \
./deploy_vps.sh --runner-only
```

runner 需要使用 workflow 中配置的 label，当前默认是：

```text
cambricon
```

## 常用环境变量

- `GITHUB_RUNNER_REPO_URL`：GitHub runner 注册目标仓库 URL；不设置时脚本尝试从当前 git remote 推断。
- `GITHUB_RUNNER_TOKEN`：首次注册 runner 时使用的 token。
- `RUNNER_LABEL`：runner label，默认 `cambricon`。
- `RUNNER_NAME`：runner 名称，默认基于主机名生成。
- `BRIDGE_PORT`：bridge 端口，默认 `14547`。
- `SLAVE_PORT`：slave 端口，默认 `14548`。
- `ALLOWED_REPO`：bridge 接受的 repo 名，默认 `sglang`；设为空表示不限制。
- `SGLANG_CI_VPS_SOURCE_DIR`：bridge/slave 源文件目录，默认是 `deploy_vps.sh` 所在目录。
- `SGLANG_CI_DEPLOY_DIR`：bridge/slave 部署目录，默认 `~/data/mlu_ci/deploy`。
- `SGLANG_CI_DATA_DIR`：任务 DB 目录，默认 `~/data/mlu_ci`。
- Jenkins 完整运行日志按 task id 保存在任务 DB 目录下的 `logs/` 子目录。
- `GITHUB_RUNNER_DIR`：GitHub runner 安装目录，默认 `~/actions-runner`。

## 常用命令

查看帮助：

```bash
./deploy_vps.sh --help
```

查看状态：

```bash
./deploy_vps.sh --status
```

重启 bridge/slave：

```bash
./deploy_vps.sh --restart
```

停止服务：

```bash
./deploy_vps.sh --stop
```

查看日志：

```bash
tail -f ~/data/mlu_ci/deploy/logs/bridge.log
tail -f ~/data/mlu_ci/deploy/logs/slave.log
tail -f ~/actions-runner/runner.log
```

如果通过环境变量修改了部署目录或 runner 目录，请查看对应目录下的日志。

查询 active task：

```bash
curl -s "http://127.0.0.1:14548/source=master&aiming=get_data" | python3 -m json.tool
```

通过 bridge 查询任务状态：

```bash
TASK_ID='<task-id>'
curl -s "http://127.0.0.1:14547/aiming=get_status&id=${TASK_ID}" | python3 -m json.tool
```

其中 `status` 表示 CI 状态，`log_status` 表示完整 Jenkins 日志同步状态；`log_status=failed` 时可查看 `log_error` 判断日志 artifact 是否可能不完整。

下载完整 Jenkins 日志：

```bash
TASK_ID='<task-id>'
curl -s "http://127.0.0.1:14547/aiming=get_log&id=${TASK_ID}" -o "jenkins-${TASK_ID}.log"
```

只查看最后 300 行 Jenkins 日志：

```bash
TASK_ID='<task-id>'
curl -s "http://127.0.0.1:14547/aiming=get_log&id=${TASK_ID}&tail=300"
```

清理某个任务：

```bash
TASK_ID='<task-id>'
curl -s "http://127.0.0.1:14548/source=bridge&aiming=end_job&id=${TASK_ID}" | python3 -m json.tool
```
