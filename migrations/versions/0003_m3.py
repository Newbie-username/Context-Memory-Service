from alembic import op

revision = "0003_m3"
down_revision = "0002_m2"

TABLES = ("tasks", "task_events", "task_snapshots")


def upgrade():
    op.execute("""
        CREATE TABLE tasks (
            tenant_id text NOT NULL REFERENCES tenants(tenant_id),
            stream_id uuid NOT NULL DEFAULT gen_random_uuid(),
            application_id text NOT NULL,
            owner_id text NOT NULL,
            task_id text NOT NULL,
            version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
            status text NOT NULL DEFAULT 'created' CHECK (status IN (
                'created', 'active', 'waiting_external', 'resumed', 'completed', 'failed', 'cancelled'
            )),
            PRIMARY KEY (tenant_id, stream_id),
            UNIQUE (tenant_id, application_id, owner_id, task_id)
        )
    """)
    op.execute("""
        CREATE TABLE task_events (
            tenant_id text NOT NULL,
            stream_id uuid NOT NULL,
            sequence bigint NOT NULL CHECK (sequence > 0),
            session_id uuid NOT NULL,
            session_sequence bigint NOT NULL,
            PRIMARY KEY (tenant_id, stream_id, sequence),
            UNIQUE (tenant_id, session_id, session_sequence),
            FOREIGN KEY (tenant_id, stream_id) REFERENCES tasks(tenant_id, stream_id),
            FOREIGN KEY (tenant_id, session_id, session_sequence)
                REFERENCES session_events(tenant_id, session_id, sequence)
        )
    """)
    op.execute("""
        CREATE TABLE task_snapshots (
            tenant_id text NOT NULL,
            snapshot_id uuid NOT NULL,
            stream_id uuid NOT NULL,
            through_sequence bigint NOT NULL CHECK (through_sequence >= 0),
            payload jsonb NOT NULL,
            digest text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, snapshot_id),
            FOREIGN KEY (tenant_id, stream_id) REFERENCES tasks(tenant_id, stream_id)
        )
    """)
    op.execute("""
        CREATE INDEX task_latest_snapshot
            ON task_snapshots (tenant_id, stream_id, through_sequence DESC, created_at DESC)
    """)
    op.execute("ALTER TABLE sessions NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE session_events NO FORCE ROW LEVEL SECURITY")
    op.execute("""
        INSERT INTO tasks (tenant_id, application_id, owner_id, task_id)
        SELECT DISTINCT tenant_id, application_id, owner_id, task_id
        FROM sessions WHERE task_id IS NOT NULL
    """)
    op.execute("""
        INSERT INTO task_events (tenant_id, stream_id, sequence, session_id, session_sequence)
        SELECT t.tenant_id, t.stream_id,
               row_number() OVER (PARTITION BY t.tenant_id, t.stream_id
                                  ORDER BY s.created_at, s.session_id, e.sequence),
               e.session_id, e.sequence
        FROM tasks t JOIN sessions s USING (tenant_id, application_id, owner_id, task_id)
        JOIN session_events e USING (tenant_id, session_id)
    """)
    op.execute("""
        UPDATE tasks t SET version = offsets.sequence, status = 'active'
        FROM (
            SELECT tenant_id, stream_id, max(sequence) AS sequence
            FROM task_events GROUP BY tenant_id, stream_id
        ) AS offsets
        WHERE t.tenant_id = offsets.tenant_id AND t.stream_id = offsets.stream_id
    """)
    op.execute("ALTER TABLE session_events FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sessions FORCE ROW LEVEL SECURITY")
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
