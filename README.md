# Browser Use CubeSandbox Agent

这是一个面向 CubeSandbox 部署的飞书表单填写服务。当前公开业务能力已收敛为一个 HTTP SSE 长链：服务先从自然语言生成待确认的表单填写 payload，前端渲染给用户确认或补充，确认后同一条 run 继续打开浏览器填写并提交。

## 本地启动

```bash
PORT=50001 ENABLE_MCP=true ./scripts/run_local.sh
```

健康检查：

```bash
curl -sS http://127.0.0.1:50001/healthz
```

如运行环境没有预置 LLM 配置，可用 `/v1/init` 注入：

```bash
curl -sS -X POST http://127.0.0.1:50001/v1/init \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "LLM_BASE_URL": "https://example.com/v1",
      "LLM_API_KEY": "sk-xxx",
      "LLM_MODEL": "gpt-5.4-mini"
    }
  }'
```

## HTTP SSE 协议

| 端点 | 说明 |
| --- | --- |
| `POST /v1/feishu/form-fill/run` | 创建 run，流到 `ask_user_question` 后结束本段 SSE |
| `POST /v1/feishu/form-fill/runs/{run_id}/input` | 回传用户确认、修改、补充或取消，并返回下一段 SSE |

打开 SSE：

```bash
curl -N -sS -X POST http://127.0.0.1:50001/v1/feishu/form-fill/run \
  -H "Content-Type: application/json" \
  -d '{
    "query": "帮赵十四报名参加 2026/05/27 的活动，一共 8 人。"
  }'
```

当事件流返回 `ask_user_question`，前端渲染其中的 `payload`，本段 SSE 会结束。用户操作后把同一个事件里的 `question_id` 回传给 input 端点，input 端点会返回下一段 SSE。

确认提交：

```bash
curl --no-buffer -sS -X POST http://127.0.0.1:50001/v1/feishu/form-fill/runs/<run_id>/input \
  -H "Content-Type: application/json" \
  -d '{
    "question_id": "<question_id>",
    "content": {
      "text": "没问题"
    }
  }'
```

修改字段：

```bash
curl --no-buffer -sS -X POST http://127.0.0.1:50001/v1/feishu/form-fill/runs/<run_id>/input \
  -H "Content-Type: application/json" \
  -d '{
    "question_id": "<question_id>",
    "content": {
      "decision": "edit",
      "fields": {
        "name": "李十五",
        "attendance_time": "2026/05/28",
        "attendance_count": "6"
      }
    }
  }'
```

补充自然语言并重新生成确认草稿：

```bash
curl --no-buffer -sS -X POST http://127.0.0.1:50001/v1/feishu/form-fill/runs/<run_id>/input \
  -H "Content-Type: application/json" \
  -d '{
    "question_id": "<question_id>",
    "content": {
      "decision": "edit",
      "supplement": "换一个请假 case：请帮王小明报名 2026/06/03 的活动，人数 3 人。"
    }
  }'
```

取消：

```bash
curl --no-buffer -sS -X POST http://127.0.0.1:50001/v1/feishu/form-fill/runs/<run_id>/input \
  -H "Content-Type: application/json" \
  -d '{
    "question_id": "<question_id>",
    "content": {
      "decision": "cancel"
    }
  }'
```

最终以 `run_completed.data.success === true` 判断提交成功。最终结果会保留 `duration_sec`、`steps`、`history_excerpt`、`screenshots` 等 browser-use 轨迹信息，便于回归和排查。

更完整的协议说明见 [docs/agent-form-fill.md](docs/agent-form-fill.md)。

## 认证 Profile

私有飞书表单需要浏览器登录态。可以保存 Playwright `storage_state` 为服务端 profile：

```bash
curl -sS -X POST http://127.0.0.1:50001/v1/auth/storage-state \
  -H "Content-Type: application/json" \
  -d @examples/feishu-profile.sample.json
```

业务请求里传：

```json
{
  "auth": {
    "profile_id": "feishu-default"
  }
}
```

## CubeSandbox

服务默认监听 `PORT`，镜像内会同时带上 Browser Use、Playwright Chromium、CubeSandbox envd。常用环境变量：

| 变量 | 说明 |
| --- | --- |
| `PORT` | FastAPI 服务端口 |
| `ENABLE_MCP` | 是否启动 MCP 辅助服务 |
| `MCP_PORT` | MCP 辅助服务端口 |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | LLM 配置 |
| `BROWSER_START_TIMEOUT_SEC` | browser-use 启动浏览器 watchdog 超时，默认 120 秒 |
| `FEISHU_DEFAULT_PROFILE_ID` | 默认飞书 profile |
