# M3 任务快照与跨会话恢复

## 任务事件流

在应用路由配置中启用 `task_resume`。调用方持有会话所属的任务范围，例如 `task:printer-77`。同一租户、应用和授权主体创建带相同 `task_id` 的会话，复用同一条任务流。会话事件序号继续按会话递增，任务版本跨这些会话递增。

`POST /api/v1/sessions/{id}/events` 追加事件时，带任务的响应包含 `sequence` 和 `task_version`。普通会话沿用原响应。结构化 `task_update` 和带 `operation` 的 `tool_result` 同时要求 `sessions:write` 与 `tasks:write`。版本前置条件由 `expected_version` 提供。已接受请求用原幂等键重放时，返回原事件对应的版本。

| 接口 | 权限与结果 |
| --- | --- |
| `GET /api/v1/sessions/{id}/task` | `context:build`，返回任务状态、事件流 ID 和当前版本 |
| `POST /api/v1/sessions/{id}/compact` | `tasks:write`，按请求的 `expected_version` 提交快照，同版本复用已有有效快照 |
| `POST /api/v1/sessions/{id}/restore` | `context:build`，按快照及增量事件构建恢复上下文，生成新的审计清单 |
| `POST /api/v1/context/build` | 指定 `task_type: "task_resume"` 和 `session_id` 时使用同一恢复链路 |

结构化任务更新示例为任务版本 3 时提交确认事实及下一步。已提供的列表或映射替换同名字段，省略的字段保留原值。

```json
{
  "kind": "task_update",
  "content": "记录现场排查结果",
  "evidence_refs": ["ticket-77"],
  "task_update": {
    "expected_version": 3,
    "status": "waiting_external",
    "goal": "恢复打印服务",
    "confirmed_facts": {"device": "printer-77"},
    "assumptions": ["驱动可能过期"],
    "constraints": ["等待操作回执后再决定下一步"],
    "pending_steps": ["核对设备日志"]
  }
}
```

为工具结果记录 `operation_id` 和 `outcome`。结果为 `succeeded` 或 `failed` 时需要业务回执 `receipt`，并可记录 `error_code`。尚未核实的结果使用 `unknown`。已确认回执与后续回执冲突时返回 `TASK_OPERATION_CONFLICT`。业务执行层根据恢复响应中的 `unconfirmed_operations` 核对回执。无结构化回执的旧工具事件进入 `unclassified_tool_results`，恢复后由业务方补核。

## 压缩与恢复

快照存放已确认事实、证据引用、假设、约束、待办、操作结果和会话证据。可重取的成功工具输出使用操作与证据引用表示。错误、未知结果、一次性输出、安全证据和用户原话保留。原始会话事件继续作为审计与重放来源。

恢复时按当前租户、应用、主体、任务范围及 `history` 规则读取最新有效快照，再重放到确定的版本。`include_knowledge` 和 `include_memory` 可按当前权限重新检索现时证据。提交清单前再次校验任务版本、规则和记忆状态。模型输入把任务数据放在 `task_context`，响应另带任务头、快照位置、重放数量及待核对操作。

任务输入占可用输入预算的 72% 时，有 `tasks:write` 的恢复请求会尝试生成新快照。缺少快照时可用原始事件重建。压缩建议校验失败时沿用可用快照和事件增量。必须保留的内容超出窗口时返回 `CONTEXT_BUDGET_EXCEEDED`。并发更新返回 409 `SESSION_VERSION_CONFLICT`。

配置内网 `CONTEXT_SUMMARY_URL` 后，压缩请求发送带序号的助手备注，响应格式为 `{"notes":[{"sequence":2,"content":"来自原备注的片段"}]}`。服务核对序号、原文和大小，结构化任务字段由确定性事件重放产生。无压缩端点时，本地按最新助手备注生成摘录。可用 `CONTEXT_SUMMARY_TOKEN` 配置服务凭据。

## 升级与验证

执行 `alembic upgrade head` 后重新应用 [运行角色授权](../deploy/grant-runtime.sql)。迁移保留已有会话、原始事件和会话序号。旧任务流按会话创建时间、会话 ID、会话内序号生成可重放的顺序。已有会话与事件幂等键继续可用。

本地演示可以用 `uv run python scripts/dev_token.py --task-id printer-77` 签发含任务范围的测试令牌，然后创建 `task_id` 为 `printer-77` 的会话。

本地回归覆盖跨会话重放、关键内容保留、老数据升级、摘要错误与超时、迟到快照、损坏快照、并发写入和恢复预算。真实任务和内网摘要端点的准确率及延迟仍需按试点样本验收。
