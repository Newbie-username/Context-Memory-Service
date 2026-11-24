from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, create_engine, event, text

from app.core.auth import Identity
from app.core.contracts import EventRequest, Manifest, SessionRequest, Source
from app.core.errors import ServiceError, denied

SCHEMA_VERSION = "0002_m2"

_RUNTIME_ROLE_CHECK = """
    SELECT rolsuper OR rolbypassrls OR EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname IN (
              'tenants', 'policy_versions', 'sessions',
              'session_events', 'context_builds', 'audit_events', 'memory_entries',
              'memory_versions', 'memory_evidence', 'memory_embeddings', 'memory_outbox'
          )
          AND pg_has_role(c.relowner, 'MEMBER')
    ) FROM pg_roles WHERE rolname = current_user
"""


def connect_database(url: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name != "postgresql":
        raise ValueError("PostgreSQL is required")

    @event.listens_for(engine, "connect")
    def verify_runtime_role(dbapi_connection, _record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute(_RUNTIME_ROLE_CHECK)
            if cursor.fetchone()[0]:
                raise ServiceError(503, "DATABASE_ROLE_INVALID")
        dbapi_connection.rollback()

    return engine


def check_schema(engine: Engine) -> None:
    with engine.connect() as conn:
        revision = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        if revision != SCHEMA_VERSION:
            raise ServiceError(503, "DATABASE_SCHEMA_NOT_READY")


@contextmanager
def tenant_transaction(engine: Engine, tenant_id: str) -> Iterator[Connection]:
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('context.tenant_id', :tenant, true)"),
            {"tenant": tenant_id},
        )
        yield conn


def tenant_active(conn: Connection, tenant_id: str) -> bool:
    active = conn.execute(
        text("""
        SELECT active FROM tenants WHERE tenant_id = :tenant
    """),
        {"tenant": tenant_id},
    ).scalar_one_or_none()
    return active is True


def require_tenant(conn: Connection, identity: Identity) -> None:
    if not tenant_active(conn, identity.tenant_id):
        raise denied()


def record_audit(
    conn: Connection, identity: Identity, request_id: UUID, action: str, resource: UUID
):
    conn.execute(
        text("""
        INSERT INTO audit_events
            (tenant_id, event_id, application_id, owner_id, request_id, action, resource_id)
        VALUES (:tenant, :id, :application, :owner, :request, :action, :resource)
    """),
        {
            "tenant": identity.tenant_id,
            "id": uuid4(),
            "application": identity.application_id,
            "owner": identity.sub,
            "request": request_id,
            "action": action,
            "resource": resource,
        },
    )


def create_session(
    conn: Connection,
    identity: Identity,
    payload: SessionRequest,
    key: str,
    request_id: UUID,
) -> UUID:
    identity.require_task(payload.task_id)
    params = {
        "tenant": identity.tenant_id,
        "id": uuid4(),
        "application": identity.application_id,
        "owner": identity.sub,
        "task": payload.task_id,
        "key": key,
        "digest": sha256(payload.model_dump_json().encode()).hexdigest(),
    }
    created = conn.execute(
        text("""
        INSERT INTO sessions
            (tenant_id, session_id, application_id, owner_id, task_id, idempotency_key, request_digest)
        VALUES (:tenant, :id, :application, :owner, :task, :key, :digest)
        ON CONFLICT (tenant_id, application_id, owner_id, idempotency_key) DO NOTHING
        RETURNING session_id
    """),
        params,
    ).scalar_one_or_none()
    if created is not None:
        record_audit(conn, identity, request_id, "session_created", created)
        return created

    previous = conn.execute(
        text("""
        SELECT session_id, request_digest FROM sessions
        WHERE tenant_id = :tenant AND application_id = :application
          AND owner_id = :owner AND idempotency_key = :key
    """),
        params,
    ).one()
    if previous.request_digest != params["digest"]:
        raise ServiceError(409, "IDEMPOTENCY_CONFLICT")
    return previous.session_id


def session_task(
    conn: Connection, identity: Identity, session_id: UUID, lock: bool = False
):
    statement = """
        SELECT task_id FROM sessions WHERE tenant_id = :tenant AND session_id = :id
          AND application_id = :application AND owner_id = :owner
    """
    if lock:
        statement += " FOR UPDATE"
    row = conn.execute(
        text(statement),
        {
            "tenant": identity.tenant_id,
            "id": session_id,
            "application": identity.application_id,
            "owner": identity.sub,
        },
    ).one_or_none()
    if row is None:
        raise denied()
    identity.require_task(row.task_id)
    return row.task_id


def append_event(
    conn: Connection,
    identity: Identity,
    session_id: UUID,
    payload: EventRequest,
    key: str,
    request_id: UUID,
) -> int:
    session_task(conn, identity, session_id, lock=True)
    params = {
        "tenant": identity.tenant_id,
        "session": session_id,
        "key": key,
        "payload": payload.model_dump_json(),
    }
    digest = sha256(params["payload"].encode()).hexdigest()
    previous = conn.execute(
        text("""
        SELECT sequence, request_digest FROM session_events
        WHERE tenant_id = :tenant AND session_id = :session AND idempotency_key = :key
    """),
        params,
    ).one_or_none()
    if previous is not None:
        if previous.request_digest != digest:
            raise ServiceError(409, "IDEMPOTENCY_CONFLICT")
        return previous.sequence

    sequence = conn.execute(
        text("""
        UPDATE sessions SET next_sequence = next_sequence + 1
        WHERE tenant_id = :tenant AND session_id = :session RETURNING next_sequence - 1
    """),
        params,
    ).scalar_one()
    conn.execute(
        text("""
        INSERT INTO session_events
            (tenant_id, session_id, sequence, idempotency_key, request_digest, payload)
        VALUES (:tenant, :session, :sequence, :key, :digest, CAST(:payload AS jsonb))
    """),
        {**params, "sequence": sequence, "digest": digest},
    )
    record_audit(conn, identity, request_id, "session_event_appended", session_id)
    return sequence


def recent_events(conn: Connection, identity: Identity, session_id: UUID):
    return conn.execute(
        text("""
        SELECT sequence, payload FROM session_events
        WHERE tenant_id = :tenant AND session_id = :session
        ORDER BY sequence DESC LIMIT 20
    """),
        {"tenant": identity.tenant_id, "session": session_id},
    ).all()


def save_manifest(
    conn: Connection, identity: Identity, manifest: Manifest, request_id: UUID
):
    conn.execute(
        text("""
        INSERT INTO context_builds
            (tenant_id, context_id, application_id, owner_id, session_id, manifest)
        VALUES (:tenant, :id, :application, :owner, :session, CAST(:manifest AS jsonb))
    """),
        {
            "tenant": identity.tenant_id,
            "id": manifest.context_id,
            "session": manifest.session_id,
            "application": identity.application_id,
            "owner": identity.sub,
            "manifest": manifest.model_dump_json(),
        },
    )
    record_audit(conn, identity, request_id, "context_built", manifest.context_id)


def load_manifest(
    conn: Connection,
    identity: Identity,
    context_id: UUID,
    permitted_sources: frozenset[Source],
    request_id: UUID,
) -> Manifest:
    payload = conn.execute(
        text("""
        SELECT manifest FROM context_builds WHERE tenant_id = :tenant AND context_id = :id
          AND application_id = :application AND owner_id = :owner
    """),
        {
            "tenant": identity.tenant_id,
            "id": context_id,
            "application": identity.application_id,
            "owner": identity.sub,
        },
    ).scalar_one_or_none()
    if payload is None:
        raise denied()

    manifest = Manifest.model_validate(payload)
    if manifest.session_id is not None:
        session_task(conn, identity, manifest.session_id)
    refs = manifest.references + [entry.item for entry in manifest.excluded]
    refs.extend(ref for conflict in manifest.conflicts for ref in conflict.items)
    visible_scopes = identity.visible_scopes
    if any(
        ref.scope not in visible_scopes or ref.source not in permitted_sources
        for ref in refs
    ):
        raise denied()
    if not manifest.policy_versions.keys() <= visible_scopes | {
        "tenant",
        "application",
    }:
        raise denied()
    record_audit(conn, identity, request_id, "manifest_read", context_id)
    return manifest
