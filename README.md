# SGLang MLU CI/CD Scripts

该仓库存放 SGLang MLU 后端 CI/CD 的完整部署方案：VPS 外网网关、内网 Jenkins 调度器、Jenkins Pipeline。

> **架构文档**：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 模块交互、消息流、API 定义、安全边界。

当前方案按部署边界拆成三个模块目录：

```text
./
  jenkins/   # Jenkins job pipeline 与 Jenkins 侧说明
  internal/  # 公司内网 master 调度器、链路验证脚本与整体方案文档
  vps/       # VPS 上的 GitHub runner 辅助脚本、bridge/slave 与任务队列
```

## 模块职责

1. `vps/`
   - 部署在外网 VPS 或 DMZ 机器上。
   - 运行 GitHub self-hosted runner，runner label 为 `cambricon`。
   - 运行 `bridge.py` 接收 GitHub Actions 提交的任务。
   - 运行 `slave.py` 保存任务状态，并开放给内网 master 主动轮询。
   - 不保存 Jenkins 地址以外的内部凭据，不访问 Jenkins。

2. `internal/`
   - 部署在公司内网机器上。
   - 运行 `master.py`，主动访问 VPS `slave.py` 拉取 active task。
   - 使用 Jenkins user/token 触发 Jenkins job，并轮询 Jenkins build 结果。
   - 将 Jenkins build id、成功/失败状态写回 VPS `slave.py`。

3. `jenkins/`
   - 保存 Jenkins pipeline 文件。
   - Jenkins job 使用该 pipeline 申请 MLU 资源、拉起测试镜像、clone 指定 GitHub 源并执行 SGLang MLU 测试。

## 消息流

```text
GitHub Actions job
  -> VPS runner 本机 http://127.0.0.1:14547/bridge.py
  -> VPS slave.py 写入任务 DB
  <- internal/master.py 主动轮询 VPS slave.py
internal/master.py
  -> Jenkins buildWithParameters
Jenkins pipeline
  -> MLU 测试环境执行测试
internal/master.py
  -> VPS slave.py 写回结果
GitHub Actions job
  -> 轮询 bridge.py 获取终态并决定 job 成功/失败
```

## 部署入口

- VPS 侧：`vps/deploy_vps.sh`
- 内网侧：`internal/deploy_master.sh`
- Jenkins 侧：`jenkins/jenkins_sglang.pipeline`
- 端到端验证：`internal/verify_chain.sh`

详细部署步骤请分别查看：

- `vps/README.md`
- `internal/README.md`
- `jenkins/README.md`

## 安全边界

- Jenkins、内部镜像仓库、MLU 集群只在内网可见。
- 外网 VPS 只承担任务网关和 GitHub runner 功能。
- VPS 的 `14547` 只应监听 `127.0.0.1`，仅供本机 GitHub runner 调用。
- VPS 的 `14548` 需要被内网 master 访问，应通过云安全组限制来源 IP。
- Jenkins user/token 只在内网 master 机器上通过环境变量注入，不写入 Git 仓库。
