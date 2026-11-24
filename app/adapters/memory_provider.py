import httpx
from sqlalchemy import Connection, Engine

from app.adapters import memory_store, postgres
from app.adapters.embeddings import embed
from app.core.auth import Identity
from app.core.errors import ServiceError, denied
from app.core.memory import SearchResponse
from app.core.policy import effective_policy
from app.settings import EmbeddingProfile


def require_memory_policy(conn: Connection, identity: Identity) -> None:
    postgres.require_tenant(conn, identity)
    policy = effective_policy(
        conn, identity.tenant_id, identity.application_id, identity.visible_scopes
    )
    if "memory" not in policy.sources:
        raise denied()


class PostgresMemory:
    def __init__(
        self, database: Engine, client: httpx.Client, embedding: EmbeddingProfile | None
    ):
        self.database = database
        self.client = client
        self.embedding = embedding

    def search(self, identity: Identity, query: str, limit: int) -> SearchResponse:
        identity.require("memory:read")
        with postgres.tenant_transaction(self.database, identity.tenant_id) as conn:
            require_memory_policy(conn, identity)
        embedding = None
        warnings = []
        if self.embedding is not None:
            try:
                embedding = embed(self.client, self.embedding, query)
            except ServiceError as exc:
                warnings.append(exc.code)

        with postgres.tenant_transaction(self.database, identity.tenant_id) as conn:
            require_memory_policy(conn, identity)
            model = self.embedding.model_version if self.embedding else None
            items = memory_store.search(conn, identity, query, limit, embedding, model)
            memory_store.require_current(conn, identity, items)
        return SearchResponse(
            items=items, mode="hybrid" if embedding else "keyword", warnings=warnings
        )
