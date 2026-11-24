from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints

from app.core.auth import Identity
from app.core.contracts import Contract, Identifier, LongText, Scope

MemoryKind = Literal["user_preference", "resolved_incident"]
MemoryStatus = Literal[
    "candidate",
    "pending_review",
    "verified",
    "rejected",
    "superseded",
    "expired",
    "revoked",
]


class SourceEvent(Contract):
    session_id: UUID
    sequence: int = Field(gt=0, le=2**63 - 1)


class CandidateRequest(Contract):
    application_id: Identifier
    kind: MemoryKind
    scope: Scope
    topic: Identifier
    content: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)
    ]
    model_version: Identifier
    source: SourceEvent
    expires_at: AwareDatetime
    confirmed: bool = False


class VersionRef(Contract):
    memory_id: UUID
    version: int = Field(gt=0, le=2**63 - 1)


class ReviewRequest(Contract):
    expected_version: int = Field(gt=0, le=2**63 - 1)
    decision: Literal["verify", "reject"] = "verify"
    replaces: VersionRef | None = None


class RevokeRequest(Contract):
    expected_version: int = Field(gt=0, le=2**63 - 1)


class MemoryState(VersionRef):
    status: MemoryStatus


class MemoryHit(VersionRef):
    kind: MemoryKind
    scope: Scope
    topic: Identifier
    content: str
    model_version: Identifier
    expires_at: AwareDatetime


class SearchRequest(Contract):
    application_id: Identifier
    query: LongText
    limit: int = Field(default=8, ge=1, le=20)


class SearchResponse(Contract):
    items: list[MemoryHit]
    mode: Literal["keyword", "hybrid"]
    warnings: list[str]


class Version(Contract):
    version: int
    status: MemoryStatus
    changed_at: AwareDatetime
    reason: str


class VersionHistory(Contract):
    memory_id: UUID
    source: SourceEvent
    versions: list[Version]
    review_content: str | None = None


class MemoryProvider(Protocol):
    def search(self, identity: Identity, query: str, limit: int) -> SearchResponse:
        """Return visible, verified memory with versioned references."""
