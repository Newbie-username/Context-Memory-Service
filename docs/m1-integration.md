# M0 契约与 M1 联调说明

业务应用从 Context Service 获取模型消息、固定工具和预算，并负责最终模型调用。PostgreSQL 保存租户、规则版本、会话、顺序事件、Context Manifest 和审计记录。知识检索通过 HTTP 适配器。M1 定义记忆读取契约 `MemoryProvider.search`，请求选用该来源时按权限返回 `MEMORY_NOT_ENABLED` 或拒绝。记忆管理 API 随 M2 启用。

## 委托身份与权限

使用配置的 RSA 公钥验证 RS256 JWT。身份平台签发 `iss`、`aud`、`iat`、`exp`、`sub`、`tenant_id`、`application_id`、`permissions` 和 `scopes`。应用和租户在部署端登记，身份平台负责把范围绑定到用户和调用应用。示例范围包括 `tenant:demo`、`team:helpdesk`、`project:rollout`、`device:printer-7` 和 `task:incident-7`。主体自身的范围从 `sub` 推导。

| 权限 | 校验 |
| --- | --- |
| `sessions:write` | 核对租户、应用和所有者。会话绑定任务时核对任务范围 |
| `context:build` | 核对身份中的应用、启用的任务类型和有效规则允许的数据源 |
| `context:audit` | 核对租户、应用和所有者，再按当前规则重查会话及证据范围 |

策略按租户、应用和已授权范围装配。每级数据源取交集，结构化约束冲突时拒绝。每个策略选择器取当前生效的最新版本，租户系统指令优先。RLS 在事务内绑定租户，事务结束后清除范围。数据库使用独立的迁移与运行账号，授权模板见 `deploy/grant-runtime.sql`。

## HTTP 契约

`/docs` 提供交互式 API 文档，`/openapi.json` 提供机器可读契约。全部业务路由使用 `/api/v1` 前缀。写会话和追加事件需要 `Idempotency-Key`。同键同内容返回原会话 ID 或事件序号，同键不同内容返回 409。构建清单和审计先在同一事务内持久化，再返回模型输入。

| 接口 | 功能 |
| --- | --- |
| `POST /sessions` | 创建由调用主体持有的会话 |
| `POST /sessions/{session_id}/events` | 追加消息或工具结果并返回递增序号 |
| `POST /context/build` | 返回模型消息、工具、预算及警告 |
| `GET /context/{context_id}/manifest` | 读取规则版本、证据引用和取舍记录 |
| `GET /health/ready` | 检查迁移版本、租户状态和基础规则 |
| `GET /metrics` | 供内网采集 Prometheus 指标 |

错误体包含 `code`、`request_id` 和 `details`，响应头包含 `X-Request-ID`。常见结果包括 401 `AUTH_REQUIRED`、403 `AUTH_SCOPE_DENIED`、409 `IDEMPOTENCY_CONFLICT`、422 `CONTEXT_BUDGET_EXCEEDED`、424 `EVIDENCE_UNAVAILABLE` 和 504 `DEPENDENCY_TIMEOUT`。可选检索超时返回 200，状态为 `degraded`。知识库明确拒绝访问时返回 403。通过 `CONTEXT_OTLP_ENDPOINT` 可将请求和检索 span 发送到内网采集器。

## 知识库适配器

适配器向 `CONTEXT_RAG_URL` 发送 JSON POST，字段包括 `tenant_id`、`application_id`、`subject`、`scopes` 和 `query`。可用 `CONTEXT_RAG_TOKEN` 配置服务身份。上游按该主体和范围过滤，适配器下游再次核对响应中每条证据的租户和范围。

```json
{
  "items": [
    {
      "tenant_id": "demo",
      "scope": "tenant:demo",
      "source_id": "printer-e77",
      "version": 2,
      "content": "检查打印机电源和连接状态。",
      "expires_at": null,
      "required": true,
      "facts": {"error-code": "E-77"}
    }
  ]
}
```

知识问答需要至少一条可用证据。标记为必需的证据须通过版本、时效、冲突和预算校验。普通对话默认按权限读近期会话，可以请求知识检索。知识和历史的初始动态配额按 45 比 15 分配，剩余预算可用于其它可选片段。会话和证据通过带来源的 `untrusted_context` 传入。清单记载授权证据的引用，越权结果只计数。

## 联调与阶段出口

| 待联调项 | 确认材料 |
| --- | --- |
| 企业身份 | 测试签发方、受众、公钥、应用映射和委托样本 |
| 真实知识库 | 测试端点、服务凭据和带权限及来源版本的响应 |
| 目标模型 | 固定工具、真实输入格式、窗口配置和本地 `tokenizer_file` |
| 质量对照 | 脱敏问题、访问主体、正确结论、必需引用和现有 RAG 回放结果 |

本地默认对规范化 JSON 请求做 UTF-8 字节估算。联调时用目标推理服务的实际序列化和 tokenizer 核对中文、错误码及长文本。对同一批留出样本分别统计两个链路的正确率、证据充分率和动态 Token，记录失败样本及原因。真实知识库和质量出口以业务联调结果为准。

本地测试覆盖签名、连接池复用下的 RLS、同租户越权、规则冲突、会话锁与幂等、必需及可选来源超时、证据过滤、预算边界和清单权限。
