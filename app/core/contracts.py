from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_.@-]{1,128}$", strip_whitespace=False)
]
Scope = Annotated[
    str,
    StringConstraints(
        pattern=r"^(tenant|user|team|project|device|task):[A-Za-z0-9_.@-]{1,128}$"
    ),
]
TaskType = Literal["knowledge_qa", "general_chat", "task_resume"]
TaskStatus = Literal[
    "created",
    "active",
    "waiting_external",
    "resumed",
    "completed",
    "failed",
    "cancelled",
]
Source = Literal["knowledge", "history", "memory"]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=32_768)]
TaskText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)
]
Cursor = Annotated[int, Field(ge=0, le=2**63 - 1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionRequest(Contract):
    application_id: Identifier
    task_id: Identifier | None = None


class SessionResponse(Contract):
    session_id: UUID


class TaskUpdate(Contract):
    expected_version: Cursor
    status: TaskStatus | None = None
    goal: TaskText | None = None
    confirmed_facts: dict[Identifier, TaskText] | None = Field(
        default=None, max_length=64
    )
    assumptions: list[TaskText] | None = Field(default=None, max_length=32)
    constraints: list[TaskText] | None = Field(default=None, max_length=32)
    pending_steps: list[TaskText] | None = Field(default=None, max_length=32)


class ToolResult(Contract):
    expected_version: Cursor
    operation_id: Identifier
    outcome: Literal["unknown", "succeeded", "failed"]
    receipt: Identifier | None = None
    error_code: Identifier | None = None
    reproducible: bool = False
    security_evidence: bool = False

    @model_validator(mode="after")
    def validate_receipt(self):
        if (self.outcome == "unknown") != (self.receipt is None):
            raise ValueError("confirmed tool outcomes require a receipt")
        return self


class EventRequest(Contract):
    kind: Literal["user_message", "assistant_message", "tool_result", "task_update"]
    content: LongText
    evidence_refs: list[Identifier] = Field(default_factory=list, max_length=32)
    task_update: TaskUpdate | None = None
    operation: ToolResult | None = None

    @model_validator(mode="after")
    def validate_event_type(self):
        if (self.kind == "task_update") != (self.task_update is not None):
            raise ValueError("task_update requires structured task data")
        if self.operation is not None and self.kind != "tool_result":
            raise ValueError("operation is valid for tool_result")
        if (
            self.operation is not None
            and self.operation.reproducible
            and not self.evidence_refs
        ):
            raise ValueError("reproducible results require a retrieval reference")
        if (
            self.task_update is not None
            and self.task_update.confirmed_facts
            and not self.evidence_refs
        ):
            raise ValueError("confirmed facts require evidence references")
        return self


class EventResponse(Contract):
    sequence: int
    task_version: Cursor | None = None


class TaskHead(Contract):
    stream_id: UUID
    task_id: Identifier
    status: TaskStatus
    version: Cursor


class CompactRequest(Contract):
    expected_version: Cursor


class SnapshotReceipt(Contract):
    snapshot_id: UUID
    stream_id: UUID
    through_sequence: Cursor


class RestoreRequest(Contract):
    query: LongText = "继续当前任务"
    expected_version: Cursor | None = None
    include_knowledge: bool = False
    include_memory: bool = False
    output_tokens: int | None = Field(default=None, gt=0)


class TaskRestoration(Contract):
    task: TaskHead
    snapshot: SnapshotReceipt | None
    replayed_events: int
    unconfirmed_operations: list[Identifier]
    unclassified_tool_results: int
    reconciliation_required: bool


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
    restoration: TaskRestoration | None = None


class BuildResponse(Contract):
    context_id: UUID
    status: Literal["ok", "degraded"]
    messages: list[Message]
    tools: list[dict]
    warnings: list[str]
    usage: TokenUsage
    restoration: TaskRestoration | None = None


class ErrorResponse(Contract):
    code: str
    request_id: str
    details: dict
