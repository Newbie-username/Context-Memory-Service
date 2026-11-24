from typing import Annotated

import httpx
from pydantic import Field, StringConstraints, ValidationError

from app.core.contracts import Contract, Cursor
from app.core.errors import ServiceError
from app.core.task_state import EventNote, TaskProjection
from app.settings import Settings


class Excerpt(Contract):
    sequence: Cursor
    content: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class Summary(Contract):
    notes: list[Excerpt] = Field(max_length=8)


def compact(
    state: TaskProjection, client: httpx.Client, settings: Settings
) -> TaskProjection:
    sources = state.assistant_notes[-32:]
    if settings.summary_url is None or not sources:
        notes = [
            EventNote(sequence=note.sequence, content=note.content[:1024])
            for note in sources[-8:]
        ]
    else:
        headers = {}
        if settings.summary_token:
            headers["Authorization"] = (
                f"Bearer {settings.summary_token.get_secret_value()}"
            )
        try:
            response = client.post(
                settings.summary_url,
                headers=headers,
                timeout=settings.summary_timeout_seconds,
                json={"notes": [note.model_dump() for note in sources]},
            ).raise_for_status()
        except httpx.TimeoutException:
            raise ServiceError(
                504, "DEPENDENCY_TIMEOUT", source="task_summary"
            ) from None
        except httpx.HTTPError:
            raise ServiceError(
                424, "EVIDENCE_UNAVAILABLE", source="task_summary"
            ) from None
        try:
            summary = Summary.model_validate_json(response.content)
        except ValidationError:
            raise ServiceError(422, "SNAPSHOT_VALIDATION_FAILED") from None
        originals = {note.sequence: note.content for note in sources}
        sequences = [note.sequence for note in summary.notes]
        if sequences != sorted(set(sequences)) or any(
            not note.content.strip()
            or note.content not in originals.get(note.sequence, "")
            for note in summary.notes
        ):
            raise ServiceError(422, "SNAPSHOT_VALIDATION_FAILED")
        notes = [
            EventNote(sequence=note.sequence, content=note.content)
            for note in summary.notes
        ]
    return state.model_copy(update={"assistant_notes": notes})
