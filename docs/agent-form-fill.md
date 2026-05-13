# Agent 表单填写功能接入说明

本文描述 `browser-use-cubesandbox-agent` 的飞书预置问卷填写能力。假设调用方已经拿到一个可访问的 HTTP 主域名，例如：

```text
HTTP_BASE=http://<host>/api/v1/sandboxes/<sandbox_id>/proxy/49999
```

服务支持两阶段表单填写：

1. 文本解析阶段：把用户自然语言解析成表单字段草稿，返回 `draft_session_id`，停下来等待人工确认。
2. 确认提交阶段：人工确认或修正字段后，带着 `draft_session_id` 让浏览器 Agent 打开表单、填写并提交。

当前内置预置问卷是“参会登记问卷”，默认表单地址为：

```text
https://lexmount.feishu.cn/share/base/form/shrcnMEX6kDGkDArxCLgnsIWR8f
```

默认字段固定为：

| 字段 key | 表单字段 | 类型 | 说明 |
| --- | --- | --- | --- |
| `name` | 姓名 | string | 从自然语言中抽取参会人姓名 |
| `attendance_time` | 参会时间 | timestamp_ms | 先归一化成 `YYYY-MM-DD`，提交时转换成毫秒时间戳 |
| `attendance_count` | 参会人数 | number | 从“几人/几个人”等表达抽取数字 |

## 前置条件

调用业务接口前建议先检查服务状态：

```bash
curl -sS "$HTTP_BASE/healthz"
```

`status` 为 `ok` 表示运行时配置和可写目录检查通过。如果返回 `needs_init`，需要先调用 `/v1/init` 注入 LLM 配置。

```bash
curl -sS -X POST "$HTTP_BASE/v1/init" \
  -H 'Content-Type: application/json' \
  -d '{
    "config": {
      "LLM_BASE_URL": "https://example.com/v1",
      "LLM_API_KEY": "sk-xxx",
      "LLM_MODEL": "gpt-5.4-mini"
    }
  }'
```

`/v1/init` 支持 env 风格字段，也支持大小写不敏感的 key 归一化。`OPENAI_API_KEY`、`LLM_API_KEY`、`api_key` 等会被归一到实际的 `llm_api_key`。

## HTTP 调用方式

### 阶段 1：解析文本并生成草稿

Endpoint：

```text
POST /v1/feishu/form-fill/prepare
```

请求 body：

```json
{
  "query": "我叫张三，5月8号参会，3个人参加。",
  "llm": {
    "base_url": "https://example.com/v1",
    "api_key": "sk-xxx",
    "model": "gpt-5.4-mini",
    "temperature": 0
  }
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `query` | 是 | 用户自然语言描述，用于解析表单字段 |
| `llm` | 否 | 单次调用的 LLM 覆盖配置；通常可省略，使用 `/v1/init` 或环境变量里的配置 |

curl 示例：

```bash
curl -sS -X POST "$HTTP_BASE/v1/feishu/form-fill/prepare" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "我叫张三，5月8号参会，3个人参加。"
  }'
```

典型响应：

```json
{
  "run_id": "51b8d87e-4767-4837-98d5-39b7cf6df058",
  "success": false,
  "mode": "feishu_form_fill",
  "form_url": "https://lexmount.feishu.cn/share/base/form/shrcnMEX6kDGkDArxCLgnsIWR8f",
  "form_name": "参会登记问卷",
  "awaiting_human_confirmation": true,
  "draft_session_id": "8da6a3c7-a79f-41c3-b381-885310a6db8c",
  "draft_session_expires_at": 1778475009.8038566,
  "payload": {
    "version": "goclaw.gateway.reply.v1",
    "kind": "ask_user",
    "title": "参会登记问卷信息确认",
    "text": "请确认以下信息是否提交。",
    "summary": [
      { "id": "name", "label": "姓名", "value": "张三" },
      { "id": "attendance_time", "label": "参会时间", "value": "2026-05-08" },
      { "id": "attendance_count", "label": "参会人数", "value": "3" }
    ],
    "questions": [
      {
        "header": "提交确认",
        "question": "这些信息是否正确？",
        "multiSelect": false,
        "options": [
          {
            "id": "confirm",
            "label": "确认提交",
            "description": "继续填写并提交表单"
          },
          {
            "id": "edit",
            "label": "我要修改",
            "description": "补充或更正信息后再确认"
          },
          {
            "id": "cancel",
            "label": "取消",
            "description": "停止本次操作"
          }
        ]
      }
    ],
    "fields": []
  },
  "errors": [],
  "notes": []
}
```

注意：

- 阶段 1 会停在人工确认点，所以 `success: false` 不等于失败。判断是否正常进入确认态应看 `awaiting_human_confirmation: true`、`draft_session_id` 非空、`errors` 为空。
- `payload` 是给前端或 gateway 渲染确认 UI 的结构化载荷。`draft_session_id`、`awaiting_human_confirmation`、`draft_session_expires_at` 等执行状态仍保留在响应最外层，避免污染 UI 协议。
- 接入方应渲染 `payload`，不要依赖内部草稿结构。原始草稿会保留在服务内部用于阶段 2 校验和合并人工修正。
- `draft_session_id` 是阶段 2 的绑定 token，默认 TTL 是 1800 秒，且提交后会被消费。
- 如果用户在确认阶段补充了新的自然语言信息，例如“再加一句其实是 4 个人”，不要直接 submit。应把补充后的完整文本重新调用 prepare，生成新的 draft。

### 人工确认或修正

调用方应把 `payload` 展示给用户确认。用户可以：

- 直接确认：阶段 2 只传 `draft_session_id`。
- 修正字段：阶段 2 传 `confirmed_answers` 覆盖某些字段。
- 取消：不调用阶段 2。

`confirmed_answers` 每个元素至少需要一个定位字段：`index`、`field_key`、`field_label` 三选一；同时需要提供 `confirmed_value`、`normalized_values` 或 `clear_value: true` 之一。

### 阶段 2：确认后提交表单

Endpoint：

```text
POST /v1/feishu/form-fill/submit
```

最小请求 body：

```json
{
  "draft_session_id": "8da6a3c7-a79f-41c3-b381-885310a6db8c",
  "timeout_sec": 900,
  "max_steps": 60,
  "use_vision": "auto"
}
```

带人工修正的请求 body：

```json
{
  "draft_session_id": "8da6a3c7-a79f-41c3-b381-885310a6db8c",
  "human_confirmation_notes": "用户已确认，人数修正为 4。",
  "confirmed_answers": [
    {
      "field_key": "attendance_count",
      "field_label": "参会人数",
      "confirmed_value": "4"
    }
  ],
  "timeout_sec": 900,
  "max_steps": 60,
  "use_vision": "auto"
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `draft_session_id` | 是 | 阶段 1 返回的原始 ID，必须原样传回 |
| `human_confirmation_notes` | 否 | 人工确认说明。若包含新的业务数据，建议重新 prepare |
| `confirmed_answers` | 否 | 对阶段 1 草稿字段的人工覆盖 |
| `field_ids` | 否 | 非预置或重建表单时覆盖飞书字段 ID；预置表单可省略 |
| `allowed_domains` | 否 | 浏览器允许访问的域名限制；省略时使用服务默认 |
| `headless` | 否 | 是否无头浏览器运行 |
| `max_steps` | 否 | 浏览器 Agent 最大步数，默认 35，最大 120 |
| `timeout_sec` | 否 | 调用超时，默认 900，最大 3600。sandbox 代理超时也应大于该值 |
| `use_vision` | 否 | `auto` / `always` / `never` |
| `llm` | 否 | 单次调用 LLM 覆盖配置 |
| `auth` | 否 | 浏览器登录态配置，私有飞书表单需要传 profile 或 storage state |

curl 示例：

```bash
curl -sS -X POST "$HTTP_BASE/v1/feishu/form-fill/submit" \
  -H 'Content-Type: application/json' \
  -d '{
    "draft_session_id": "8da6a3c7-a79f-41c3-b381-885310a6db8c",
    "timeout_sec": 900,
    "max_steps": 60,
    "use_vision": "auto"
  }'
```

典型成功响应：

```json
{
  "run_id": "51b8d87e-4767-4837-98d5-39b7cf6df058",
  "success": true,
  "mode": "feishu_form_fill",
  "final_text": "Submitted",
  "form_url": "https://lexmount.feishu.cn/share/base/form/shrcnMEX6kDGkDArxCLgnsIWR8f",
  "submission_result": "Submitted",
  "current_url": "https://lexmount.feishu.cn/share/base/form/shrcnMEX6kDGkDArxCLgnsIWR8f",
  "steps": 7,
  "duration_sec": 52.246,
  "errors": [],
  "notes": [],
  "history_excerpt": [
    "Typed '张三'",
    "Typed '2026/05/08'",
    "Typed '3'",
    "Clicked button \"Submit\""
  ]
}
```

阶段 2 的判断建议：

- `success: true` 是最终提交成功的主信号。
- `submission_result` 或 `final_text` 通常会是 `Submitted`。
- `current_url` / `form_url` 是这次操作的表单地址。
- `errors` 可能包含中间 Agent 输出解析告警；如果 `success: true`，通常可以作为非致命日志记录，但不应覆盖成功状态。

## MCP 调用方式

MCP 走独立端口，默认是 `60000`。如果 HTTP 主域名是：

```text
HTTP_BASE=http://<host>/api/v1/sandboxes/<sandbox_id>/proxy/49999
```

则 MCP URL 通常是：

```text
MCP_URL=http://<host>/api/v1/sandboxes/<sandbox_id>/proxy/60000/mcp
```

MCP server 使用 streamable HTTP。请求头建议固定为：

```http
Content-Type: application/json
Accept: application/json, text/event-stream
```

响应是 SSE，事件一般形如：

```text
event: message
data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{...tool result json...}"}]}}
```

也就是说，实际工具结果在 JSON-RPC `result.content[0].text` 中，且这个 `text` 本身是一段 JSON 字符串，需要再解析一次。

### 查看工具列表

```bash
curl -sS -N -X POST "$MCP_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {}
  }'
```

表单填写相关工具：

| MCP 工具名 | 对应 HTTP endpoint | 说明 |
| --- | --- | --- |
| `feishu_form_fill_prepare` | `/v1/feishu/form-fill/prepare` | 阶段 1：自然语言解析并返回待确认草稿 |
| `feishu_form_fill_submit` | `/v1/feishu/form-fill/submit` | 阶段 2：带 `draft_session_id` 填写并提交表单 |

### MCP 阶段 1：prepare

```bash
curl -sS -N -X POST "$MCP_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "feishu_form_fill_prepare",
      "arguments": {
        "query": "我叫李四，5月9号参会，2个人参加。"
      }
    }
  }'
```

MCP prepare 的 `arguments` schema：

```json
{
  "query": "string, required",
  "llm": {
    "base_url": "string, optional",
    "api_key": "string, optional",
    "model": "string, optional",
    "temperature": "number, optional"
  }
}
```

返回内容与 HTTP prepare 基本一致，重点读取：

- `awaiting_human_confirmation`
- `draft_session_id`
- `draft_session_expires_at`
- `payload`
- `errors`

### MCP 阶段 2：submit

```bash
curl -sS -N -X POST "$MCP_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "feishu_form_fill_submit",
      "arguments": {
        "draft_session_id": "8da6a3c7-a79f-41c3-b381-885310a6db8c",
        "timeout_sec": 900,
        "max_steps": 60,
        "use_vision": "auto"
      }
    }
  }'
```

MCP submit 的 `arguments` schema：

```json
{
  "draft_session_id": "string, required",
  "human_confirmation_notes": "string, optional",
  "confirmed_answers": [
    {
      "index": "integer, optional",
      "field_key": "string, optional",
      "field_label": "string, optional",
      "confirmed_value": "string, optional",
      "normalized_values": ["string"],
      "clear_value": "boolean, optional"
    }
  ],
  "field_ids": {
    "name": "string, optional",
    "attendance_time": "string, optional",
    "attendance_count": "string, optional"
  },
  "allowed_domains": ["string"],
  "headless": "boolean, optional",
  "max_steps": "integer, default 35",
  "timeout_sec": "integer, default 900",
  "use_vision": "auto | always | never",
  "llm": {
    "base_url": "string, optional",
    "api_key": "string, optional",
    "model": "string, optional",
    "temperature": "number, optional"
  },
  "auth": {
    "profile_id": "string, optional",
    "storage_state": "object, optional",
    "storage_state_path": "string, optional",
    "sensitive_data": "object, optional"
  }
}
```

返回内容与 HTTP submit 基本一致，重点读取：

- `success`
- `final_text`
- `submission_result`
- `current_url`
- `steps`
- `duration_sec`
- `errors`
- `history_excerpt`

## 推荐客户端流程

伪代码：

```text
healthz()
if status == "needs_init":
    init(llm config)

prepare_response = prepare(user_text)
if prepare_response.awaiting_human_confirmation != true:
    show_error(prepare_response.errors)
    stop

show payload to human

if human provides new facts in natural language:
    prepare again with updated full text
else:
    submit_response = submit(
        draft_session_id = prepare_response.draft_session_id,
        confirmed_answers = optional human corrections
    )

if submit_response.success:
    show submitted state and form_url/current_url
else:
    show errors and history_excerpt
```

## 常见错误和处理

| 现象 | 可能原因 | 处理方式 |
| --- | --- | --- |
| `/healthz` 返回 `needs_init` | LLM 配置未注入 | 调 `/v1/init` 注入 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` |
| prepare 返回 `success: false` | 正常停在人工确认阶段 | 只要 `awaiting_human_confirmation: true` 且有 `draft_session_id`，即可进入确认流程 |
| submit 返回 draft 不存在或已消费 | `draft_session_id` 过期、写错或已经提交过 | 重新调用 prepare 获取新的 draft |
| submit 被代理 502/超时 | sandbox 或外层代理超时小于浏览器执行耗时 | 将外层超时调到大于 `timeout_sec`，建议至少 900 秒 |
| 表单需要登录 | 缺少飞书登录态 | 通过 `auth.profile_id`、`auth.storage_state` 或预置 auth profile 提供登录态 |
| submit 成功但 `errors` 非空 | 中间 Agent 输出解析告警 | 以 `success: true` 为最终成功信号，同时记录 `errors` 便于排查 |

## 接入约束

- 阶段 2 必须使用阶段 1 返回的同一个 `draft_session_id`。
- `draft_session_id` 是一次性的，提交成功或失败后都可能被消费。
- 用户有新的业务信息时，应重新跑阶段 1，而不是把新信息塞进 `human_confirmation_notes`。
- 接入方应该展示 `payload` 给用户确认，避免直接把解析结果提交到真实表单。
- 当前预置问卷字段是固定三字段；如果表单字段 ID 被重建，可以通过 submit 的 `field_ids` 覆盖。
