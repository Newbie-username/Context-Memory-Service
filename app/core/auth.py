from typing import Literal

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from pydantic import BaseModel, Field, ValidationError

from app.core.contracts import Identifier, Scope
from app.core.errors import ServiceError, denied

Permission = Literal[
    "sessions:write",
    "context:build",
    "context:audit",
    "memory:propose",
    "memory:read",
    "memory:review",
]


class Identity(BaseModel):
    sub: Identifier
    tenant_id: Identifier
    application_id: Identifier
    permissions: frozenset[Permission] = Field(max_length=6)
    scopes: frozenset[Scope] = Field(max_length=100)

    @property
    def visible_scopes(self) -> frozenset[str]:
        return self.scopes | {f"user:{self.sub}"}

    def require(
        self, permission: Permission, application_id: str | None = None
    ) -> None:
        if permission not in self.permissions or (
            application_id is not None and application_id != self.application_id
        ):
            raise denied()

    def require_task(self, task_id: str | None) -> None:
        if task_id is not None and f"task:{task_id}" not in self.scopes:
            raise denied()


class DelegationVerifier:
    def __init__(self, public_key_pem: bytes, issuer: str, audience: str):
        key = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
            raise ValueError("delegated JWTs require an RSA key of at least 2048 bits")
        self.key = key
        self.issuer = issuer
        self.audience = audience

    def verify(self, token: str) -> Identity:
        try:
            claims = jwt.decode(
                token,
                self.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
            identity = Identity.model_validate(claims)
        except (jwt.InvalidTokenError, ValidationError):
            raise ServiceError(401, "AUTH_REQUIRED") from None

        for scope in identity.scopes:
            kind, resource = scope.split(":", 1)
            if (kind == "tenant" and resource != identity.tenant_id) or (
                kind == "user" and resource != identity.sub
            ):
                raise denied()
        return identity
