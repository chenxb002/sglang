# SGLang MLU CI 消息流示意图

本文用一张静态图片展示 SGLang MLU CI 中 GitHub Actions、Runner/VPS、Master、Jenkins 之间的通信关系和消息流。图片不依赖 Mermaid 渲染，可以直接在 Markdown 预览、浏览器或 PPT 中使用。

![SGLang MLU CI 消息流](./mlu_ci_message_flow_zh.svg)

## 消息流说明

| 序号 | 方向 | 消息 | 说明 |
| --- | --- | --- | --- |
| A | GitHub Actions -> bridge.py | `POST /` | 提交 CI 任务，包含 `repo_url`、`git_ref`、`commit_sha`、`trigger_type` |
| B | bridge.py -> slave.py | `POST /` | bridge 将任务转发给 slave，slave 生成并保存 `task_id` |
| C | master.py -> slave.py | `GET /source=master&aiming=get_data` | 内网 master 主动拉取 active task |
| D | master.py -> Jenkins | `POST buildWithParameters` | 触发 Jenkins job，传入代码版本和 `task_id` |
| E | Jenkins Job -> Pipeline | 启动 pipeline | Jenkins 按 `jenkins_sglang.pipeline` 执行 CI 流程 |
| F | Pipeline -> MLU Pod | 容器调度和测试 | 拉起 MLU 容器，安装依赖，执行 `test/run_suite.py --hw mlu` |
| G | master.py -> slave.py | `POST /` | 写回 `working`、`success`、`*_fail`、`error` 等状态 |
| H | GitHub Actions -> bridge.py | `GET get_status` / `GET end_job` | GitHub Actions 轮询最终状态并清理任务 |

## 通信边界

- GitHub Actions 只访问外网/DMZ 的 `bridge.py`。
- `bridge.py` 只和 `slave.py` 通信，不访问 Jenkins。
- `slave.py` 保存任务和状态，不保存 Jenkins 凭据。
- `master.py` 部署在内网，主动访问 `slave.py` 并触发 Jenkins。
- Jenkins 和 MLU 资源只在内网访问，不暴露给 GitHub Actions。

## 一句话总结

```text
GitHub Actions 把任务交给外网 Runner/VPS；Runner/VPS 只保存任务和状态；
内网 master 主动拉任务并触发 Jenkins；Jenkins 在内网 MLU 环境完成测试；
结果再沿 slave/bridge 回传给 GitHub Actions。
```
