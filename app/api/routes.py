from time import perf_counter
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.adapters import postgres, rag
from app.core.auth import Identity
from app.core.builder import (
    ContextBuilder,
    ContextItem,
    canonical_json,
    knowledge_items,
)
from app.core.contracts import (
    BuildRequest,
    BuildResponse,
    ErrorResponse,
    EventRequest,
    EventResponse,
    ItemRef,
    Manifest,
    SessionRequest,
    SessionResponse,
)
from app.core.errors import ServiceError, denied
from app.core.policy import effective_policy

router = APIRouter(
    prefix="/api/v1",
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
bearer = HTTPBearer(auto_error=False)


def authenticate(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Identity:
    if credentials is None:
        raise ServiceError(401, "AUTH_REQUIRED")
    identity = request.app.state.verifier.verify(credentials.credentials)
    config = request.app.state.settings.applications.get(identity.application_id)
    if config is None or identity.tenant_id not in config.tenants:
        raise denied()
    return identity


Caller = Annotated[Identity, Depends(authenticate)]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
]


@router.get("/health/ready", responses={503: {"model": ErrorResponse}})
def readiness(request: Request):
    state = request.app.state
    postgres.check_schema(state.db)
    for application, config in state.settings.applications.items():
        for tenant in config.tenants:
            with postgres.tenant_transaction(state.db, tenant) as conn:
                if not postgres.tenant_active(conn, tenant):
                    raise ServiceError(503, "POLICY_NOT_READY")
                try:
                    effective_policy(conn, tenant, application, frozenset())
                except ServiceError:
                    raise ServiceError(503, "POLICY_NOT_READY") from None
    return {"status": "ready"}


@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"model": ErrorResponse},
    },
)
def create_session(
    request: Request, payload: SessionRequest, identity: Caller, key: IdempotencyKey
) -> SessionResponse:
    identity.require("sessions:write", payload.application_id)
    with postgres.tenant_transaction(request.app.state.db, identity.tenant_id) as conn:
        postgres.require_tenant(conn, identity)
        session_id = postgres.create_session(
            conn, identity, payload, key, request.state.request_id
        )
    return SessionResponse(session_id=session_id)


@router.post(
    "/sessions/{session_id}/events",
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"model": ErrorResponse},
    },
)
def append_event(
    request: Request,
    session_id: UUID,
    payload: EventRequest,
    identity: Caller,
    key: IdempotencyKey,
) -> EventResponse:
    identity.require("sessions:write")
    with postgres.tenant_transaction(request.app.state.db, identity.tenant_id) as conn:
        postgres.require_tenant(conn, identity)
        sequence = postgres.append_event(
            conn, identity, session_id, payload, key, request.state.request_id
        )
    return EventResponse(sequence=sequence)


@router.get("/context/{context_id}/manifest")
def manifest(request: Request, context_id: UUID, identity: Caller) -> Manifest:
    identity.require("context:audit")
    with postgres.tenant_transaction(request.app.state.db, identity.tenant_id) as conn:
        postgres.require_tenant(conn, identity)
        policy = effective_policy(
            conn,
            identity.tenant_id,
            identity.application_id,
            identity.scopes | {f"user:{identity.sub}"},
        )
        return postgres.load_manifest(
            conn, identity, context_id, policy.sources, request.state.request_id
        )


@router.post(
    "/context/build",
    responses={
        409: {"model": ErrorResponse},
        424: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
def build(request: Request, payload: BuildRequest, identity: Caller) -> BuildResponse:
    identity.require("context:build", payload.application_id)
    state = request.app.state
    config = state.settings.applications[identity.application_id]
    if payload.task_type not in config.routes:
        raise denied()
    required = payload.task_type == "knowledge_qa"
    use_knowledge = (
        required if payload.include_knowledge is None else payload.include_knowledge
    )
    if required and not use_knowledge:
        raise ServiceError(424, "EVIDENCE_UNAVAILABLE", reason="knowledge_required")
    scopes = identity.scopes | {f"user:{identity.sub}"}
    history = []
    with postgres.tenant_transaction(state.db, identity.tenant_id) as conn:
        postgres.require_tenant(conn, identity)
        policy = effective_policy(
            conn, identity.tenant_id, identity.application_id, scopes
        )
        if use_knowledge and "knowledge" not in policy.sources:
            raise denied()
        if payload.include_memory and "memory" not in policy.sources:
            raise denied()
        builder = ContextBuilder(
            payload, policy, config.model, state.counters[identity.application_id]
        )
        if payload.session_id is not None:
            task = postgres.session_task(conn, identity, payload.session_id)
            if payload.include_history and "history" in policy.sources:
                scope = f"task:{task}" if task is not None else f"user:{identity.sub}"
                history = [
                    ContextItem(
                        ItemRef(
                            source="history",
                            source_id=str(payload.session_id),
                            version=row.sequence,
                            scope=scope,
                        ),
                        canonical_json(row.payload),
                    )
                    for row in postgres.recent_events(
                        conn, identity, payload.session_id
                    )
                ]

    items = []
    warnings = ["MEMORY_NOT_ENABLED"] if payload.include_memory else []
    if use_knowledge:
        started = perf_counter()
        with state.tracer.start_as_current_span(
            "knowledge.search", record_exception=False
        ):
            try:
                if state.settings.rag_url is None:
                    raise ServiceError(424, "EVIDENCE_UNAVAILABLE", source="knowledge")
                items = rag.search(
                    state.rag,
                    state.settings.rag_url,
                    state.rag_token,
                    identity,
                    payload.query,
                )
            except ServiceError as exc:
                if required or exc.status == 403:
                    raise
                warnings.append(exc.code)
            finally:
                state.dependency_seconds.labels("knowledge").observe(
                    perf_counter() - started
                )

    knowledge, excluded, conflicts, unauthorized, missing = knowledge_items(
        items, identity
    )
    if missing and required:
        raise ServiceError(
            424, "EVIDENCE_UNAVAILABLE", reason="required_evidence_unverified"
        )
    if (
        use_knowledge
        and (not knowledge or missing)
        and not any(
            code in warnings for code in ("EVIDENCE_UNAVAILABLE", "DEPENDENCY_TIMEOUT")
        )
        and not required
    ):
        warnings.append("EVIDENCE_UNAVAILABLE")
    response, record = builder.build(
        knowledge, history, required, excluded, conflicts, unauthorized, warnings
    )
    with postgres.tenant_transaction(state.db, identity.tenant_id) as conn:
        postgres.require_tenant(conn, identity)
        if (
            effective_policy(conn, identity.tenant_id, identity.application_id, scopes)
            != policy
        ):
            raise ServiceError(409, "POLICY_CHANGED")
        if payload.session_id is not None:
            postgres.session_task(conn, identity, payload.session_id)
        postgres.save_manifest(conn, identity, record, request.state.request_id)
    state.input_tokens.labels(response.usage.counter).observe(response.usage.input)
    state.builds.labels(response.status).inc()
    return response
