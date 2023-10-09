# Jenkins 模块说明

`jenkins` 保存 Jenkins 侧 pipeline 文件，用于创建或更新 SGLang MLU CI Jenkins job。

## 文件

- `jenkins_sglang.pipeline`：SGLang MLU CI Jenkins pipeline。

## Jenkins job 要求

建议 job 路径：

```text
http://jenkins.svc.cambricon.com/dist/job/SGLANG/job/DEBUG/job/sglang_ci/
```

job 需要支持 `buildWithParameters`，参数名需与 internal master 配置一致：

```text
repo
timestamp
pr_id
task_id
trigger_type
repo_url
git_ref
commit_sha
```

## 镜像

SGLang MLU CI 使用的测试镜像：

```text
yellow.hub.cambricon.com/cambricon_pytorch_container/cambricon_pytorch_container:v26.04.0-torch2.11.0-torchmlu1.32.2-ubuntu22.04-py310
```

## 部署方式

1. 在 Jenkins 中创建或打开对应 Pipeline job。
2. 将 `jenkins_sglang.pipeline` 内容配置为 Pipeline script，或配置为从 SCM 读取该文件。
3. 确认 Jenkins 用户具备该 job 的 `Read` 和 `Build` 权限。
4. 使用内网 master 的 `deploy_master.sh --status` 验证 Jenkins API 返回 `200`。

## 执行职责

Jenkins pipeline 负责：

- 接收 master 传入的 GitHub 仓库、分支/commit、task id 等参数。
- clone 指定代码版本。
- 拉起 MLU 测试环境和镜像。
- 安装 SGLang MLU 依赖。
- 执行 MLU 测试脚本。
- 将 Jenkins build result 暴露给 master 轮询。
