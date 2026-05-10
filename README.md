# Browser Use CubeSandbox Agent

这是一个可以直接在 CubeSandbox 里构建为 template 的浏览器 Agent 项目，目标是把 `browser-use` 封装成标准 HTTP 服务，并同时提供普通 JSON 返回和 SSE 流式返回。

项目包含几个重点能力：

1. 通用浏览器 Agent：传入 `query`，让 Agent 操作浏览器并返回最终结果。
2. Feishu Showcase：把飞书多维表格转换为问卷，并返回最终问卷链接。
3. Feishu 预置问卷填写：服务内置固定飞书问卷链接和固定字段 schema（姓名、参会时间、参会人数），调用方只需传自然语言描述，系统先解析确认，再正式提交。
4. Feishu 登录态注入：支持把 Playwright `storage_state` 保存成服务端 profile 并复用。

## 1. 设计取舍

### 为什么同时提供 `/run` 和 `/stream`

- `/v1/agent/run` 适合 MCP tool、后端服务编排、同步调用。
- `/v1/agent/stream` 适合长耗时操作的实时进度展示。
- 两者底层复用同一套执行逻辑，避免两套行为不一致。

### 为什么镜像不直接基于 `cubesandbox-base`

`browser-use` 当前官方仓库在 GitHub README 中写明需要 Python `>=3.11`，而 CubeSandbox 官方 `bring-your-own-image` 文档给出的 `cubesandbox-base` 示例是 `ubuntu:22.04` 体系。为了避免自己再额外装 Python 3.11，本项目选择：

- 业务镜像基于 `python:3.11-bookworm@sha256:2209d186b561bf8a8298f86e82f2d79cb45fb3b42e89b1e3b2e25329f87d8401`
- 再按 CubeSandbox 官方文档的推荐方式，从 `ghcr.io/tencentcloud/cubesandbox-base@sha256:b83eee5be295b042560229b571e933ec785f055d4b6ecaca795b5c89ba0acd0a` 注入 `envd` 和 `cube-entrypoint.sh`

这样更稳，也更贴近 `browser-use` 的运行要求。

这里的两个镜像都故意固定到完整 digest，而不是只写 tag。`python:3.11-bookworm` 是可变 tag，2026-05-08 可命中缓存的 index digest 是 `sha256:2209d186b561bf8a8298f86e82f2d79cb45fb3b42e89b1e3b2e25329f87d8401`（`linux/amd64` 子 manifest 为 `sha256:45003752429b5d332df28574833d70edce02cdadc617080e77ad2a622b8e2e29`），而 2026-05-10 该 tag 已经漂移到 `sha256:99f4240b31e5b9cf1e792420390b0d531022af3922b3d15681e7b818ca63fe37`。如果不 pin digest，BuildKit 会把 `RUN apt-get ...`、`pip install`、`playwright install` 等后续层都挂到新的 parent rootfs 下，旧的重缓存层就无法复用。`cubesandbox-base` 也固定到 `linux/amd64` 子 manifest，匹配 `agent.build.yaml` 里的 `platform: linux/amd64`。升级基础镜像时应该显式改 digest，并预期会触发一次完整重建。

## 2. 目录结构

```text
browser-use-cubesandbox-agent/
├── .env.example
├── .gitignore
├── Dockerfile
├── README.md
├── agent.build.yaml
├── app
│   ├── __init__.py
│   ├── config.py
│   ├── main.py
│   ├── models.py
│   ├── prompts.py
│   ├── service.py
│   └── sse.py
├── mcp_server
│   ├── __init__.py
│   ├── client_example.py
│   └── server.py
├── pyproject.toml
└── schemas
    ├── mcp.browser_agent_run.input.schema.json
    ├── mcp.feishu_bitable_draft_form.input.schema.json
    ├── mcp.feishu_form_fill_prepare.input.schema.json
    ├── mcp.feishu_form_fill_submit.input.schema.json
    ├── mcp.feishu_bitable_publish_form.input.schema.json
    └── mcp.tools.catalog.json
```

## 3. HTTP 接口

### `GET /healthz`

业务服务健康检查 + 运行时配置可观测端点。

- **状态**：`status` 字段值为 `ok` / `needs_init` / `degraded`；服务在缺少 LLM 配置时返回 `needs_init`，提示先调 `/v1/init`。
- **当前配置**：`runtime_config` 字段是 env + `/v1/init` 叠加后的实际配置快照，敏感字段（如 `llm_api_key`）只返回 `*_set: bool` 和 `*_preview: "abcd...xxxx"` 形式。
- **可选 LLM 探活**：传 `?probe=auth`，服务会用当前 `llm_api_key` 去 `GET <llm_base_url>/models` 真实校验一次（**会发起一次上游请求**），按 `interpretation` 字段返回 `auth_ok` / `auth_failed` / `endpoint_missing_but_url_reachable` / `unreachable` 等结论。

```bash
# 不探活（默认）
curl -fsS http://127.0.0.1:49999/healthz | python -m json.tool

# 包含 LLM auth 探活（会真实命中上游 /models）
curl -fsS 'http://127.0.0.1:49999/healthz?probe=auth' | python -m json.tool
```

返回示意：

```json
{
  "status": "ok",
  "initialized_at": "2026-05-07T07:30:00.123456+00:00",
  "initialized_keys": ["llm_api_key", "llm_base_url", "llm_model"],
  "runtime_config": {
    "llm_base_url": "https://api.openai.com/v1",
    "llm_api_key_set": true,
    "llm_api_key_preview": "sk-p...xxxx",
    "llm_model": "gpt-4.1-mini",
    "llm_temperature": 0.0,
    "browser_headless": true,
    "feishu_default_profile_id": "feishu-default",
    "draft_session_ttl_sec": 1800
  },
  "checks": {
    "llm_api_key_set": true,
    "llm_base_url_set": true,
    "llm_model_set": true,
    "browser_artifacts_dir_writable": true,
    "auth_state_dir_writable": true,
    "llm_auth_probe": {
      "ok": true,
      "status": 200,
      "url": "https://api.openai.com/v1/models",
      "interpretation": "auth_ok"
    }
  },
  "auth_profiles_count": 1
}
```

### `POST /v1/init`

运行时注入 env 配置——专为**无法在 sandbox 创建时传环境变量**的场景准备。所有字段都可选（`null`/缺省即跳过）；多次调用会**合并**而不是覆盖：

```bash
curl -fsS -X POST http://127.0.0.1:49999/v1/init \
  -H 'Content-Type: application/json' \
  -d '{
    "llm_base_url": "https://api.openai.com/v1",
    "llm_api_key": "sk-...",
    "llm_model": "gpt-4.1-mini",
    "llm_temperature": 0,
    "feishu_default_profile_id": "feishu-default"
  }'
```

可注入的字段（白名单，未在表里的字段会 400）：

| 字段 | 类型 | 对应的 env |
|---|---|---|
| `llm_base_url` | string | `LLM_BASE_URL` |
| `llm_api_key` | string | `LLM_API_KEY` |
| `llm_model` | string | `LLM_MODEL` |
| `llm_temperature` | number (0-2) | `LLM_TEMPERATURE` |
| `browser_headless` | bool | `BROWSER_HEADLESS` |
| `browser_window_width` | int (320-4096) | `BROWSER_WINDOW_WIDTH` |
| `browser_window_height` | int (320-4096) | `BROWSER_WINDOW_HEIGHT` |
| `feishu_default_profile_id` | string | `FEISHU_DEFAULT_PROFILE_ID` |

**注意事项**：

- 配置仅放在**进程内存**，重启即失。多副本部署需要把 LLM 凭据外置成共享 secret；showcase 场景单副本就够。
- `AUTH_STATE_DIR` / `BROWSER_ARTIFACTS_DIR` / `DRAFT_SESSION_TTL_SEC` 故意不在白名单里——它们影响磁盘路径或长生命周期对象，运行时改容易出怪问题，必须用 env 在 boot 时设。
- 优先级：**`/v1/init` overlay > env > 内置默认值**。同名 key 多次 `/v1/init` 会按顺序覆盖。
- 服务在没拿到 `llm_api_key` 之前 `/v1/agent/run` 会返回 422 + 友好提示，不会卡死。

### `POST /v1/agent/run`

同步执行浏览任务，返回最终 JSON 结果。

通用请求示例：

```json
{
  "mode": "general",
  "query": "打开 https://github.com/browser-use/browser-use ，告诉我它当前 star 数，并返回仓库链接",
  "start_url": "https://github.com/browser-use/browser-use",
  "allowed_domains": ["github.com"],
  "max_steps": 20,
  "timeout_sec": 300
}
```

Feishu 示例请求（推荐：把 bitable URL 直接写在 `query` 里，服务端自动推断 mode 并抽取 URL）：

```json
{
  "query": "请把这个飞书多维表格转成问卷：https://example.feishu.cn/base/xxxxxxxxxxxx",
  "auth": {
    "profile_id": "feishu-default"
  }
}
```

Feishu 预置问卷填写请求示例：

```json
{
  "mode": "feishu_form_fill",
  "query": "我叫张三，5月8号参会，3个人参加。"
}
```

服务端自动推断规则：

- **mode 自动推断**：
  - 如果显式设了 `mode=feishu_form_fill`，服务会自动使用内置的预置问卷 URL
  - 否则如果 `query`（或 `bitable_url`）里包含飞书/Lark 多维表格 URL，自动升级为 `mode=feishu_bitable_to_form`
- **URL 抽取**：只接受 `*.feishu.cn` / `*.larksuite.com` / `*.larkoffice.com`；优先 `/base/` 或 `/wiki/` 路径；自动剥离结尾标点（中英文逗号、句号、括号、引号等）。
- **HITL 默认开启**：两个 Feishu 模式下 `require_human_confirmation` 默认 `true`。bitable 模式会先停在“问卷草稿确认”，form-fill 模式会先停在“字段答案确认”。
- **`human_confirmation_granted` 不会被自动推断**——它的语义就是"人类已审核"，必须由调用方在第二次请求中显式传 `true` 并附带 phase 1 返回的 `draft_session_id`。

如果你还没把登录态存进服务端，也可以一次性直接传：

```json
{
  "query": "请把这个飞书多维表格转成问卷：https://example.feishu.cn/base/xxxxxxxxxxxx",
  "auth": {
    "storage_state": {
      "cookies": [],
      "origins": []
    }
  }
}
```

仍然支持显式传 `mode`、`bitable_url`、`require_human_confirmation` 等，显式值始终优先于推断。

典型返回：

```json
{
  "run_id": "69f3d8af-d6ce-4f5c-a1fa-8485fd0b07f4",
  "success": true,
  "mode": "feishu_bitable_to_form",
  "final_text": "https://example.feishu.cn/share/base/form/shrcnxxxx",
  "form_url": "https://example.feishu.cn/share/base/form/shrcnxxxx",
  "form_name": "用户调研问卷",
  "current_url": "https://example.feishu.cn/share/base/form/shrcnxxxx",
  "visited_urls": [
    "https://example.feishu.cn/base/xxxxxxxxxxxx",
    "https://example.feishu.cn/share/base/form/shrcnxxxx"
  ],
  "steps": 9,
  "duration_sec": 73.481,
  "screenshots": [],
  "errors": [],
  "notes": [],
  "structured_output": {
    "success": true,
    "bitable_url": "https://example.feishu.cn/base/xxxxxxxxxxxx",
    "form_url": "https://example.feishu.cn/share/base/form/shrcnxxxx",
    "form_name": "用户调研问卷",
    "notes": []
  },
  "history_excerpt": [
    "Opened the bitable and found the top-right publish entry.",
    "Confirmed the questionnaire conversion dialog.",
    "Captured the generated questionnaire URL."
  ]
}
```

### `POST /v1/agent/stream`

使用 `text/event-stream` 流式返回浏览执行进度。

事件类型：

- `run_started`
- `step_start`
- `step_end`
- `heartbeat`
- `run_completed`
- `run_failed`

`curl` 示例：

```bash
curl -N http://localhost:49999/v1/agent/stream \
  -H 'Content-Type: application/json' \
  -d '{
    "mode": "general",
    "query": "打开 browser-use 的 GitHub 仓库并总结首页关键信息",
    "start_url": "https://github.com/browser-use/browser-use",
    "allowed_domains": ["github.com"]
  }'
```

### `POST /v1/feishu/form-fill/prepare`

专用 phase 1 接口：使用内置的预置飞书问卷 schema，把自然语言 `query` 解析成固定字段草稿，但不会提交。

请求示例：

```bash
curl -X POST http://127.0.0.1:49999/v1/feishu/form-fill/prepare \
  -H 'Content-Type: application/json' \
  -d @examples/feishu-form-fill.prepare.sample.json
```

典型响应字段：

- `awaiting_human_confirmation: true`
- `draft_answers: [...]`
- `form_name`
- `draft_session_id`
- `draft_session_expires_at`
- 其中 `参会时间` 会先归一化成便于人确认的展示值，例如用户输入 `5月8号` 时，响应会展示成 `2026-05-08`；真正提交时才会转成毫秒级时间戳

### `POST /v1/feishu/form-fill/submit`

专用 phase 2 接口：带 `draft_session_id` 和人工确认后的字段值重新打开同一份预置问卷，正式填写并提交。

请求示例：

```bash
curl -X POST http://127.0.0.1:49999/v1/feishu/form-fill/submit \
  -H 'Content-Type: application/json' \
  -d @examples/feishu-form-fill.submit.sample.json
```

## 4. Feishu 登录建议

飞书这类站点通常存在登录态、组织权限、风控验证等问题。这个项目现在提供了专门的登录态管理接口。

### `POST /v1/auth/storage-state`

把现成的 Playwright `storage_state` 保存为服务端 profile：

```json
{
  "profile_id": "feishu-default",
  "set_as_feishu_default": true,
  "description": "local debug feishu account",
  "storage_state": {
    "cookies": [],
    "origins": []
  }
}
```

### `GET /v1/auth/storage-state`

列出当前已经保存的 profile。

### `GET /v1/auth/storage-state/{profile_id}`

查看单个 profile 的元信息和落盘路径。

运行任务时，推荐优先使用下面三种方式：

1. `auth.profile_id`
   先保存登录态，运行时只传 profile id。
2. `auth.storage_state`
   直接传 Playwright storage state JSON，对应已经登录好的会话。
3. `auth.storage_state_path`
   在容器内准备一个现成的 `auth.json` 文件，然后传路径。

如果是 Feishu showcase，且你没有显式传 `auth.profile_id`，服务会自动尝试使用 `.env` 里的 `FEISHU_DEFAULT_PROFILE_ID`。

### 如何拿到 Playwright storage_state

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://feishu.cn")
    input("登录完成后按回车继续...")
    context.storage_state(path="feishu.storage_state.json")
    browser.close()
```

然后把 `feishu.storage_state.json` 的内容 POST 到 `/v1/auth/storage-state`。

## 5. Feishu Human-in-the-Loop

Feishu 两类流程都拆成两阶段，对外既可以直接调 HTTP，也可以通过独立的 MCP tool 调用（见第 8 节）。两阶段之间都用一个**服务端签发的 `draft_session_id`** 绑定：

```
phase 1 (draft) ── 返回 draft_session_id ──► 人类审阅 draft_questions / draft_answers
                                                      │
                                                      ▼
phase 2 (submit/publish) ── 必须回传同一个 draft_session_id ──► 服务端校验通过后继续
```

### 服务端校验规则

`draft_session_id` 由服务端在 phase 1 返回时签发，存活时间由 `DRAFT_SESSION_TTL_SEC` 控制（默认 1800 秒）。phase 2 必须回传，且服务端会在**启动浏览器之前**校验：

| 校验项 | 不通过的后果 |
|---|---|
| session 存在且未过期 | 立刻 `run_failed`，提示重新跑 phase 1 |
| 目标 URL（`bitable_url` 或 `form_url`）与 phase 1 一致 | 立刻 `run_failed`，提示 URL 不匹配 |
| `auth.profile_id` 与 phase 1 一致 | 立刻 `run_failed`，提示 profile 不匹配 |

校验通过后**会立即从内存里删除**该 session（一次性消费），防止重放。如果 phase 2 因为浏览器侧出错失败，需要重新跑 phase 1 拿新的 `draft_session_id`。

> 当前实现是单进程内存存储，多副本部署需要换成 Redis 之类共享后端。

### 调用流程

1. 审题阶段（请求只需 `query` + `auth`）

   服务端会自动设 `mode=feishu_bitable_to_form` 和 `require_human_confirmation=true`。
   Agent 打开表单编辑界面，提取可见标题和题目列表，然后停下等待人工确认。

   响应里会带：
   - `awaiting_human_confirmation: true`
   - `draft_questions: [...]`
   - `form_name`
   - `draft_session_id`（**phase 2 要把这个值回传**）
   - `draft_session_expires_at`（unix 秒，方便客户端判断 TTL）

2. 发布阶段（请求要带 `human_confirmation_granted=true` + `draft_session_id`，可选 `human_confirmation_notes`）

   Agent 才会继续：
   - 点击右上角 `分享表单`
   - 打开 `开启表单分享`
   - 抓取最终真实问卷链接

```bash
# 审题阶段
curl -X POST http://127.0.0.1:49999/v1/agent/run \
  -H 'Content-Type: application/json' \
  -d @examples/feishu-run.sample.json
# 从响应里复制 draft_session_id 的值

# 发布阶段：编辑 examples/feishu-run.confirm.sample.json，
# 把 "REPLACE_WITH_draft_session_id_FROM_PHASE_1_RESPONSE" 替换成上一步拿到的值
curl -X POST http://127.0.0.1:49999/v1/agent/run \
  -H 'Content-Type: application/json' \
  -d @examples/feishu-run.confirm.sample.json
```

### 预置问卷填写流程

1. 解析并生成答案草稿：调用 `POST /v1/feishu/form-fill/prepare`

   响应里会带：
   - `awaiting_human_confirmation: true`
   - `draft_answers: [...]`
   - `form_name`
   - `draft_session_id`
   - `draft_session_expires_at`
   - 固定字段是：`姓名`（string）、`参会时间`（phase 1 展示为可读日期，phase 2 提交时转换为毫秒时间戳）、`参会人数`（number）

2. 人工确认或修正字段：调用 `POST /v1/feishu/form-fill/submit`

   可以通过 `confirmed_answers` 只覆盖个别字段，未覆盖的字段会复用 phase 1 的草稿答案。
   如果用户在 phase 1 结果返回后又补充了新的自然语言信息，不应该直接调 submit，而应该重新调 prepare 进入新一轮“解析 -> 确认”流程。

## 6. 本地运行

建议 Python 3.11 或 3.12。`browser-use` 官方 README 当前要求 `Python>=3.11`。

先复制环境变量：

```bash
cp .env.example .env
```

最少需要填这些值：

- `LLM_API_KEY`
- 如果不是 OpenAI 官方兼容接口，还要改 `LLM_BASE_URL`
- 如需默认 Feishu profile，可保留 `FEISHU_DEFAULT_PROFILE_ID=feishu-default`

安装依赖：

```bash
uv venv --python 3.11
source .venv/bin/activate
uv sync
python -m playwright install chromium
```

启动服务：

```bash
./scripts/run_local.sh
```

健康检查：

```bash
curl http://localhost:49999/healthz
```

保存 Feishu 登录态：

```bash
./.venv/bin/python scripts/export_feishu_storage_state.py --output ./tmp/feishu.storage_state.json
./.venv/bin/python scripts/upload_feishu_storage_state.py --input ./tmp/feishu.storage_state.json --server http://127.0.0.1:49999
```

查看当前 profile：

```bash
curl http://localhost:49999/v1/auth/storage-state
```

跑 Feishu showcase：

```bash
curl -X POST http://localhost:49999/v1/agent/run \
  -H 'Content-Type: application/json' \
  -d @examples/feishu-run.sample.json
```

## 7. 构建 CubeSandbox 镜像

### Docker 构建

```bash
docker build -t your-registry.example.com/browser-use-cubesandbox-agent:latest .
docker push your-registry.example.com/browser-use-cubesandbox-agent:latest
```

### 创建 CubeSandbox template

注意：

- `49983` 是 `envd` 控制面端口，不要给业务服务占用。
- 业务 HTTP 服务监听 `49999`。
- `ENABLE_MCP=true` 时还会在 `60000` 起一个 MCP streamable-HTTP server（详见 §9）。即便不启用 MCP，把 60000 一起 expose 也无副作用。
- 模板探针应使用 `49983/health`，这是 CubeSandbox 官方文档要求的 `envd` 探针。

```bash
cubemastercli tpl create-from-image \
  --image your-registry.example.com/browser-use-cubesandbox-agent:latest \
  --writable-layer-size 2G \
  --expose-port 49983 \
  --expose-port 60000 \
  --expose-port 49999 \
  --probe 49983 \
  --probe-path /health
```

如果你在内部平台会消费 `agent.build.yaml`，也可以直接复用仓库里的这个文件。

## 8. MCP Tool Schema

仓库提供五个 MCP tool 定义：

| Tool 名称 | inputSchema | 用途 |
|---|---|---|
| `browser_agent_run` | `mcp.browser_agent_run.input.schema.json` | 通用浏览器任务，没有 Feishu 特定流程 |
| `feishu_form_fill_prepare` | `mcp.feishu_form_fill_prepare.input.schema.json` | Feishu 预置问卷填写的**第一步**：解析固定三字段并产出待确认答案草稿 |
| `feishu_form_fill_submit` | `mcp.feishu_form_fill_submit.input.schema.json` | Feishu 预置问卷填写的**第二步**：在没有新增自然语言补充的前提下，按确认结果正式提交 |
| `feishu_bitable_draft_form` | `mcp.feishu_bitable_draft_form.input.schema.json` | Feishu 多维表格转问卷的**第一步**：抓取草稿题目，等待人工确认 |
| `feishu_bitable_publish_form` | `mcp.feishu_bitable_publish_form.input.schema.json` | Feishu 多维表格转问卷的**第二步**：人工确认后开启表单分享，返回最终问卷链接 |

其中：

- `browser_agent_run` / `feishu_bitable_*` 仍然映射到 `POST /v1/agent/run`
- `feishu_form_fill_prepare` / `feishu_form_fill_submit` 映射到专用 HTTP endpoint

`schemas/mcp.tools.catalog.json` 用 `bodyTemplate` 把 MCP 的入参映射成 HTTP body，调用方不需要再关心内部状态字段。

### 两个 Feishu tool 的调用顺序

```text
feishu_bitable_draft_form(query)
    └─ 返回 awaiting_human_confirmation=true
            + draft_questions + form_name
            + draft_session_id ★ + draft_session_expires_at
        └─ 人类审阅 draft_questions
            └─ feishu_bitable_publish_form(query, draft_session_id ★, human_confirmation_notes?)
                 └─ 服务端校验 draft_session_id（bitable_url + profile_id 必须与 phase 1 一致）
                     └─ 通过则消费 session、跑发布；失败则立即 run_failed
                         └─ 返回 form_url
```

最小调用入参（其余可全部省略）：

```jsonc
// feishu_bitable_draft_form 的 input
{ "query": "请把这个飞书多维表格转成问卷：https://lexmount.feishu.cn/wiki/DrETwm..." }

// 它的响应里会带 draft_session_id，把这个值传到下面：

// feishu_bitable_publish_form 的 input
{
  "query": "请把这个飞书多维表格转成问卷：https://lexmount.feishu.cn/wiki/DrETwm...",
  "draft_session_id": "<phase 1 返回的 draft_session_id>",
  "human_confirmation_notes": "题目内容已经确认，开启表单分享并返回最终链接"
}
```

服务端会自动从 `query` 中抽取飞书/Lark URL 并填入 `bitable_url`，因此 MCP 调用方不必单独传 URL；如果调用方已经有 URL 在手，也可以显式传 `bitable_url` 覆盖。

挂到自己的 MCP server 时的最小做法：

1. 用对应的 `inputSchema` 约束 tool 入参。
2. MCP handler 按 `bodyTemplate` 拼接 body，转发到对应的 HTTP endpoint（`/v1/agent/run` 或 `/v1/feishu/form-fill/*`）。
3. **从 draft tool 的响应里抽出 `draft_session_id`**，作为后续 publish tool 的入参之一传回（一般由你的 MCP host / 上游 LLM 在两次调用之间保管这个 token）。
4. 把 HTTP JSON 返回原样作为 tool result，或裁掉 `screenshots`、`history_excerpt` 这类大字段。
5. tool description 中已经写明调用顺序约束（"Only call after ... has returned a draft"），让上游 LLM 不会乱序触发。

## 9. 内置 MCP Server

`schemas/` 是给"自己接 MCP server"的人用的契约文件；如果你**不想自己写 MCP server**，仓库还自带了一个，跟 FastAPI 同进程跑：

### 启用方式

```bash
ENABLE_MCP=true ./scripts/run_local.sh
# 或
ENABLE_MCP=true uvicorn app.main:app --host 0.0.0.0 --port 49999
```

启用后 FastAPI 会**额外起一个线程**跑 MCP server，默认监听 `0.0.0.0:60000/mcp`（与业务 HTTP 49999 分开）。`GET /` 自检接口会回显 `"mcp_enabled": true` 和 `ports.mcp` 端口号。

CubeSandbox 模板要把 60000 一起 expose（[Dockerfile](Dockerfile) / [agent.build.yaml](agent.build.yaml) 已经配好）：

```bash
cubemastercli tpl create-from-image \
  --image your-registry.example.com/browser-use-cubesandbox-agent:latest \
  --writable-layer-size 2G \
  --expose-port 49983 \
  --expose-port 60000 \
  --expose-port 49999 \
  --probe 49983 --probe-path /health
```

放到 sandbox 里之后 MCP 的公网 URL 是 `https://60000-<sandbox_id>.cube.app/mcp`。

工具集合与 [`schemas/mcp.tools.catalog.json`](schemas/mcp.tools.catalog.json) 一致：`browser_agent_run` / `feishu_form_fill_prepare` / `feishu_form_fill_submit` / `feishu_bitable_draft_form` / `feishu_bitable_publish_form`。其中 form-fill 两个工具走专用 HTTP endpoint，其余工具走 `POST /v1/agent/run`；所有工具最终都复用同一套服务端执行逻辑和 `draft_session_id` 校验。

### 单独启动（不挂 FastAPI）

支持把 MCP server 拆成单进程跑，比如给 Claude Desktop / IDE 用 stdio：

```bash
# stdio（默认），给 IDE / desktop client 配置用
./.venv/bin/python -m mcp_server.server

# Streamable HTTP，独占端口
MCP_HOST=0.0.0.0 MCP_PORT=60000 \
./.venv/bin/python -m mcp_server.server --transport streamable-http
```

单独启动的版本仍然把工具调用反代到 FastAPI；如果 FastAPI 在另一台机器，需要设 `MCP_PROXY_BASE=http://<host>:49999`。

### 客户端验证

`mcp_server/client_example.py` 是一个最小的 streamable-HTTP 客户端，三种用法逐级递进：

**1. 仅 list_tools（最快，几秒返回，不动浏览器、不烧 LLM token）**

```bash
ENABLE_MCP=true ./scripts/run_local.sh &
./.venv/bin/python -m mcp_server.client_example
# 期望:
#   PASS MCP session initialized (server=browser-use-cubesandbox-agent ...)
#   PASS all expected tools registered
```

**2. 用 case 文件触发真实 tool 调用**

`examples/mcp/` 下放好了 6 个 case JSON，全部走"`tool` + `title` + `arguments`" 的格式（**注意它跟 `examples/feishu-run.sample.json` 那种 HTTP body 是不同形态**——后者是给 HTTP endpoint 用的，前者只是 MCP 工具入参）：

| 文件 | 工具 | 用途 |
|---|---|---|
| `browser_agent_run.example_com.json` | `browser_agent_run` | 最便宜的端到端冒烟，只要 LLM 凭据 |
| `browser_agent_run.github_stars.json` | `browser_agent_run` | 稍重一点，验证读取 GitHub 结构化数据 |
| `feishu_form_fill_prepare.json` | `feishu_form_fill_prepare` | Feishu 预置问卷填写 phase 1（要登录态 profile） |
| `feishu_form_fill_submit.json` | `feishu_form_fill_submit` | Feishu 预置问卷填写 phase 2，含 `draft_session_id` 占位符 |
| `feishu_bitable_draft_form.json` | `feishu_bitable_draft_form` | Feishu 转问卷 phase 1（要登录态 profile） |
| `feishu_bitable_publish_form.json` | `feishu_bitable_publish_form` | Feishu 转问卷 phase 2，含 `draft_session_id` 占位符 |

```bash
# 单 case
./.venv/bin/python -m mcp_server.client_example \
  --case examples/mcp/browser_agent_run.example_com.json

# 多 case 顺序执行
./.venv/bin/python -m mcp_server.client_example \
  --case examples/mcp/browser_agent_run.example_com.json \
  --case examples/mcp/browser_agent_run.github_stars.json
```

**3. 用 `--set` 在命令行覆写 case 字段**

phase 2 case 的 `draft_session_id` 是 `REPLACE_WITH_...` 占位符；客户端检测到没填会**直接报错并提示你怎么改**。三种填法任选：

```bash
# 推荐：phase 1 跑完后直接抄返回的 draft_session_id 命令行覆写
./.venv/bin/python -m mcp_server.client_example \
  --case examples/mcp/feishu_bitable_publish_form.json \
  --set draft_session_id=c18ecbc2-f8ea-4afd-9a33-4ee3ca4f739c

# 嵌套结构也能覆写（VALUE 优先按 JSON 解析）
./.venv/bin/python -m mcp_server.client_example \
  --case examples/mcp/feishu_bitable_draft_form.json \
  --set 'auth={"profile_id":"feishu-alt"}' \
  --set max_steps=20

# 或者就改 examples/mcp/feishu_bitable_publish_form.json 里的占位符再跑
```

phase 1 case 跑通后，客户端会**直接打印一行可复制粘贴的 phase 2 命令**，含真实 `draft_session_id`，省掉来回抄写：

```
PASS feishu_bitable_draft_form: phase 1 returned a draft for human review
     draft_session_id: c18ecbc2-f8ea-4afd-9a33-4ee3ca4f739c
     expires_at:       1715077200.0
       Q1: 你的姓名 [text, required]
       Q2: 反馈内容 [textarea, optional]
       ...

     Next step (phase 2): --case examples/mcp/feishu_bitable_publish_form.json --set draft_session_id=c18ecbc2-f8ea-4afd-9a33-4ee3ca4f739c
```

**指定别的 MCP URL（分离部署 / sandbox 公网）**

```bash
MCP_URL=https://60000-<sandbox_id>.cube.app/mcp \
  ./.venv/bin/python -m mcp_server.client_example \
  --case examples/mcp/browser_agent_run.example_com.json
```

## 10. 已知边界

- `browser-use` 的真实执行效果强依赖所用 LLM、网页复杂度、是否有登录态。
- Feishu “转问卷”“分享表单”“开启表单分享”入口可能因产品版本、语言、权限不同而变化，所以项目里做的是“指令模板 + 结构化结果”方案，而不是硬编码某个按钮选择器。
- 如果目标站点风控较强，单纯自托管本地浏览器可能不如 Browser Use Cloud 稳定。

## 11. 后续建议

如果你下一步就要把它接到实际 MCP server / 生产环境，建议优先补三件事：

1. 增加 API Key / 签名校验，避免任何人直接调用浏览器 Agent 或 MCP `/mcp` 端点。
2. 把 `run_id`、请求摘要、最终结果、错误堆栈接到你们现有日志系统里，方便排查 Feishu 这类长链路问题。
3. `DraftSessionStore` 当前是单进程内存版本，多副本部署需要改成 Redis 之类共享存储。
