from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.adapters.memory_provider import PostgresMemory
from app.adapters.postgres import connect_database
from app.api.memories import router as memory_router
from app.api.routes import router
from app.core.auth import DelegationVerifier
from app.core.builder import model_counter
from app.core.errors import ServiceError
from app.settings import Settings


def create_app(
    settings: Settings | None = None, rag_client: httpx.Client | None = None
) -> FastAPI:
    settings = settings or Settings()
    db = connect_database(settings.database_url.get_secret_value())
    verifier = DelegationVerifier(
        settings.jwt_public_key_file.read_bytes(),
        settings.jwt_issuer,
        settings.jwt_audience,
    )
    counters = {
        name: model_counter(config.model)
        for name, config in settings.applications.items()
    }
    provider = TracerProvider(
        resource=Resource.create({"service.name": "context-memory-service"})
    )
    if settings.otlp_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            app.state.rag.close()
            db.dispose()
            provider.shutdown()

    app = FastAPI(title="Context & Memory Service", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    app.include_router(memory_router)
    state = app.state
    state.settings = settings
    state.rag_token = (
        settings.rag_token.get_secret_value() if settings.rag_token else None
    )
    state.rag = rag_client or httpx.Client(
        timeout=settings.rag_timeout_seconds, follow_redirects=False, trust_env=False
    )
    state.db = db
    state.memory = PostgresMemory(db, state.rag, settings.embedding)
    state.verifier = verifier
    state.counters = counters
    state.tracer = provider.get_tracer(__name__)
    metrics = CollectorRegistry()
    requests = Histogram(
        "context_request_seconds", "API latency", ["route", "status"], registry=metrics
    )
    state.dependency_seconds = Histogram(
        "context_dependency_seconds",
        "Dependency latency",
        ["dependency"],
        registry=metrics,
    )
    state.input_tokens = Histogram(
        "context_input_tokens",
        "Model input size",
        ["counter"],
        registry=metrics,
        buckets=(256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536),
    )
    state.builds = Counter(
        "context_builds_total", "Persisted builds", ["status"], registry=metrics
    )

    @app.get("/metrics", include_in_schema=False)
    def prometheus():
        return Response(generate_latest(metrics), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def track(request: Request, call_next):
        request.state.request_id = uuid4()
        started = perf_counter()
        with state.tracer.start_as_current_span(
            "context.request", record_exception=False, set_status_on_exception=False
        ) as span:
            response = await call_next(request)
            route = request.scope.get("route")
            path = getattr(route, "path", "unmatched")
            span.set_attribute("http.route", path)
            span.set_attribute("http.response.status_code", response.status_code)
            span.set_attribute("request.id", str(request.state.request_id))
            requests.labels(path, str(response.status_code)).observe(
                perf_counter() - started
            )
        response.headers["X-Request-ID"] = str(request.state.request_id)
        response.headers["Cache-Control"] = "no-store"
        return response

    def error(request: Request, status: int, code: str, details: dict) -> JSONResponse:
        request_id = str(request.state.request_id)
        headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
        if status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(
            {"code": code, "request_id": request_id, "details": details},
            status_code=status,
            headers=headers,
        )

    @app.exception_handler(ServiceError)
    def domain_error(request: Request, exc: ServiceError):
        return error(request, exc.status, exc.code, exc.details)

    @app.exception_handler(RequestValidationError)
    def validation_error(request: Request, exc: RequestValidationError):
        return error(request, 422, "VALIDATION_ERROR", {})

    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException):
        return error(request, exc.status_code, f"HTTP_{exc.status_code}", {})

    @app.exception_handler(SQLAlchemyError)
    def database_error(request: Request, exc: SQLAlchemyError):
        return error(request, 503, "DATABASE_UNAVAILABLE", {})

    @app.exception_handler(Exception)
    def unexpected_error(request: Request, exc: Exception):
        return error(request, 500, "INTERNAL_ERROR", {})

    return app
