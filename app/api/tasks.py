from uuid import UUID

from fastapi import APIRouter, Request

from app.adapters import postgres, task_runtime, task_store
from app.api.routes import Caller, build_context
from app.core.auth import Identity, Permission
from app.core.contracts import (
    BuildRequest,
    BuildResponse,
    CompactRequest,
    ErrorResponse,
    RestoreRequest,
    SnapshotReceipt,
    TaskHead,
)
from app.core.errors import denied
from app.core.policy import effective_policy

router = APIRouter(
    prefix="/api/v1",
    responses={
        code: {"model": ErrorResponse} for code in (401, 403, 409, 422, 424, 503, 504)
    },
)


def require_route(request: Request, identity: Identity, permission: Permission):
    identity.require(permission)
    if (
        "task_resume"
        not in request.app.state.settings.applications[identity.application_id].routes
    ):
        raise denied()


def authorized_task(conn, identity: Identity, session_id: UUID):
    postgres.require_tenant(conn, identity)
    policy = effective_policy(
        conn, identity.tenant_id, identity.application_id, identity.visible_scopes
    )
    task_runtime.require_history(policy)
    return task_runtime.task_id_for(conn, identity, session_id), policy


@router.get("/sessions/{session_id}/task")
def head(request: Request, session_id: UUID, identity: Caller) -> TaskHead:
    require_route(request, identity, "context:build")
    with postgres.tenant_transaction(request.app.state.db, identity.tenant_id) as conn:
        task_id, _ = authorized_task(conn, identity, session_id)
        return task_store.head(conn, identity, task_id)


@router.post("/sessions/{session_id}/compact")
def compact(
    request: Request, session_id: UUID, payload: CompactRequest, identity: Caller
) -> SnapshotReceipt:
    require_route(request, identity, "tasks:write")
    with postgres.tenant_transaction(request.app.state.db, identity.tenant_id) as conn:
        task_id, policy = authorized_task(conn, identity, session_id)
        recovery = task_store.load(conn, identity, task_id, payload.expected_version)
    return task_runtime.compact_task(
        request, identity, session_id, recovery, policy
    ).snapshot


@router.post("/sessions/{session_id}/restore", response_model_exclude_none=True)
def restore(
    request: Request, session_id: UUID, payload: RestoreRequest, identity: Caller
) -> BuildResponse:
    return build_context(
        request,
        BuildRequest(
            application_id=identity.application_id,
            session_id=session_id,
            task_type="task_resume",
            **payload.model_dump(exclude={"expected_version"}),
        ),
        identity,
        payload.expected_version,
    )
