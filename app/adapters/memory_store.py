import json
import re
from collections.abc import Sequence
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from app.adapters.postgres import record_audit, session_task
from app.core.auth import Identity
from app.core.errors import ServiceError, denied
from app.core.memory import (
    CandidateRequest,
    MemoryHit,
    MemoryState,
    ReviewRequest,
    SourceEvent,
    Version,
    VersionHistory,
    VersionRef,
)

# Explicit credential and common sensitive identifier formats at the memory boundary.
_SENSITIVE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:password|passwd|api[_ -]?key|secret|token)"
    r"\s*[:=]\s*\S+|\bBearer\s+\S+|\b(?:gh[pousr]_|github_pat_|sk-|AKIA)[\w-]{8,}"
    r"|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|\b\d{3}-\d{2}-\d{4}\b",
    re.IGNORECASE,
)

_TERM = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*|[\u3400-\u9fff]+")


def _lock_writes(conn: Connection, tenant: str, application: str):
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": json.dumps([tenant, application], separators=(",", ":"))},
    )


def _identity_params(identity: Identity) -> dict:
    return {
        "tenant": identity.tenant_id,
        "app": identity.application_id,
        "scopes": sorted(identity.visible_scopes),
    }


def _transition(conn, row, status, actor, reason, request_id) -> MemoryState:
    version = row.version + 1
    params = {
        "tenant": row.tenant_id,
        "id": row.memory_id,
        "version": version,
        "status": status,
        "actor": actor.sub,
        "reason": reason,
    }
    conn.execute(
        text("""
        UPDATE memory_entries SET version = :version, status = :status
        WHERE tenant_id = :tenant AND memory_id = :id
    """),
        params,
    )
    conn.execute(
        text("""
        INSERT INTO memory_versions (tenant_id, memory_id, version, status, actor_id, reason)
        VALUES (:tenant, :id, :version, :status, :actor, :reason)
    """),
        params,
    )
    if status in ("verified", "expired", "revoked", "superseded"):
        conn.execute(
            text("""
            INSERT INTO memory_outbox (tenant_id, job_id, memory_id, version, action)
            VALUES (:tenant, :job, :id, :version, :action)
        """),
            {
                **params,
                "job": uuid4(),
                "action": "index" if status == "verified" else "remove",
            },
        )
    record_audit(conn, actor, request_id, f"memory_{status}", row.memory_id)
    return MemoryState(memory_id=row.memory_id, version=version, status=status)


def _row(conn: Connection, identity: Identity, memory_id: UUID):
    row = conn.execute(
        text("""
        SELECT *, expires_at > statement_timestamp() AS fresh
        FROM memory_entries WHERE tenant_id = :tenant AND application_id = :app
          AND memory_id = :id AND scope = ANY(CAST(:scopes AS text[]))
    """),
        {**_identity_params(identity), "id": memory_id},
    ).one_or_none()
    if row is None:
        raise denied()
    return row


def _verified_topic(conn, row, actor, request_id):
    existing = conn.execute(
        text("""
        SELECT *, expires_at > statement_timestamp() AS fresh FROM memory_entries
        WHERE tenant_id = :tenant AND application_id = :app AND scope = :scope
          AND kind = :kind AND topic = :topic AND status = 'verified'
    """),
        {
            "tenant": row.tenant_id,
            "app": row.application_id,
            "scope": row.scope,
            "kind": row.kind,
            "topic": row.topic,
        },
    ).one_or_none()
    if existing is not None and not existing.fresh:
        _transition(conn, existing, "expired", actor, "retention_ended", request_id)
        return None
    return existing


def propose(
    conn: Connection,
    identity: Identity,
    payload: CandidateRequest,
    key: str,
    request_id: UUID,
):
    if payload.scope not in identity.visible_scopes:
        raise denied()
    personal = payload.kind == "user_preference"
    if personal != payload.scope.startswith("user:"):
        raise ServiceError(422, "MEMORY_SCOPE_INVALID")
    if any(
        _SENSITIVE.search(value)
        for value in (payload.content, payload.topic, payload.model_version)
    ):
        raise ServiceError(422, "SENSITIVE_MEMORY_CONTENT")

    session_task(conn, identity, payload.source.session_id)
    source = conn.execute(
        text("""
        SELECT payload FROM session_events WHERE tenant_id = :tenant
        AND session_id = :session AND sequence = :sequence
    """),
        {
            "tenant": identity.tenant_id,
            "session": payload.source.session_id,
            "sequence": payload.source.sequence,
        },
    ).scalar_one_or_none()
    if source is None:
        raise denied()
    expected_kind = "user_message" if personal else "tool_result"
    if source["kind"] != expected_kind or (
        payload.confirmed
        and (not personal or source["content"].strip() != payload.content)
    ):
        raise ServiceError(422, "MEMORY_EVIDENCE_INVALID")

    _lock_writes(conn, identity.tenant_id, identity.application_id)
    digest = sha256(payload.model_dump_json().encode()).hexdigest()
    params = {
        **_identity_params(identity),
        "owner": identity.sub,
        "key": key,
        "scope": payload.scope,
        "kind": payload.kind,
        "topic": payload.topic,
        "content": payload.content,
        "model": payload.model_version,
        "expires": payload.expires_at,
        "session": payload.source.session_id,
        "sequence": payload.source.sequence,
        "digest": digest,
    }
    previous = conn.execute(
        text("""
        SELECT e.memory_id, e.version, e.status, e.request_digest
        FROM memory_entries e JOIN memory_evidence s USING (tenant_id, memory_id)
        WHERE e.tenant_id = :tenant AND e.application_id = :app AND e.owner_id = :owner
          AND (e.idempotency_key = :key OR (
              s.session_id = :session AND s.event_sequence = :sequence
              AND e.scope = :scope AND e.kind = :kind))
    """),
        params,
    ).all()
    if previous:
        if any(row.request_digest != digest for row in previous):
            raise ServiceError(409, "MEMORY_CANDIDATE_CONFLICT")
        row = previous[0]
        return MemoryState(
            memory_id=row.memory_id, version=row.version, status=row.status
        )
    if not conn.execute(
        text("SELECT :expires > statement_timestamp()"), params
    ).scalar_one():
        raise ServiceError(422, "MEMORY_EXPIRATION_INVALID")

    memory_id = uuid4()
    params["id"] = memory_id
    row = conn.execute(
        text("""
        INSERT INTO memory_entries
            (tenant_id, memory_id, application_id, owner_id, scope, kind, topic, content,
             model_version, status, version, expires_at, idempotency_key, request_digest)
        VALUES (:tenant, :id, :app, :owner, :scope, :kind, :topic, :content, :model,
                'candidate', 1, :expires, :key, :digest) RETURNING *
    """),
        params,
    ).one()
    conn.execute(
        text("""
        INSERT INTO memory_evidence (tenant_id, memory_id, session_id, event_sequence)
        VALUES (:tenant, :id, :session, :sequence)
    """),
        params,
    )
    conn.execute(
        text("""
        INSERT INTO memory_versions (tenant_id, memory_id, version, status, actor_id, reason)
        VALUES (:tenant, :id, 1, 'candidate', :owner, 'submitted')
    """),
        params,
    )
    pending = _transition(
        conn, row, "pending_review", identity, "evidence_attached", request_id
    )
    if payload.confirmed and _verified_topic(conn, row, identity, request_id) is None:
        row = _row(conn, identity, memory_id)
        return _transition(
            conn, row, "verified", identity, "personal_preference_confirmed", request_id
        )
    return pending


def review(
    conn: Connection,
    identity: Identity,
    memory_id: UUID,
    payload: ReviewRequest,
    request_id: UUID,
):
    _lock_writes(conn, identity.tenant_id, identity.application_id)
    row = _row(conn, identity, memory_id)
    if row.kind == "resolved_incident" and identity.sub == row.owner_id:
        raise denied()
    if (
        row.version != payload.expected_version
        or row.status != "pending_review"
        or not row.fresh
    ):
        raise ServiceError(409, "MEMORY_VERSION_CONFLICT")
    if payload.decision == "reject":
        if payload.replaces is not None:
            raise ServiceError(422, "MEMORY_REPLACEMENT_INVALID")
        return _transition(
            conn, row, "rejected", identity, "review_rejected", request_id
        )

    existing = _verified_topic(conn, row, identity, request_id)
    if existing is not None and payload.replaces is None:
        raise ServiceError(409, "MEMORY_TOPIC_CONFLICT")
    if payload.replaces is not None:
        replacement = payload.replaces
        if existing is None or (existing.memory_id, existing.version) != (
            replacement.memory_id,
            replacement.version,
        ):
            raise ServiceError(409, "MEMORY_VERSION_CONFLICT")
        _transition(
            conn, existing, "superseded", identity, "review_replaced", request_id
        )
    return _transition(conn, row, "verified", identity, "review_approved", request_id)


def revoke(
    conn: Connection,
    identity: Identity,
    memory_id: UUID,
    expected_version: int,
    request_id: UUID,
):
    _lock_writes(conn, identity.tenant_id, identity.application_id)
    row = _row(conn, identity, memory_id)
    if "memory:review" not in identity.permissions and not (
        row.kind == "user_preference"
        and row.owner_id == identity.sub
        and "memory:propose" in identity.permissions
    ):
        raise denied()
    if row.version != expected_version or row.status != "verified" or not row.fresh:
        raise ServiceError(409, "MEMORY_VERSION_CONFLICT")
    return _transition(
        conn, row, "revoked", identity, "revocation_requested", request_id
    )


def history(conn: Connection, identity: Identity, memory_id: UUID, request_id: UUID):
    row = _row(conn, identity, memory_id)
    if row.owner_id != identity.sub and "memory:review" not in identity.permissions:
        raise denied()
    params = {"tenant": identity.tenant_id, "id": memory_id}
    evidence = (
        conn.execute(
            text("""
        SELECT session_id, event_sequence AS sequence FROM memory_evidence
        WHERE tenant_id = :tenant AND memory_id = :id
    """),
            params,
        )
        .mappings()
        .one()
    )
    versions = (
        conn.execute(
            text("""
        SELECT version, status, changed_at, reason FROM memory_versions
        WHERE tenant_id = :tenant AND memory_id = :id ORDER BY version
    """),
            params,
        )
        .mappings()
        .all()
    )
    record_audit(conn, identity, request_id, "memory_history_read", memory_id)
    return VersionHistory(
        memory_id=memory_id,
        source=SourceEvent.model_validate(dict(evidence)),
        versions=[Version.model_validate(dict(version)) for version in versions],
        review_content=row.content
        if row.status == "pending_review" and row.fresh
        else None,
    )


def expire_due(conn: Connection, tenant: str, application: str) -> None:
    _lock_writes(conn, tenant, application)
    rows = conn.execute(
        text("""
        SELECT * FROM memory_entries
        WHERE tenant_id = :tenant AND application_id = :app
          AND status IN ('verified', 'pending_review') AND expires_at <= now()
        ORDER BY expires_at LIMIT 100
    """),
        {"tenant": tenant, "app": application},
    ).all()
    if rows:
        worker = Identity(
            sub="memory-worker",
            tenant_id=tenant,
            application_id=application,
            permissions=frozenset(),
            scopes=frozenset(),
        )
        for row in rows:
            _transition(conn, row, "expired", worker, "retention_ended", uuid4())


def _terms(query: str) -> list[str]:
    tokens = []
    for match in _TERM.finditer(query.lower()):
        value = match.group()
        if len(value) > 1 and "\u3400" <= value[0] <= "\u9fff":
            tokens.extend(
                value[index : index + 2] for index in range(min(len(value) - 1, 32))
            )
        else:
            tokens.append(value)
        if len(tokens) >= 32:
            break
    return list(dict.fromkeys(tokens))[:32]


def search(
    conn: Connection,
    identity: Identity,
    query: str,
    limit: int,
    embedding: list[float] | None,
    model_version: str | None,
) -> list[MemoryHit]:
    params = {
        **_identity_params(identity),
        "terms": _terms(query),
        "limit": limit,
        "model": model_version,
        "dimensions": len(embedding) if embedding else 0,
        "embedding": json.dumps(embedding) if embedding else None,
    }
    rows = conn.execute(
        text("""
        WITH visible AS MATERIALIZED (
            SELECT memory_id, tenant_id, version, kind, scope, topic, content,
                   model_version, expires_at
            FROM memory_entries
            WHERE tenant_id = :tenant AND application_id = :app
              AND status = 'verified' AND expires_at > now()
              AND scope = ANY(CAST(:scopes AS text[]))
        ), scores AS (
            SELECT v.*, (SELECT count(*) FROM unnest(CAST(:terms AS text[])) AS tokens(token)
                WHERE CASE WHEN token ~ '^[a-z0-9]+[-_.][a-z0-9]' THEN EXISTS (
                    SELECT 1 FROM regexp_split_to_table(lower(v.content || ' ' || v.topic),
                        '[^a-z0-9_.-]+') AS words(word)
                    WHERE trim(both '.-' from word) = token
                ) ELSE strpos(lower(v.content), token) > 0 OR strpos(lower(v.topic), token) > 0 END
            ) AS keywords,
            coalesce(1 - (e.embedding <=> CAST(:embedding AS vector)), 0) AS similarity
            FROM visible v LEFT JOIN memory_embeddings e
              ON e.tenant_id = v.tenant_id AND e.memory_id = v.memory_id
              AND e.version = v.version AND e.model_version = :model AND e.dimensions = :dimensions
        )
        SELECT memory_id, version, kind, scope, topic, content, model_version, expires_at
        FROM scores WHERE keywords > 0 OR similarity >= 0.55
        ORDER BY keywords + similarity DESC, memory_id LIMIT :limit
    """),
        params,
    ).mappings()
    return [MemoryHit.model_validate(dict(row)) for row in rows]


def require_current(
    conn: Connection, identity: Identity, refs: Sequence[VersionRef]
) -> None:
    if not refs:
        return
    rows = conn.execute(
        text("""
        SELECT memory_id, version FROM memory_entries WHERE tenant_id = :tenant
          AND application_id = :app AND memory_id = ANY(CAST(:ids AS uuid[]))
          AND status = 'verified' AND expires_at > statement_timestamp()
          AND scope = ANY(CAST(:scopes AS text[]))
        FOR SHARE
    """),
        {**_identity_params(identity), "ids": [ref.memory_id for ref in refs]},
    ).all()
    if {(row.memory_id, row.version) for row in rows} != {
        (ref.memory_id, ref.version) for ref in refs
    }:
        raise ServiceError(409, "MEMORY_VERSION_CONFLICT")
