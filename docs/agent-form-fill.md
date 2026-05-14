# Agent 表单填写 SSE 协议

当前公开业务接口只保留飞书表单填写的分段 SSE：服务先根据自然语言生成确认卡片，前端把 `payload` 渲染给用户；用户确认、修改、补充或闲聊后，再调用 input 端点开启下一段 SSE。

## CubeSandbox

当前 CubeSandbox 适配保持可用：

| 端口 | 用途 |
| --- | --- |
| `49983` | envd control plane，CubeSandbox readiness probe 使用 `/health` |
| `49999` | FastAPI HTTP/SSE 业务服务 |
| `60000` | MCP contract 辅助服务，可通过 `ENABLE_MCP=false` 关闭 |

CubeSandbox 代理地址示例：

```text
HTTP_BASE=http://<host>/api/v1/sandboxes/<sandbox_id>/proxy/49999
```

本地调试可以把业务端口临时改成 `50001`：

```bash
PORT=50001 ENABLE_MCP=true ./scripts/run_local.sh
```

如运行环境没有预置 LLM 配置，可先调用 `/v1/init` 注入；飞书登录态则通过 `/v1/auth/storage-state` 预置，服务会默认尝试使用 `FEISHU_DEFAULT_PROFILE_ID=feishu-default`。

## 端点

| 端点 | 说明 |
| --- | --- |
| `POST /v1/feishu/form-fill/run` | 创建 run，流到 `ask_user_question` 或最终态 |
| `POST /v1/feishu/form-fill/runs/{run_id}/input` | 回传用户确认、修改、补充或取消，并返回下一段 SSE |

## Run 请求

主协议只要求 `query`：

```json
{
  "query": "帮赵十四报名参加 2026/06/27 的活动，一共 8 人。"
}
```

## SSE 事件

| event | 说明 |
| --- | --- |
| `run_started` | run 已创建 |
| `phase_started` | 开始 `draft` 或 `submit` 阶段 |
| `ask_user_question` | 需要前端渲染确认 UI；本段 SSE 到这里结束 |
| `user_response_received` | 用户输入已被服务接收和理解 |
| `step_start` / `step_end` | browser-use 执行轨迹 |
| `run_completed` | 最终完成 |
| `run_failed` | 最终失败或超时 |
| `run_cancelled` | 用户取消 |
| `heartbeat` | 长时间无事件时保活 |

`ask_user_question` 示例：

```json
{
  "event": "ask_user_question",
  "run_id": "eb3b108e-c2b2-4e8d-b1b7-8f23c5a93805",
  "data": {
    "status": "awaiting_user",
    "question_id": "39d7f8f5-8d6d-4e84-bd0f-3f2f0f95f1a8",
    "input_url": "/v1/feishu/form-fill/runs/eb3b108e-c2b2-4e8d-b1b7-8f23c5a93805/input",
    "expires_at": 1778475009.8038566,
    "stream_closed": true,
    "payload": {
      "version": "goclaw.gateway.reply.v1",
      "kind": "ask_user",
      "title": "参会登记问卷信息确认",
      "text": "请确认以下信息是否提交。",
      "summary": [
        { "id": "name", "label": "姓名", "value": "赵十四" },
        { "id": "attendance_time", "label": "参会时间", "value": "2026-06-27" },
        { "id": "attendance_count", "label": "参会人数", "value": "8" }
      ],
      "questions": [
        {
          "header": "提交确认",
          "question": "这些信息是否正确？",
          "multiSelect": false,
          "options": [
            { "id": "confirm", "label": "确认提交", "description": "继续填写并提交表单" },
            { "id": "edit", "label": "我要修改", "description": "补充或更正信息后再确认" },
            { "id": "cancel", "label": "取消", "description": "停止本次操作" }
          ]
        }
      ],
      "fields": []
    }
  }
}
```

`stream_closed: true` 是连接生命周期提示：当前 SSE 段到这里会结束，前端应该渲染确认卡片并等待用户输入。用户输入后再调用 `input_url` 开启下一段 SSE。

## Input 请求

Input 主协议只要求 `question_id` 和 `content`。自然语言回复放在 `content.text`；按钮语义或结构化修改放在 `content.decision`、`content.fields`、`content.supplement`。

自然语言确认：

```json
{
  "question_id": "39d7f8f5-8d6d-4e84-bd0f-3f2f0f95f1a8",
  "content": {
    "text": "没问题"
  }
}
```

按钮确认：

```json
{
  "question_id": "39d7f8f5-8d6d-4e84-bd0f-3f2f0f95f1a8",
  "content": {
    "decision": "confirm"
  }
}
```

自然语言修改或补充会交给模型结合当前 `payload.summary` 理解：

```json
{
  "question_id": "39d7f8f5-8d6d-4e84-bd0f-3f2f0f95f1a8",
  "content": {
    "text": "人数改成 6，其他没问题"
  }
}
```

前端也可以直接提交结构化字段修改：

```json
{
  "question_id": "39d7f8f5-8d6d-4e84-bd0f-3f2f0f95f1a8",
  "content": {
    "decision": "edit",
    "fields": {
      "attendance_count": "6"
    }
  }
}
```

换一个 case，重新生成确认草稿：

```json
{
  "question_id": "39d7f8f5-8d6d-4e84-bd0f-3f2f0f95f1a8",
  "content": {
    "text": "换一个请假 case：请帮王小明报名 2026/06/03 的活动，人数 3 人。"
  }
}
```

取消：

```json
{
  "question_id": "39d7f8f5-8d6d-4e84-bd0f-3f2f0f95f1a8",
  "content": {
    "decision": "cancel"
  }
}
```

跑题或不确定输入不会触发提交，服务会返回新的 `ask_user_question` 继续让用户确认。

## Curl 示例

本地：

```bash
HTTP_BASE=http://127.0.0.1:50001
```

CubeSandbox：

```bash
HTTP_BASE=http://<host>/api/v1/sandboxes/<sandbox_id>/proxy/49999
```

开始 run：

```bash
curl --no-buffer -sS -X POST "$HTTP_BASE/v1/feishu/form-fill/run" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "帮赵十四报名参加 2026/06/27 的活动，一共 8 人。"
  }'
```

正常提交：

```bash
curl --no-buffer -sS -X POST "$HTTP_BASE/v1/feishu/form-fill/runs/<run_id>/input" \
  -H 'Content-Type: application/json' \
  -d '{
    "question_id": "<question_id>",
    "content": {"text": "没问题"}
  }'
```

补充信息：

```bash
curl --no-buffer -sS -X POST "$HTTP_BASE/v1/feishu/form-fill/runs/<run_id>/input" \
  -H 'Content-Type: application/json' \
  -d '{
    "question_id": "<question_id>",
    "content": {"text": "人数改成 6，其他没问题"}
  }'
```

换一个 case：

```bash
curl --no-buffer -sS -X POST "$HTTP_BASE/v1/feishu/form-fill/runs/<run_id>/input" \
  -H 'Content-Type: application/json' \
  -d '{
    "question_id": "<question_id>",
    "content": {"text": "换一个请假 case：请帮王小明报名 2026/06/03 的活动，人数 3 人。"}
  }'
```

直接闲聊：

```bash
curl --no-buffer -sS -X POST "$HTTP_BASE/v1/feishu/form-fill/runs/<run_id>/input" \
  -H 'Content-Type: application/json' \
  -d '{
    "question_id": "<question_id>",
    "content": {"text": "今天天气不错"}
  }'
```

## 判断成功

最终以 `run_completed` 为准：

```text
run_completed.data.success === true
```

最终结果会保留 `duration_sec`、`steps`、`history_excerpt`、`screenshots` 等 browser-use 轨迹信息，便于回归和排查。
