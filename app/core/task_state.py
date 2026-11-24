from typing import Literal

from pydantic import Field

from app.core.contracts import (
    Contract,
    Cursor,
    EventRequest,
    Identifier,
    LongText,
    TaskStatus,
    ToolResult,
)
from app.core.errors import ServiceError

_TRANSITIONS = {
    "created": {"active", "waiting_external", "cancelled"},
    "active": {"waiting_external", "completed", "failed", "cancelled"},
    "waiting_external": {"resumed", "completed", "failed", "cancelled"},
    "resumed": {"active", "waiting_external", "completed", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


def next_status(current: TaskStatus, event: EventRequest) -> TaskStatus:
    update = event.task_update
    if update is None or update.status is None:
        return "active" if current == "created" else current
    if update.status == current and current != "created":
        return current
    if update.status not in _TRANSITIONS[current]:
        raise ServiceError(409, "TASK_STATUS_CONFLICT")
    return update.status


class EventNote(Contract):
    sequence: Cursor
    content: LongText


class Operation(Contract):
    sequence: Cursor
    outcome: Literal["unknown", "succeeded", "failed"]
    receipt: Identifier | None
    error_code: Identifier | None
    evidence_refs: list[Identifier]


class TaskProjection(Contract):
    through: Cursor = 0
    status: TaskStatus = "created"
    goal: str | None = None
    confirmed_facts: dict[str, str] = Field(default_factory=dict)
    fact_evidence_refs: list[Identifier] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    pending_steps: list[str] = Field(default_factory=list)
    operations: dict[Identifier, Operation] = Field(default_factory=dict)
    evidence_refs: list[Identifier] = Field(default_factory=list)
    user_messages: list[EventNote] = Field(default_factory=list)
    assistant_notes: list[EventNote] = Field(default_factory=list)
    retained_tool_results: list[EventNote] = Field(default_factory=list)
    unclassified_tool_results: int = Field(default=0, ge=0)


def clearable_result(operation: ToolResult, evidence_refs: list[str]) -> bool:
    return (
        operation.outcome == "succeeded"
        and operation.reproducible
        and bool(evidence_refs)
        and not operation.security_evidence
        and operation.error_code is None
    )


def replay(base: TaskProjection, events) -> TaskProjection:
    state = base.model_copy(deep=True)
    evidence = dict.fromkeys(state.evidence_refs)
    for row in events:
        if row.sequence != state.through + 1:
            raise ServiceError(503, "TASK_STREAM_INCOMPLETE")
        event = EventRequest.model_validate(row.payload)
        state.through = row.sequence
        state.status = next_status(state.status, event)
        evidence.update(dict.fromkeys(event.evidence_refs))
        note = EventNote(sequence=row.sequence, content=event.content)
        if event.task_update is not None:
            update = event.task_update.model_dump(
                exclude_none=True, exclude={"status", "expected_version"}
            )
            for name, value in update.items():
                setattr(state, name, value)
            if event.task_update.confirmed_facts is not None:
                state.fact_evidence_refs = event.evidence_refs
        elif event.kind == "assistant_message":
            state.assistant_notes.append(note)
        elif event.kind == "user_message":
            state.user_messages.append(note)
        elif event.operation is None:
            state.unclassified_tool_results += 1
            state.retained_tool_results.append(note)
        else:
            operation = event.operation
            previous = state.operations.get(operation.operation_id)
            refs = list(
                dict.fromkeys(
                    [
                        *(previous.evidence_refs if previous else []),
                        *event.evidence_refs,
                    ]
                )
            )
            state.operations[operation.operation_id] = Operation(
                sequence=row.sequence,
                outcome=operation.outcome,
                receipt=operation.receipt,
                error_code=operation.error_code,
                evidence_refs=refs,
            )
            if not clearable_result(operation, event.evidence_refs):
                state.retained_tool_results.append(note)
    state.evidence_refs = list(evidence)
    return state
