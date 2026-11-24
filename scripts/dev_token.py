import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def main() -> None:
    directory = Path(".local")
    directory.mkdir(mode=0o700, exist_ok=True)
    private_key = directory / "delegation.pem"
    if private_key.exists():
        key = serialization.load_pem_private_key(
            private_key.read_bytes(), password=None
        )
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        descriptor = os.open(
            private_key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode=0o600
        )
        with os.fdopen(descriptor, "wb") as file:
            file.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
    (directory / "delegation.pub.pem").write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    now = datetime.now(UTC)
    print(
        jwt.encode(
            {
                "iss": "context-local",
                "aud": "context-memory-service",
                "sub": "demo-user",
                "iat": now,
                "exp": now + timedelta(minutes=20),
                "tenant_id": "demo",
                "application_id": "it-helpdesk",
                "permissions": ["sessions:write", "context:build", "context:audit"],
                "scopes": ["tenant:demo"],
            },
            key,
            algorithm="RS256",
        )
    )


if __name__ == "__main__":
    main()
