# Context-Memory-Service
一个独立的上下文与长期记忆基础服务，为IT 知识库、智能工单中心、终端诊断 Agent 提供统一的上下文构建、跨会话记忆、任务恢复、上下文压缩和记忆治理能力。

## M1 Context Builder

当前提供知识问答和普通对话的上下文构建、版本化规则、会话事件、预算和审计清单。业务应用接收模型消息后调用自己的推理服务。[开发计划](docs/development-plan.md) · [接口和联调说明](docs/m1-integration.md)

## 本地启动

使用 Python 3.11 或更新版本、uv 和 Docker Compose。以下配置用于回环地址上的本地开发。

```bash
uv sync --locked
docker compose up -d --wait postgres
cp .env.example .env
CONTEXT_MIGRATION_DATABASE_URL=postgresql+psycopg://postgres:local-migrate@127.0.0.1:55432/context \
  uv run alembic upgrade head
```

首次初始化本地数据库时创建运行角色和样例策略。

```bash
docker compose exec -T postgres psql -U postgres -d context -v ON_ERROR_STOP=1 < deploy/init-dev.sql
docker compose exec -T postgres psql -U postgres -d context -v ON_ERROR_STOP=1 \
  -v runtime_role=context_app < deploy/grant-runtime.sql
uv run python scripts/dev_token.py >/dev/null
uv run uvicorn app.main:create_app --factory --env-file .env --no-access-log
```

另开一个终端。签名脚本复用 `.local` 中的本地测试密钥，并签发短期演示令牌。

```bash
curl --fail-with-body http://127.0.0.1:8000/api/v1/health/ready
context_demo_token=$(uv run python scripts/dev_token.py)
curl --fail-with-body http://127.0.0.1:8000/api/v1/context/build \
  -H "Authorization: Bearer ${context_demo_token}" \
  -H 'Content-Type: application/json' \
  -d '{"application_id":"it-helpdesk","task_type":"general_chat","query":"打印机报错 E-77"}'
```

知识问答需要配置可访问的 `CONTEXT_RAG_URL`。接入时使用 [知识库适配器契约](docs/m1-integration.md#知识库适配器)。开发实例的交互文档位于 `http://127.0.0.1:8000/docs`。

## 验证

```bash
uv run ruff check .
uv run ruff format --check .
CONTEXT_TEST_ADMIN_DATABASE_URL=postgresql+psycopg://postgres:local-migrate@127.0.0.1:55432/postgres \
  uv run pytest
```

集成测试在配置的测试 PostgreSQL 实例中创建独立的临时数据库和角色，结束后清理。部署用独立的迁移与运行凭据，并配置应用范围、委托公钥和内网依赖。
