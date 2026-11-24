from alembic import op

revision = "0001_m1"
down_revision = None

TABLES = (
    "tenants",
    "policy_versions",
    "sessions",
    "session_events",
    "context_builds",
    "audit_events",
)


def upgrade():
    op.execute("""
        CREATE TABLE tenants (
            tenant_id text PRIMARY KEY,
            active boolean NOT NULL DEFAULT true
        )
    """)
    op.execute("""
        CREATE TABLE policy_versions (
            tenant_id text NOT NULL REFERENCES tenants(tenant_id),
            application_id text NOT NULL DEFAULT '',
            scope text NOT NULL DEFAULT '',
            version bigint NOT NULL CHECK (version > 0),
            starts_at timestamptz NOT NULL DEFAULT now(),
            ends_at timestamptz CHECK (ends_at > starts_at),
            rules jsonb NOT NULL,
            CHECK (scope = '' OR application_id <> ''),
            PRIMARY KEY (tenant_id, application_id, scope, version)
        )
    """)
    op.execute("""
        CREATE TABLE sessions (
            tenant_id text NOT NULL REFERENCES tenants(tenant_id),
            session_id uuid NOT NULL,
            application_id text NOT NULL,
            owner_id text NOT NULL,
            task_id text,
            idempotency_key text NOT NULL,
            request_digest text NOT NULL,
            next_sequence bigint NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, session_id),
            UNIQUE (tenant_id, application_id, owner_id, idempotency_key)
        )
    """)
    op.execute("""
        CREATE TABLE session_events (
            tenant_id text NOT NULL,
            session_id uuid NOT NULL,
            sequence bigint NOT NULL,
            idempotency_key text NOT NULL,
            request_digest text NOT NULL,
            payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, session_id, sequence),
            UNIQUE (tenant_id, session_id, idempotency_key),
            FOREIGN KEY (tenant_id, session_id) REFERENCES sessions(tenant_id, session_id)
        )
    """)
    op.execute("""
        CREATE TABLE context_builds (
            tenant_id text NOT NULL REFERENCES tenants(tenant_id),
            context_id uuid NOT NULL,
            application_id text NOT NULL,
            owner_id text NOT NULL,
            session_id uuid,
            manifest jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, context_id),
            FOREIGN KEY (tenant_id, session_id) REFERENCES sessions(tenant_id, session_id)
        )
    """)
    op.execute("""
        CREATE TABLE audit_events (
            tenant_id text NOT NULL REFERENCES tenants(tenant_id),
            event_id uuid NOT NULL,
            application_id text NOT NULL,
            owner_id text NOT NULL,
            request_id uuid NOT NULL,
            action text NOT NULL,
            resource_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, event_id)
        )
    """)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_scope ON {table}
            USING (tenant_id = nullif(current_setting('context.tenant_id', true), ''))
            WITH CHECK (tenant_id = nullif(current_setting('context.tenant_id', true), ''))
        """)


def downgrade():
    for table in reversed(TABLES):
        op.drop_table(table)
