import json
from collections.abc import Callable
from uuid import UUID

import httpx
from sqlalchemy import Connection, Engine, text

from app.adapters import postgres
from app.adapters.embeddings import embed
from app.core.errors import ServiceError
from app.settings import EmbeddingProfile


def dispatch(
    conn: Connection,
    tenant: str,
    indexing_enabled: bool,
    send: Callable[[str, str], None],
) -> int:
    jobs = (
        conn.execute(
            text("""
        SELECT job_id FROM memory_outbox
        WHERE tenant_id = :tenant AND completed_at IS NULL
          AND (:indexing_enabled OR action = 'remove')
          AND (dispatched_at IS NULL OR dispatched_at <= now() - interval '1 minute')
        ORDER BY created_at LIMIT 50 FOR UPDATE SKIP LOCKED
    """),
            {"tenant": tenant, "indexing_enabled": indexing_enabled},
        )
        .scalars()
        .all()
    )
    for job_id in jobs:
        send(tenant, str(job_id))
        conn.execute(
            text("""
            UPDATE memory_outbox SET dispatched_at = now()
            WHERE tenant_id = :tenant AND job_id = :job
        """),
            {"tenant": tenant, "job": job_id},
        )
    return len(jobs)


def _job(conn, tenant, job_id, lock=False):
    statement = """
        SELECT o.job_id, o.memory_id, o.version AS job_version, o.action, e.application_id,
               e.status, e.version, e.content, e.expires_at > statement_timestamp() AS fresh
        FROM memory_outbox o JOIN memory_entries e USING (tenant_id, memory_id)
        WHERE o.tenant_id = :tenant AND o.job_id = :job AND o.completed_at IS NULL
    """
    if lock:
        statement += " FOR UPDATE OF o FOR SHARE OF e"
    return conn.execute(
        text(statement), {"tenant": tenant, "job": job_id}
    ).one_or_none()


def _live(job):
    return job.status == "verified" and job.version == job.job_version and job.fresh


def process(
    database: Engine,
    client: httpx.Client,
    tenant: str,
    job_id: UUID,
    profile: EmbeddingProfile | None,
    enabled_applications: frozenset[str],
) -> None:
    with postgres.tenant_transaction(database, tenant) as conn:
        job = _job(conn, tenant, job_id)
        if job is None:
            return
        index = (
            job.action == "index"
            and _live(job)
            and postgres.tenant_active(conn, tenant)
            and job.application_id in enabled_applications
        )

    embedding = None
    if index:
        if profile is None:
            raise ServiceError(503, "EMBEDDING_NOT_CONFIGURED")
        embedding = embed(client, profile, job.content)

    with postgres.tenant_transaction(database, tenant) as conn:
        current = _job(conn, tenant, job_id, lock=True)
        if current is None:
            return
        params = {
            "tenant": tenant,
            "id": current.memory_id,
            "version": current.job_version,
            "job": job_id,
        }
        if current.action == "remove":
            conn.execute(
                text("""
                DELETE FROM memory_embeddings
                WHERE tenant_id = :tenant AND memory_id = :id AND version <= :version
            """),
                params,
            )
        if (
            embedding is not None
            and _live(current)
            and postgres.tenant_active(conn, tenant)
        ):
            conn.execute(
                text("""
                INSERT INTO memory_embeddings
                    (tenant_id, memory_id, version, model_version, dimensions, embedding)
                VALUES (:tenant, :id, :version, :model, :dimensions, CAST(:embedding AS vector))
                ON CONFLICT DO NOTHING
            """),
                {
                    **params,
                    "model": profile.model_version,
                    "dimensions": len(embedding),
                    "embedding": json.dumps(embedding),
                },
            )
        conn.execute(
            text("""
            UPDATE memory_outbox SET completed_at = now()
            WHERE tenant_id = :tenant AND job_id = :job
        """),
            params,
        )
