from uuid import UUID

from fastapi import Request

from app.adapters import postgres, task_store, task_summary
from app.core.auth import Identity
from app.core.errors import ServiceError, denied
from app.core.policy import EffectivePolicy, effective_policy


def require_history(policy: EffectivePolicy) -> None:
    if "history" not in policy.sources:
        raise denied()


def task_id_for(conn, identity: Identity, session_id: UUID) -> str:
    task_id = postgres.session_task(conn, identity, session_id)
    if task_id is None:
        raise ServiceError(422, "TASK_SESSION_REQUIRED")
    return task_id


def compact_task(
    request: Request,
    identity: Identity,
    session_id: UUID,
    recovery: task_store.Replay,
    policy: EffectivePolicy,
) -> task_store.Replay:
    if (
        recovery.snapshot is not None
        and recovery.snapshot.through_sequence == recovery.task.version
    ):
        return recovery
    state = request.app.state
    compacted = task_summary.compact(recovery.state, state.rag, state.settings)
    with postgres.tenant_transaction(state.db, identity.tenant_id) as conn:
        postgres.require_tenant(conn, identity)
        latest_policy = effective_policy(
            conn, identity.tenant_id, identity.application_id, identity.visible_scopes
        )
        require_history(latest_policy)
        if latest_policy != policy:
            raise ServiceError(409, "POLICY_CHANGED")
        task_id_for(conn, identity, session_id)
        active, created = task_store.save(conn, identity, recovery.task, compacted)
        if created:
            postgres.record_audit(
                conn,
                identity,
                request.state.request_id,
                "task_snapshot_created",
                active.snapshot.snapshot_id,
            )
    return active
