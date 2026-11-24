import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture(scope="session")
def signing_key(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_file = tmp_path_factory.mktemp("jwt") / "key.pub"
    public_file.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return key, public_file


@pytest.fixture(scope="session")
def database():
    admin_url = os.getenv("CONTEXT_TEST_ADMIN_DATABASE_URL")
    if not admin_url:
        pytest.skip(
            "set CONTEXT_TEST_ADMIN_DATABASE_URL for PostgreSQL integration tests"
        )

    suffix = uuid4().hex[:12]
    name = f"context_test_{suffix}"
    role = f"context_app_{suffix}"
    password = uuid4().hex
    cluster = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with cluster.connect() as admin:
        admin.execute(text(f"CREATE DATABASE {name}"))
        admin.execute(
            text(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{password}'"
            )
        )

    migration_url = make_url(admin_url).set(database=name)
    application_url = migration_url.set(username=role, password=password)
    engine = create_engine(migration_url)
    try:
        with patch.dict(
            os.environ,
            {
                "CONTEXT_MIGRATION_DATABASE_URL": migration_url.render_as_string(
                    hide_password=False
                ),
            },
        ):
            command.upgrade(Config("alembic.ini"), "head")
        with engine.begin() as conn:
            grant = (
                Path("deploy/grant-runtime.sql")
                .read_text()
                .replace(':"runtime_role"', role)
            )
            for statement in grant.split(";"):
                if statement.strip():
                    conn.execute(text(statement))
            conn.execute(
                text("INSERT INTO tenants (tenant_id) VALUES ('alpha'), ('beta')")
            )
            conn.execute(
                text("""
                INSERT INTO policy_versions (tenant_id, application_id, version, rules)
                SELECT tenant_id, app, 1,
                    '{"sources":["knowledge","history","memory"],"constraints":{"mode":"safe"}}'
                FROM tenants CROSS JOIN unnest(ARRAY['', 'it']) AS app
            """)
            )
        yield application_url.render_as_string(hide_password=False), engine
    finally:
        engine.dispose()
        with cluster.connect() as admin:
            admin.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
            admin.execute(text(f"DROP ROLE {role}"))
        cluster.dispose()


@pytest.fixture
def headers(signing_key):
    key, _ = signing_key

    def make(**changes):
        tenant = changes.get("tenant_id", "alpha")
        return {
            "Authorization": "Bearer "
            + jwt.encode(
                {
                    "iss": "test-issuer",
                    "aud": "context-service",
                    "sub": "alice",
                    "iat": datetime.now(UTC),
                    "exp": datetime.now(UTC) + timedelta(minutes=10),
                    "tenant_id": tenant,
                    "application_id": "it",
                    "scopes": [f"tenant:{tenant}"],
                    "permissions": ["sessions:write", "context:build", "context:audit"],
                    **changes,
                },
                key,
                algorithm="RS256",
            ),
            "Idempotency-Key": "test-" + uuid4().hex,
        }

    return make
