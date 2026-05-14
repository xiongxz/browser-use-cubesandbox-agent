# Feishu Form Fill SSE 示例

当前公开业务接口只保留飞书表单填写的 HTTP SSE 长链：

- `POST /v1/feishu/form-fill/run`：创建一次表单填写 run，流到 `ask_user_question` 后结束本段 SSE。
- `POST /v1/feishu/form-fill/runs/{run_id}/input`：当 SSE 返回 `ask_user_question` 时，回传用户确认、修改、补充或取消，并返回下一段 SSE。

## 启动

```bash
PORT=50001 ENABLE_MCP=true ./scripts/run_local.sh
```

## 打开 SSE

```bash
curl -N -sS -X POST http://127.0.0.1:50001/v1/feishu/form-fill/run \
  -H "Content-Type: application/json" \
  -d @examples/feishu-form-fill.run.sample.json
```

当事件流返回 `ask_user_question` 后，复制其中的 `run_id` 和 `question_id`，再调用 input 接口。本段 run SSE 到这里会结束。

## 确认提交

```bash
curl --no-buffer -sS -X POST http://127.0.0.1:50001/v1/feishu/form-fill/runs/<run_id>/input \
  -H "Content-Type: application/json" \
  -d @examples/feishu-form-fill.input.message-confirm.sample.json
```

## 修改字段后再次确认

```bash
curl --no-buffer -sS -X POST http://127.0.0.1:50001/v1/feishu/form-fill/runs/<run_id>/input \
  -H "Content-Type: application/json" \
  -d @examples/feishu-form-fill.input.edit.sample.json
```

服务会再次返回新的 `ask_user_question`，前端继续渲染 `payload` 等用户最终确认。

## 补充自然语言

```bash
curl --no-buffer -sS -X POST http://127.0.0.1:50001/v1/feishu/form-fill/runs/<run_id>/input \
  -H "Content-Type: application/json" \
  -d @examples/feishu-form-fill.input.supplement.sample.json
```

这会让服务用补充信息重新生成草稿，适合“换一个请假 case”“其实人数改成 8 个”这类场景。
