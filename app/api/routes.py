from time import perf_counter
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.adapters import memory_store, postgres, rag, task_runtime, task_store
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
from app.core.memory import MemoryProvider
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
    response_model_exclude_none=True,
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
        result = postgres.append_event(
            conn, identity, session_id, payload, key, request.state.request_id
        )
    return result


@router.get("/context/{context_id}/manifest")
def manifest(request: Request, context_id: UUID, identity: Caller) -> Manifest:
    identity.require("context:audit")
    with postgres.tenant_transaction(request.app.state.db, identity.tenant_id) as conn:
        postgres.require_tenant(conn, identity)
        policy = effective_policy(
            conn,
            identity.tenant_id,
            identity.application_id,
            identity.visible_scopes,
        )
        return postgres.load_manifest(
            conn, identity, context_id, policy.sources, request.state.request_id
        )


@router.post(
    "/context/build",
    response_model_exclude_none=True,
    responses={
        409: {"model": ErrorResponse},
        424: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
def build(request: Request, payload: BuildRequest, identity: Caller) -> BuildResponse:
    return build_context(request, payload, identity)


def build_context(
    request: Request,
    payload: BuildRequest,
    identity: Identity,
    expected_task_version: int | None = None,
) -> BuildResponse:
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
    scopes = identity.visible_scopes
    use_memory = payload.include_memory and config.memory_enabled
    if use_memory:
        identity.require("memory:read")
    history = []
    recovery = None
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
        if payload.task_type == "task_resume":
            if payload.session_id is None:
                raise ServiceError(422, "TASK_SESSION_REQUIRED")
            if not payload.include_history:
                raise ServiceError(422, "TASK_HISTORY_REQUIRED")
            task_runtime.require_history(policy)
            task_id = task_runtime.task_id_for(conn, identity, payload.session_id)
            recovery = task_store.load(conn, identity, task_id, expected_task_version)
        elif payload.session_id is not None:
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
    warnings = (
        ["MEMORY_NOT_ENABLED"] if payload.include_memory and not use_memory else []
    )
    if recovery is not None:
        metadata = recovery.metadata()
        task_data = recovery.state.model_dump(mode="json")
        if (
            metadata.replayed_events
            and "tasks:write" in identity.permissions
            and builder.should_compact(task_data)
        ):
            try:
                recovery = task_runtime.compact_task(
                    request, identity, payload.session_id, recovery, policy
                )
            except ServiceError as exc:
                if exc.code not in (
                    "SNAPSHOT_VALIDATION_FAILED",
                    "DEPENDENCY_TIMEOUT",
                    "EVIDENCE_UNAVAILABLE",
                ):
                    raise
                warnings.append(exc.code)
            metadata = recovery.metadata()
            task_data = recovery.state.model_dump(mode="json")
        if metadata.reconciliation_required:
            warnings.append("TASK_RECONCILIATION_REQUIRED")
        builder.attach_task(task_data, metadata)
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
    memory_hits = []
    if use_memory:
        provider: MemoryProvider = state.memory
        recall = provider.search(identity, payload.query, 8)
        memory_hits = recall.items
        warnings.extend(recall.warnings)
    response, record = builder.build(
        knowledge,
        history,
        required,
        excluded,
        conflicts,
        unauthorized,
        warnings,
        memory=[
            ContextItem(
                ItemRef(
                    source="memory",
                    source_id=str(hit.memory_id),
                    version=hit.version,
                    scope=hit.scope,
                ),
                hit.content,
            )
            for hit in memory_hits
        ],
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
        if recovery is not None:
            task_store.verify_head(conn, identity, recovery.task)
        memory_store.require_current(conn, identity, memory_hits)
        postgres.save_manifest(conn, identity, record, request.state.request_id)
    state.input_tokens.labels(response.usage.counter).observe(response.usage.input)
    state.builds.labels(response.status).inc()
    return response
