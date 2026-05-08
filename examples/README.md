# Agent Run 示例

本目录包含调用 `/v1/agent/run` 接口的请求示例。

## 通用模式 (general mode)

用于自由度较高的浏览器自动化任务，不需要特定的网站或业务流程。

### 请求示例

```bash
curl -X POST http://127.0.0.1:49999/v1/agent/run \
  -H "Content-Type: application/json" \
  -d @examples/general-chat.sample.json
```

### 请求字段说明

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `query` | string | 必填 | 自然语言指令 |
| `mode` | string | "general" | 执行模式：`general` 或 `feishu_bitable_to_form` |
| `start_url` | string | null | 起始 URL（可选） |
| `allowed_domains` | array | [] | 允许访问的域名白名单 |
| `max_steps` | int | 35 | 最大执行步数（1-120） |
| `timeout_sec` | int | 600 | 超时时间（30-3600秒） |
| `use_vision` | string | "auto" | 视觉模式：`auto`/`always`/`never` |

### 带认证的请求

如果目标网站需要登录，使用 `auth` 字段：

```json
{
  "query": "帮我查看 Gmail 收件箱里的未读邮件数量",
  "mode": "general",
  "start_url": "https://mail.google.com",
  "auth": {
    "profile_id": "my-gmail-session"
  }
}
```

### SSE 流式输出

使用 `/v1/agent/stream` 获取实时进度：

```bash
curl -N -X POST http://127.0.0.1:49999/v1/agent/stream \
  -H "Content-Type: application/json" \
  -d @examples/general-chat.sample.json
```

## 飞书模式 (feishu_bitable_to_form)

专用于飞书多维表格转问卷的流程，支持两阶段确认机制。

见 [feishu-run.sample.json](./feishu-run.sample.json) 和 [feishu-run.confirm.sample.json](./feishu-run.confirm.sample.json)。
