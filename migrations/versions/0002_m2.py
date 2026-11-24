from alembic import op

revision = "0002_m2"
down_revision = "0001_m1"

TABLES = (
    "memory_entries",
    "memory_versions",
    "memory_evidence",
    "memory_embeddings",
    "memory_outbox",
)


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("""
        CREATE TABLE memory_entries (
            tenant_id text NOT NULL REFERENCES tenants(tenant_id),
            memory_id uuid NOT NULL,
            application_id text NOT NULL,
            owner_id text NOT NULL,
            scope text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('user_preference', 'resolved_incident')),
            topic text NOT NULL,
            content text NOT NULL,
            model_version text NOT NULL,
            status text NOT NULL CHECK (
                status IN ('candidate', 'pending_review', 'verified', 'rejected',
                           'superseded', 'expired', 'revoked')
            ),
            version bigint NOT NULL CHECK (version > 0),
            expires_at timestamptz NOT NULL,
            idempotency_key text NOT NULL,
            request_digest text NOT NULL,
            PRIMARY KEY (tenant_id, memory_id),
            UNIQUE (tenant_id, application_id, owner_id, idempotency_key)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX memory_verified_topic
        ON memory_entries (tenant_id, application_id, scope, kind, topic)
        WHERE status = 'verified'
    """)
    op.execute("""
        CREATE INDEX memory_expiration
        ON memory_entries (tenant_id, application_id, expires_at)
        WHERE status IN ('pending_review', 'verified')
    """)
    op.execute("""
        CREATE TABLE memory_versions (
            tenant_id text NOT NULL,
            memory_id uuid NOT NULL,
            version bigint NOT NULL,
            status text NOT NULL,
            actor_id text NOT NULL,
            reason text NOT NULL,
            changed_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, memory_id, version),
            FOREIGN KEY (tenant_id, memory_id) REFERENCES memory_entries(tenant_id, memory_id)
        )
    """)
    op.execute("""
        CREATE TABLE memory_evidence (
            tenant_id text NOT NULL,
            memory_id uuid NOT NULL,
            session_id uuid NOT NULL,
            event_sequence bigint NOT NULL,
            PRIMARY KEY (tenant_id, memory_id),
            FOREIGN KEY (tenant_id, memory_id) REFERENCES memory_entries(tenant_id, memory_id),
            FOREIGN KEY (tenant_id, session_id, event_sequence)
              REFERENCES session_events(tenant_id, session_id, sequence)
        )
    """)
    op.execute("""
        CREATE INDEX memory_source_event ON memory_evidence (tenant_id, session_id, event_sequence)
    """)
    op.execute("""
        CREATE TABLE memory_embeddings (
            tenant_id text NOT NULL,
            memory_id uuid NOT NULL,
            version bigint NOT NULL,
            model_version text NOT NULL,
            dimensions integer NOT NULL CHECK (dimensions BETWEEN 1 AND 2000),
            embedding vector NOT NULL CHECK (vector_dims(embedding) = dimensions),
            PRIMARY KEY (tenant_id, memory_id, version, model_version),
            FOREIGN KEY (tenant_id, memory_id, version)
              REFERENCES memory_versions(tenant_id, memory_id, version)
        )
    """)
    op.execute("""
        CREATE TABLE memory_outbox (
            tenant_id text NOT NULL,
            job_id uuid NOT NULL,
            memory_id uuid NOT NULL,
            version bigint NOT NULL,
            action text NOT NULL CHECK (action IN ('index', 'remove')),
            created_at timestamptz NOT NULL DEFAULT now(),
            dispatched_at timestamptz,
            completed_at timestamptz,
            PRIMARY KEY (tenant_id, job_id),
            UNIQUE (tenant_id, memory_id, version, action),
            FOREIGN KEY (tenant_id, memory_id, version)
              REFERENCES memory_versions(tenant_id, memory_id, version)
        )
    """)
    op.execute("""
        CREATE INDEX memory_pending_jobs ON memory_outbox (tenant_id, created_at)
        WHERE completed_at IS NULL
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
