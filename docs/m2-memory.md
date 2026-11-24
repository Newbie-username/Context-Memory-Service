# M2 受控长期记忆

## 启用条件

在应用的 `CONTEXT_APPLICATIONS` 对象中启用 `memory_enabled`，并在租户和应用生效策略的 `sources` 中允许 `memory`。JWT 的 `memory:propose`、`memory:read` 和 `memory:review` 分别控制提交、使用和审核。记忆按租户、应用和作用域隔离，可在同一授权范围内跨会话使用。

升级既有 M1 部署时，先使用与数据卷排序规则兼容的 pgvector PostgreSQL 16 镜像并运行 `alembic upgrade head`，再以迁移账号重新执行 [运行角色授权](../deploy/grant-runtime.sql)。本地 Compose 使用与原开发数据卷兼容的 Debian Trixie 版本。迁移在原有数据库上追加表，已有会话和上下文清单可继续访问。

## 接口

所有请求使用 `/api/v1` 前缀。候选创建需要 `Idempotency-Key`。审核和撤销需要提交最新的 `expected_version`，旧版本返回 409 `MEMORY_VERSION_CONFLICT`。

| 接口 | 行为 |
| --- | --- |
| `POST /memories/candidates` | 提交带来源事件、作用域和有效期的候选 |
| `POST /memories/{memory_id}/verify` | 审核通过或拒绝。替代已有记忆时指定其 ID 和版本 |
| `POST /memories/{memory_id}/revoke` | 撤销可见记忆。个人偏好的提交人可撤销自己的已验证偏好 |
| `POST /memories/search` | 查找当前有效且有权访问的已验证记忆 |
| `GET /memories/{memory_id}/versions` | 供有权的提交人或审核人查看状态历史、证据引用，以及待审内容 |

候选示例使用用户明确确认过的原始会话消息。

```json
{
  "application_id": "it-helpdesk",
  "kind": "user_preference",
  "scope": "user:demo-user",
  "topic": "printer-default-mode",
  "content": "默认使用双面打印",
  "model_version": "manual-v1",
  "source": {"session_id": "8f046522-7a61-451d-a2a7-97690e318947", "sequence": 1},
  "expires_at": "2026-12-31T00:00:00Z",
  "confirmed": true
}
```

业务端只在用户明确同意保留偏好时发送 `confirmed: true`。自动验证要求自身作用域、自己会话的 `user_message` 与偏好文本一致，并且同主题没有有效记忆。工单经验使用 `resolved_incident` 和 `tool_result` 来源，进入待审队列，由同一范围内的独立审核人核对原始业务证据。候选阶段筛查常见凭据、邮箱和标识符格式，企业数据分类仍由接入流程确认。状态、审批及来源版本在 PostgreSQL 中留痕。

同来源、同种类、同作用域的相同提交返回当前记录。同键或同来源的不同内容返回 `MEMORY_CANDIDATE_CONFLICT`。不同来源的同主题候选可以并列待审。需要替换已验证项时在审核请求中传入 `replaces: {"memory_id": "...", "version": 3}`。

## 搜索和异步索引

`POST /memories/search` 使用 `application_id`、`query` 和可选的 `limit`。关键词检索支持中文组合和如 `E-77` 的错误码。配置嵌入服务时使用授权范围内的关键词和 pgvector 余弦分数排序。嵌入请求失败时仍可用关键词，响应带 `EMBEDDING_UNAVAILABLE`。本阶段基于授权后的候选精确计算向量距离。

`CONTEXT_EMBEDDING` 设置内部服务的 `url`、`model_version`、`dimensions`，以及可选的 `token` 和 `timeout_seconds`。接口接收 `{"model":"example-v1","input":"待编码文本"}`，返回与配置维数一致的 `{"embedding":[0.4,0.2]}`。版本、维数和当前记忆版本同时匹配的索引才参与检索。

Redis 传递任务，PostgreSQL 事务发件箱保留待完成记录。支持只使用关键词的部署。需要投递索引、清理撤销项及物化过期状态时，启动两个进程。

```bash
uv run --env-file .env python -m app.worker
uv run --env-file .env dramatiq app.worker --processes 1 --threads 2
```

向量写入前再次核对记忆状态和版本，重复任务复用已完成记录。检索和 `POST /context/build` 在返回前把候选版本与 PostgreSQL 权威记录对比，撤销或过期后的记忆立即退出召回。构建请求以 `include_memory: true` 选用记忆，使用和取舍记录都会进入上下文清单。已生成的历史清单可保留授权范围内的审计引用。

## 验收范围

本地用带 PostgreSQL、pgvector 和 Redis 的环境，以及固定的脱敏工单、中文偏好和嵌入响应验证流程。企业嵌入服务、真实工单及质量对照尚待联调。
