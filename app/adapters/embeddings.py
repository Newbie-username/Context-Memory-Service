import math

import httpx
from pydantic import Field, ValidationError

from app.core.contracts import Contract
from app.core.errors import ServiceError
from app.settings import EmbeddingProfile


class EmbeddingResponse(Contract):
    embedding: list[float] = Field(min_length=1, max_length=2000)


def embed(client: httpx.Client, profile: EmbeddingProfile, content: str) -> list[float]:
    headers = (
        {"Authorization": f"Bearer {profile.token.get_secret_value()}"}
        if profile.token
        else {}
    )
    try:
        response = client.post(
            profile.url,
            headers=headers,
            timeout=profile.timeout_seconds,
            json={"model": profile.model_version, "input": content},
        ).raise_for_status()
        values = EmbeddingResponse.model_validate_json(response.content).embedding
    except (httpx.HTTPError, ValidationError):
        raise ServiceError(503, "EMBEDDING_UNAVAILABLE") from None

    magnitude = math.hypot(*values)
    if (
        not math.isfinite(magnitude)
        or magnitude == 0
        or len(values) != profile.dimensions
    ):
        raise ServiceError(503, "EMBEDDING_UNAVAILABLE")
    return [number / magnitude for number in values]
