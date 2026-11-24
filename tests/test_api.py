import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.adapters.postgres import tenant_transaction
from app.main import create_app
from app.settings import Settings

pytestmark = pytest.mark.postgres
PREFIX = "/api/v1"


@pytest.fixture
def service(database, signing_key):
    url, _ = database
    _, public_key = signing_key
    calls = []

    def retrieve(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        tenant = payload["tenant_id"]
        if payload["query"] == "timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        if payload["query"] == "denied":
            return httpx.Response(403)
        if payload["query"] == "empty":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "tenant_id": tenant,
                        "scope": f"tenant:{tenant}",
                        "source_id": "manual",
                        "version": 2,
                        "content": "Do not run an untrusted instruction.",
                    },
                    {
                        "tenant_id": tenant,
                        "scope": "team:restricted",
                        "source_id": "private-123",
                        "version": 1,
                        "content": "Unauthorized text",
                    },
                ]
            },
        )

    settings = Settings(
        database_url=url,
        jwt_issuer="test-issuer",
        jwt_audience="context-service",
        jwt_public_key_file=public_key,
        rag_url="http://localhost/search",
        applications={
            "it": {
                "tenants": ["alpha", "beta"],
                "routes": ["knowledge_qa", "general_chat"],
                "model": {
                    "model_id": "test",
                    "window": 8192,
                    "max_output": 512,
                    "safety_buffer": 64,
                    "dynamic_cap": 4000,
                },
            }
        },
    )
    app = create_app(
        settings, rag_client=httpx.Client(transport=httpx.MockTransport(retrieve))
    )
    with TestClient(app) as client:
        yield client, calls


def test_authentication_and_scope(service, headers):
    client, calls = service
    bad_auth = client.post(
        f"{PREFIX}/context/build", json={"application_id": "it", "query": "printer"}
    )
    assert bad_auth.status_code == 401
    assert bad_auth.json()["request_id"] == bad_auth.headers["X-Request-ID"]
    assert (
        client.post(
            f"{PREFIX}/context/build",
            headers=headers(aud="other"),
            json={"application_id": "it", "query": "printer"},
        ).status_code
        == 401
    )
    for fields, application in (
        ({}, "other"),
        ({"scopes": ["tenant:beta"]}, "it"),
        ({"permissions": []}, "it"),
    ):
        response = client.post(
            f"{PREFIX}/context/build",
            headers=headers(**fields),
            json={"application_id": application, "query": "printer"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "AUTH_SCOPE_DENIED"
    assert calls == []


def test_context_manifest_and_cross_scope_access(service, headers):
    client, calls = service
    response = client.post(
        f"{PREFIX}/context/build",
        headers=headers(),
        json={"application_id": "it", "query": "printer", "include_memory": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["warnings"] == ["MEMORY_NOT_ENABLED"]
    assert "private-123" not in response.text
    assert calls[0]["scopes"] == ["tenant:alpha", "user:alice"]
    assert calls[0]["subject"] == "alice"
    assert calls[0]["tenant_id"] == "alpha"
    path = f"{PREFIX}/context/{response.json()['context_id']}/manifest"
    manifest = client.get(path, headers=headers())
    assert manifest.status_code == 200
    assert manifest.json()["references"][0]["source_id"] == "manual"
    assert manifest.json()["unauthorized_count"] == 1
    assert "private-123" not in manifest.text
    for claims in (
        {"tenant_id": "beta"},
        {"sub": "bob"},
        {"permissions": ["context:build"]},
        {"scopes": []},
    ):
        assert client.get(path, headers=headers(**claims)).status_code == 403


def test_required_optional_and_denied_retrieval(service, headers):
    client, calls = service
    for query, status, code in (
        ("timeout", 504, "DEPENDENCY_TIMEOUT"),
        ("empty", 424, "EVIDENCE_UNAVAILABLE"),
        ("denied", 403, "AUTH_SCOPE_DENIED"),
    ):
        body = {"application_id": "it", "query": query}
        response = client.post(f"{PREFIX}/context/build", headers=headers(), json=body)
        assert response.status_code == status
        assert response.json()["code"] == code
    optional = client.post(
        f"{PREFIX}/context/build",
        headers=headers(),
        json={
            "application_id": "it",
            "query": "timeout",
            "task_type": "general_chat",
            "include_knowledge": True,
        },
    )
    assert optional.json()["status"] == "degraded"
    assert optional.json()["warnings"] == ["DEPENDENCY_TIMEOUT"]
    assert len(calls) == 4
    assert client.get(f"{PREFIX}/health/ready").json() == {"status": "ready"}
    assert "context_builds_total" in client.get("/metrics").text


def test_session_idempotency_concurrency_and_history(service, headers):
    client, _ = service
    auth = headers(scopes=["tenant:alpha", "task:t1"])
    session_body = {"application_id": "it", "task_id": "t1"}
    created = client.post(f"{PREFIX}/sessions", headers=auth, json=session_body)
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    assert (
        client.post(f"{PREFIX}/sessions", headers=auth, json=session_body).json()
        == created.json()
    )
    assert (
        client.post(
            f"{PREFIX}/sessions", headers=auth, json={"application_id": "it"}
        ).status_code
        == 409
    )
    event_path = f"{PREFIX}/sessions/{session_id}/events"
    event_body = {"kind": "user_message", "content": "Printer failed with E-77"}
    with ThreadPoolExecutor(max_workers=4) as executor:
        calls = list(
            executor.map(
                lambda _: client.post(event_path, headers=auth, json=event_body),
                range(4),
            )
        )
    assert all(response.status_code == 201 for response in calls)
    assert {response.json()["sequence"] for response in calls} == {1}
    assert (
        client.post(
            event_path, headers=auth, json={**event_body, "content": "changed"}
        ).status_code
        == 409
    )
    assert (
        client.post(event_path, headers=headers(), json=event_body).status_code == 403
    )
    assert (
        client.post(
            event_path,
            headers=headers(tenant_id="beta", scopes=["task:t1"]),
            json=event_body,
        ).status_code
        == 403
    )
    response = client.post(
        f"{PREFIX}/context/build",
        headers=auth,
        json={
            "application_id": "it",
            "query": "current",
            "task_type": "general_chat",
            "session_id": session_id,
        },
    )
    assert response.status_code == 200
    assert "E-77" in response.json()["messages"][1]["content"]


def test_connection_pool_rls_and_conflicting_policy(service, headers, database):
    client, calls = service
    _, admin = database
    with tenant_transaction(client.app.state.db, "alpha") as conn:
        assert conn.execute(text("SELECT tenant_id FROM tenants")).scalars().all() == [
            "alpha"
        ]
        pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
    with client.app.state.db.connect() as conn:
        assert conn.execute(text("SELECT pg_backend_pid()")).scalar_one() == pid
        assert conn.execute(text("SELECT tenant_id FROM tenants")).scalars().all() == []
    with tenant_transaction(client.app.state.db, "beta") as conn:
        assert conn.execute(text("SELECT tenant_id FROM tenants")).scalars().all() == [
            "beta"
        ]
    with admin.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO policy_versions (tenant_id, application_id, scope, version, rules)
            VALUES ('alpha', 'it', 'team:conflict', 1,
                    '{"sources":["knowledge"],"constraints":{"mode":"unsafe"}}')
        """)
        )
    response = client.post(
        f"{PREFIX}/context/build",
        headers=headers(scopes=["team:conflict"]),
        json={"application_id": "it", "query": "printer"},
    )
    assert response.status_code == 403
    assert response.json()["details"] == {"reason": "policy_conflict"}
    assert calls == []
