from dataclasses import dataclass
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import Connection, text

from app.core.auth import Identity
from app.core.builder import canonical_json
from app.core.contracts import EventRequest, SnapshotReceipt, TaskHead, TaskRestoration
from app.core.errors import ServiceError
from app.core.task_state import TaskProjection, next_status, replay


def _key(identity: Identity, task_id: str) -> dict:
    return {
        "tenant": identity.tenant_id,
        "app": identity.application_id,
        "owner": identity.sub,
        "task": task_id,
    }


def ensure_stream(conn: Connection, identity: Identity, task_id: str):
    conn.execute(
        text("""
        INSERT INTO tasks (tenant_id, application_id, owner_id, task_id)
        VALUES (:tenant, :app, :owner, :task)
        ON CONFLICT (tenant_id, application_id, owner_id, task_id) DO NOTHING
    """),
        _key(identity, task_id),
    )


def head(
    conn: Connection,
    identity: Identity,
    task_id: str,
    lock: Literal["update", "share"] | None = None,
) -> TaskHead:
    statement = """
        SELECT stream_id, task_id, status, version FROM tasks
        WHERE tenant_id = :tenant AND application_id = :app
          AND owner_id = :owner AND task_id = :task
    """
    if lock == "update":
        statement += " FOR UPDATE"
    elif lock == "share":
        statement += " FOR SHARE"
    row = (
        conn.execute(text(statement), _key(identity, task_id)).mappings().one_or_none()
    )
    if row is None:
        raise ServiceError(503, "TASK_STREAM_INCOMPLETE")
    return TaskHead.model_validate(dict(row))


def verify_head(
    conn: Connection, identity: Identity, current: TaskHead, *, write: bool = False
):
    latest = head(conn, identity, current.task_id, "update" if write else "share")
    if latest != current:
        raise ServiceError(409, "SESSION_VERSION_CONFLICT")


def append_to_stream(
    conn: Connection,
    identity: Identity,
    task_id: str,
    session_id: UUID,
    session_sequence: int,
    event: EventRequest,
) -> int:
    current = head(conn, identity, task_id, "update")
    change = event.task_update or event.operation
    if change is not None and change.expected_version != current.version:
        raise ServiceError(409, "SESSION_VERSION_CONFLICT")
    params = {"tenant": identity.tenant_id, "stream": current.stream_id}
    operation = event.operation
    if operation is not None:
        previous = conn.execute(
            text("""
            SELECT e.payload -> 'operation' FROM task_events t
            JOIN session_events e ON e.tenant_id = t.tenant_id AND e.session_id = t.session_id
                AND e.sequence = t.session_sequence
            WHERE t.tenant_id = :tenant AND t.stream_id = :stream
              AND e.payload -> 'operation' ->> 'operation_id' = :operation
            ORDER BY t.sequence DESC LIMIT 1
        """),
            {**params, "operation": operation.operation_id},
        ).scalar_one_or_none()
        if (
            previous is not None
            and previous["outcome"] != "unknown"
            and (
                previous["outcome"],
                previous.get("receipt"),
                previous.get("error_code"),
            )
            != (operation.outcome, operation.receipt, operation.error_code)
        ):
            raise ServiceError(409, "TASK_OPERATION_CONFLICT")

    version = current.version + 1
    conn.execute(
        text("""
        UPDATE tasks SET version = :version, status = :status
        WHERE tenant_id = :tenant AND stream_id = :stream
    """),
        {**params, "version": version, "status": next_status(current.status, event)},
    )
    conn.execute(
        text("""
        INSERT INTO task_events (tenant_id, stream_id, sequence, session_id, session_sequence)
        VALUES (:tenant, :stream, :version, :session, :sequence)
    """),
        {
            **params,
            "version": version,
            "session": session_id,
            "sequence": session_sequence,
        },
    )
    return version


def original_task_version(
    conn: Connection, identity: Identity, session_id: UUID, sequence: int
):
    return conn.execute(
        text("""
        SELECT sequence FROM task_events
        WHERE tenant_id = :tenant AND session_id = :session AND session_sequence = :sequence
    """),
        {"tenant": identity.tenant_id, "session": session_id, "sequence": sequence},
    ).scalar_one_or_none()


def _snapshot(conn: Connection, tenant: str, task: TaskHead):
    rows = conn.execute(
        text("""
        SELECT snapshot_id, through_sequence, payload, digest FROM task_snapshots
        WHERE tenant_id = :tenant AND stream_id = :stream AND through_sequence <= :version
        ORDER BY through_sequence DESC, created_at DESC, snapshot_id DESC
    """),
        {"tenant": tenant, "stream": task.stream_id, "version": task.version},
    )
    for snapshot_id, through, payload, digest in rows:
        if sha256(canonical_json(payload).encode()).hexdigest() != digest:
            continue
        try:
            state = TaskProjection.model_validate(payload)
        except ValidationError:
            continue
        if state.through == through:
            return SnapshotReceipt(
                snapshot_id=snapshot_id,
                stream_id=task.stream_id,
                through_sequence=through,
            ), state
    return None, TaskProjection()


@dataclass(frozen=True)
class Replay:
    task: TaskHead
    snapshot: SnapshotReceipt | None
    state: TaskProjection

    def metadata(self) -> TaskRestoration:
        pending = [
            name
            for name, operation in self.state.operations.items()
            if operation.outcome == "unknown"
        ]
        return TaskRestoration(
            task=self.task,
            snapshot=self.snapshot,
            replayed_events=self.task.version
            - (self.snapshot.through_sequence if self.snapshot else 0),
            unconfirmed_operations=pending,
            unclassified_tool_results=self.state.unclassified_tool_results,
            reconciliation_required=bool(
                pending or self.state.unclassified_tool_results
            ),
        )


def load(
    conn: Connection, identity: Identity, task_id: str, expected_version: int | None
) -> Replay:
    task = head(conn, identity, task_id)
    if expected_version is not None and task.version != expected_version:
        raise ServiceError(409, "SESSION_VERSION_CONFLICT")
    snapshot, base = _snapshot(conn, identity.tenant_id, task)
    events = conn.execute(
        text("""
        SELECT t.sequence, e.payload FROM task_events t
        JOIN session_events e ON e.tenant_id = t.tenant_id AND e.session_id = t.session_id
            AND e.sequence = t.session_sequence
        WHERE t.tenant_id = :tenant AND t.stream_id = :stream
          AND t.sequence > :through AND t.sequence <= :version ORDER BY t.sequence
    """),
        {
            "tenant": identity.tenant_id,
            "stream": task.stream_id,
            "through": base.through,
            "version": task.version,
        },
    )
    state = replay(base, events)
    if state.through != task.version or state.status != task.status:
        raise ServiceError(503, "TASK_STREAM_INCOMPLETE")
    return Replay(task=task, snapshot=snapshot, state=state)


def save(
    conn: Connection,
    identity: Identity,
    task: TaskHead,
    state: TaskProjection,
) -> tuple[Replay, bool]:
    verify_head(conn, identity, task, write=True)
    existing, stored = _snapshot(conn, identity.tenant_id, task)
    if existing is not None and existing.through_sequence == task.version:
        return Replay(task, existing, stored), False
    if state.through != task.version or state.status != task.status:
        raise ServiceError(422, "SNAPSHOT_VALIDATION_FAILED")
    payload = canonical_json(state.model_dump(mode="json"))
    snapshot_id = uuid4()
    conn.execute(
        text("""
        INSERT INTO task_snapshots (tenant_id, snapshot_id, stream_id, through_sequence, payload, digest)
        VALUES (:tenant, :id, :stream, :version, CAST(:payload AS jsonb), :digest)
    """),
        {
            "tenant": identity.tenant_id,
            "id": snapshot_id,
            "stream": task.stream_id,
            "version": task.version,
            "payload": payload,
            "digest": sha256(payload.encode()).hexdigest(),
        },
    )
    receipt = SnapshotReceipt(
        snapshot_id=snapshot_id, stream_id=task.stream_id, through_sequence=task.version
    )
    return Replay(task, receipt, state), True
