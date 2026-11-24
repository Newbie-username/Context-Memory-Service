from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_.@-]{1,128}$", strip_whitespace=False)
]
Scope = Annotated[
    str,
    StringConstraints(
        pattern=r"^(tenant|user|team|project|device|task):[A-Za-z0-9_.@-]{1,128}$"
    ),
]
TaskType = Literal["knowledge_qa", "general_chat"]
Source = Literal["knowledge", "history", "memory"]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=32_768)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionRequest(Contract):
    application_id: Identifier
    task_id: Identifier | None = None


class SessionResponse(Contract):
    session_id: UUID


class EventRequest(Contract):
    kind: Literal["user_message", "assistant_message", "tool_result"]
    content: LongText
    evidence_refs: list[Identifier] = Field(default_factory=list, max_length=32)


class EventResponse(Contract):
    sequence: int


class BuildRequest(Contract):
    application_id: Identifier
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32_768)
    ]
    task_type: TaskType = "knowledge_qa"
    session_id: UUID | None = None
    include_knowledge: bool | None = None
    include_history: bool = True
    include_memory: bool = False
    output_tokens: int | None = Field(default=None, gt=0)


class Evidence(Contract):
    tenant_id: Identifier
    scope: Scope
    source_id: Identifier
    version: int = Field(gt=0, le=2**63 - 1)
    content: LongText
    expires_at: AwareDatetime | None = None
    required: bool = False
    facts: dict[Identifier, Annotated[str, StringConstraints(max_length=512)]] = Field(
        default_factory=dict, max_length=32
    )


class EvidenceResponse(Contract):
    items: list[Evidence] = Field(max_length=100)


class Message(Contract):
    role: Literal["system", "user"]
    content: str


class ItemRef(Contract):
    source: Source
    source_id: str
    version: int
    scope: Scope


class Decision(Contract):
    item: ItemRef
    reason: str


class Conflict(Contract):
    fact: Identifier
    items: list[ItemRef]


class TokenUsage(Contract):
    counter: str
    window: int
    fixed: int
    dynamic_budget: int
    dynamic_used: int
    input: int
    output_reserved: int
    safety_buffer: int


class Manifest(Contract):
    context_id: UUID
    application_id: Identifier
    session_id: UUID | None
    task_type: TaskType
    policy_versions: dict[str, int]
    model_id: Identifier
    builder_version: str
    references: list[ItemRef]
    excluded: list[Decision]
    unauthorized_count: int = 0
    conflicts: list[Conflict]
    warnings: list[str]
    usage: TokenUsage


class BuildResponse(Contract):
    context_id: UUID
    status: Literal["ok", "degraded"]
    messages: list[Message]
    tools: list[dict]
    warnings: list[str]
    usage: TokenUsage


class ErrorResponse(Contract):
    code: str
    request_id: str
    details: dict
