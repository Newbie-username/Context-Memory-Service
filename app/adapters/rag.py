import httpx
from pydantic import ValidationError

from app.core.auth import Identity
from app.core.contracts import Evidence, EvidenceResponse
from app.core.errors import ServiceError, denied


def search(
    client: httpx.Client, url: str, token: str | None, identity: Identity, query: str
) -> list[Evidence]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = client.post(
            url,
            headers=headers,
            json={
                "tenant_id": identity.tenant_id,
                "application_id": identity.application_id,
                "subject": identity.sub,
                "scopes": sorted(identity.visible_scopes),
                "query": query,
            },
        )
        if response.status_code == 403:
            raise denied()
        response.raise_for_status()
        return EvidenceResponse.model_validate_json(response.content).items
    except httpx.TimeoutException:
        raise ServiceError(504, "DEPENDENCY_TIMEOUT", source="knowledge") from None
    except (httpx.HTTPError, ValidationError):
        raise ServiceError(424, "EVIDENCE_UNAVAILABLE", source="knowledge") from None
