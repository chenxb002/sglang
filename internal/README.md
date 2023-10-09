# Internal 模块部署说明

`internal` 部署在公司内网，负责从 VPS 拉取任务、触发 Jenkins、轮询 Jenkins build 结果，并将结果写回 VPS task DB。

## 组件

- `master.py`：内网调度器，周期性轮询 VPS `slave.py`。
- `common.py`：公共配置、URL 处理与环境变量读取工具函数。
- `internal_master.conf`：master 默认配置模板，不写入 Jenkins 凭据。
- `deploy_master.sh`：内网 master 自动部署脚本。
- `verify_chain.sh`：端到端链路验证脚本。
- `docs/`：整体方案、消息流图片和详细流程说明。

## 必需环境变量

Jenkins 凭据只在内网机器上设置：

```bash
export SGLANG_JENKINS_USER='<jenkins-user>'
export SGLANG_JENKINS_TOKEN='<jenkins-api-token>'
```

可选覆盖项：

```bash
export SLAVE_HOST='8.222.226.16'
export SLAVE_PORT='14548'
export SGLANG_JENKINS_PATH='jenkins.svc.cambricon.com/dist/job/SGLANG/job/DEBUG/job/sglang_ci/'
export POLL_INTERVAL='10'
```

## 自动部署

```bash
cd <ci-script-dir>
export SGLANG_JENKINS_USER='<jenkins-user>'
export SGLANG_JENKINS_TOKEN='<jenkins-api-token>'
./deploy_master.sh
```

脚本会将运行文件复制到：

```text
internal/deploy/
```

并在该目录下生成 `internal_master.conf` 和 `logs/master.log`。该目录已被 `.gitignore` 忽略。

## 常用命令

查看帮助：

```bash
cd <ci-script-dir>
./deploy_master.sh --help
```

查看状态：

```bash
cd <ci-script-dir>
./deploy_master.sh --status
```

重启 master：

```bash
cd <ci-script-dir>
./deploy_master.sh --restart
```

停止 master：

```bash
cd <ci-script-dir>
./deploy_master.sh --stop
```

单次调试运行：

```bash
cd <ci-script-dir>
./deploy_master.sh --once
```

查看日志：

```bash
tail -f <ci-script-dir>/deploy/logs/master.log
```

## 链路验证

快速检查 VPS slave 与 Jenkins 连通性：

```bash
cd <ci-script-dir>
./verify_chain.sh --quick
```

提交一个真实 smoke 任务：

```bash
cd <ci-script-dir>
export SGLANG_JENKINS_USER='<jenkins-user>'
export SGLANG_JENKINS_TOKEN='<jenkins-api-token>'
BRIDGE_HOST='8.222.226.16' ./verify_chain.sh --smoke
```

如果 `BRIDGE_PORT=14547` 只监听 VPS localhost，内网机器不能直接访问 bridge，可只通过 GitHub Actions 触发真实任务；内网侧主要验证 `slave.py` 与 Jenkins。
