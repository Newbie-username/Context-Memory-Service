from uuid import UUID

from fastapi import APIRouter, Request

from app.adapters import memory_store, postgres
from app.adapters.memory_provider import require_memory_policy
from app.api.routes import Caller, IdempotencyKey
from app.core.auth import Identity
from app.core.contracts import ErrorResponse
from app.core.errors import ServiceError
from app.core.memory import (
    CandidateRequest,
    MemoryProvider,
    MemoryState,
    ReviewRequest,
    RevokeRequest,
    SearchRequest,
    SearchResponse,
    VersionHistory,
)

router = APIRouter(
    prefix="/api/v1",
    responses={code: {"model": ErrorResponse} for code in (401, 403, 409, 422, 503)},
)


def enabled(request: Request, identity: Identity) -> None:
    if not request.app.state.settings.applications[
        identity.application_id
    ].memory_enabled:
        raise ServiceError(503, "MEMORY_NOT_ENABLED")


@router.post("/memories/candidates", status_code=201)
def propose(
    request: Request, payload: CandidateRequest, identity: Caller, key: IdempotencyKey
) -> MemoryState:
    identity.require("memory:propose", payload.application_id)
    enabled(request, identity)
    with postgres.tenant_transaction(request.app.state.db, identity.tenant_id) as conn:
        require_memory_policy(conn, identity)
        return memory_store.propose(
            conn, identity, payload, key, request.state.request_id
        )


@router.post("/memories/search")
def search(
    request: Request, payload: SearchRequest, identity: Caller
) -> SearchResponse:
    identity.require("memory:read", payload.application_id)
    enabled(request, identity)
    provider: MemoryProvider = request.app.state.memory
    return provider.search(identity, payload.query, payload.limit)


@router.post("/memories/{memory_id}/verify")
def verify(
    request: Request, memory_id: UUID, payload: ReviewRequest, identity: Caller
) -> MemoryState:
    identity.require("memory:review")
    enabled(request, identity)
    with postgres.tenant_transaction(request.app.state.db, identity.tenant_id) as conn:
        require_memory_policy(conn, identity)
        return memory_store.review(
            conn, identity, memory_id, payload, request.state.request_id
        )


@router.post("/memories/{memory_id}/revoke")
def revoke(
    request: Request, memory_id: UUID, payload: RevokeRequest, identity: Caller
) -> MemoryState:
    enabled(request, identity)
    with postgres.tenant_transaction(request.app.state.db, identity.tenant_id) as conn:
        require_memory_policy(conn, identity)
        return memory_store.revoke(
            conn,
            identity,
            memory_id,
            payload.expected_version,
            request.state.request_id,
        )


@router.get("/memories/{memory_id}/versions")
def history(request: Request, memory_id: UUID, identity: Caller) -> VersionHistory:
    if "memory:review" not in identity.permissions:
        identity.require("memory:read")
    enabled(request, identity)
    with postgres.tenant_transaction(request.app.state.db, identity.tenant_id) as conn:
        require_memory_policy(conn, identity)
        return memory_store.history(conn, identity, memory_id, request.state.request_id)
