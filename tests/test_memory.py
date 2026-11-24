import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import dramatiq
import httpx
import pytest
from dramatiq.brokers.redis import RedisBroker
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.adapters import memory_jobs, memory_store, postgres
from app.core.errors import ServiceError
from app.main import create_app
from app.settings import EmbeddingProfile, Settings

pytestmark = pytest.mark.postgres
MEMORY = "/api/v1/memories"
SESSIONS = "/api/v1/sessions"
PERMISSIONS = [
    "sessions:write",
    "context:build",
    "context:audit",
    "memory:read",
    "memory:propose",
]


@pytest.fixture
def memory_service(database, signing_key):
    url, _ = database
    _, public_key = signing_key
    profile = EmbeddingProfile(
        url="http://localhost/embed", model_version="test-v1", dimensions=3
    )
    settings = Settings(
        database_url=url,
        jwt_issuer="test-issuer",
        jwt_audience="context-service",
        jwt_public_key_file=public_key,
        embedding=profile,
        applications={
            "it": {
                "tenants": ["alpha", "beta"],
                "routes": ["general_chat"],
                "memory_enabled": True,
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
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"embedding": [1, 0, 0]})
        )
    )
    app = create_app(settings, rag_client=client)
    with TestClient(app) as api:
        yield api, profile


def _candidate(
    client, headers, *, personal=True, tenant="alpha", content="打印机 E-77"
):
    owner = headers(permissions=PERMISSIONS, tenant_id=tenant)
    session = client.post(SESSIONS, headers=owner, json={"application_id": "it"})
    assert session.status_code == 201, session.text
    session_id = session.json()["session_id"]
    source = client.post(
        f"{SESSIONS}/{session_id}/events",
        headers=owner,
        json={
            "kind": "user_message" if personal else "tool_result",
            "content": content,
        },
    )
    assert source.status_code == 201, source.text
    body = {
        "application_id": "it",
        "kind": "user_preference" if personal else "resolved_incident",
        "scope": "user:alice" if personal else f"tenant:{tenant}",
        "topic": f"topic-{uuid4().hex}",
        "content": content,
        "model_version": "sample-v1",
        "confirmed": personal,
        "source": {"session_id": session_id, "sequence": source.json()["sequence"]},
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    return owner, body


def _post(client, path, headers, payload, expected=200):
    response = client.post(path, headers=headers, json=payload)
    assert response.status_code == expected, response.text
    return response.json()


def _search(client, headers, query):
    return _post(
        client, f"{MEMORY}/search", headers, {"application_id": "it", "query": query}
    )["items"]


def test_preference_cross_session_and_scope(memory_service, headers):
    client, _ = memory_service
    owner, body = _candidate(client, headers)
    created = _post(client, f"{MEMORY}/candidates", owner, body, 201)
    assert created["status"] == "verified"
    assert created["version"] == 3
    other_key = headers(permissions=PERMISSIONS)
    assert _post(client, f"{MEMORY}/candidates", other_key, body, 201) == created
    assert (
        _post(client, f"{MEMORY}/candidates", owner, {**body, "topic": "other"}, 409)[
            "code"
        ]
        == "MEMORY_CANDIDATE_CONFLICT"
    )
    another_owner, another = _candidate(client, headers, content="设置 E-770")
    _post(client, f"{MEMORY}/candidates", another_owner, another, 201)
    assert [hit["memory_id"] for hit in _search(client, owner, "E-77")] == [
        created["memory_id"]
    ]
    assert _search(client, headers(sub="bob", permissions=PERMISSIONS), "E-77") == []
    assert (
        _search(client, headers(tenant_id="beta", permissions=PERMISSIONS), "E-77")
        == []
    )
    session = _post(client, SESSIONS, other_key, {"application_id": "it"}, 201)
    context = _post(
        client,
        "/api/v1/context/build",
        owner,
        {
            "application_id": "it",
            "query": "打印机什么设置",
            "task_type": "general_chat",
            "include_memory": True,
            "session_id": session["session_id"],
        },
    )
    assert "打印机 E-77" in context["messages"][1]["content"]
    manifest = client.get(
        f"/api/v1/context/{context['context_id']}/manifest", headers=owner
    )
    assert manifest.json()["references"][0]["source"] == "memory"


def test_review_replace_and_expire(memory_service, headers, database):
    client, _ = memory_service
    owner, body = _candidate(
        client, headers, personal=False, content="工单 E-102 已验证断电重启"
    )
    pending = _post(client, f"{MEMORY}/candidates", owner, body, 201)
    topic = body["topic"]
    reviewer = headers(sub="reviewer", permissions=["memory:review", "memory:read"])
    path = f"{MEMORY}/{pending['memory_id']}/verify"
    assert _search(client, reviewer, "E-102") == []
    _post(
        client,
        path,
        headers(permissions=["memory:review"]),
        {"expected_version": 2},
        403,
    )
    review_body = {"expected_version": 2}
    with ThreadPoolExecutor(max_workers=3) as executor:
        reviews = list(
            executor.map(
                lambda _: client.post(path, headers=reviewer, json=review_body),
                range(3),
            )
        )
    assert sorted(response.status_code for response in reviews) == [200, 409, 409]
    first = next(response.json() for response in reviews if response.status_code == 200)
    assert len(_search(client, reviewer, "E-102")) == 1

    owner, body = _candidate(
        client, headers, personal=False, content="工单 E-102 更换电源"
    )
    body["topic"] = topic
    pending = _post(client, f"{MEMORY}/candidates", owner, body, 201)
    path = f"{MEMORY}/{pending['memory_id']}/verify"
    assert (
        _post(client, path, reviewer, review_body, 409)["code"]
        == "MEMORY_TOPIC_CONFLICT"
    )
    approved = _post(
        client,
        path,
        reviewer,
        {
            **review_body,
            "replaces": {"memory_id": first["memory_id"], "version": first["version"]},
        },
    )
    assert _search(client, reviewer, "E-102")[0]["memory_id"] == approved["memory_id"]

    _, admin = database
    with admin.begin() as conn:
        conn.execute(
            text("UPDATE memory_entries SET expires_at = now() WHERE memory_id = :id"),
            {"id": approved["memory_id"]},
        )
    assert _search(client, reviewer, "E-102") == []
    with postgres.tenant_transaction(client.app.state.db, "alpha") as conn:
        memory_store.expire_due(conn, "alpha", "it")
    audit = client.get(
        f"{MEMORY}/{approved['memory_id']}/versions", headers=reviewer
    ).json()
    assert [event["status"] for event in audit["versions"]] == [
        "candidate",
        "pending_review",
        "verified",
        "expired",
    ]


def test_reject_wrong_evidence_and_credentials(memory_service, headers):
    client, _ = memory_service
    owner, body = _candidate(client, headers, content="新样例 C-417")
    for content, code in (
        ("Bearer credentials-should-not-be-retained", "SENSITIVE_MEMORY_CONTENT"),
        ("unrelated confirmation", "MEMORY_EVIDENCE_INVALID"),
    ):
        error = _post(
            client, f"{MEMORY}/candidates", owner, {**body, "content": content}, 422
        )
        assert error["code"] == code
    assert _search(client, owner, "C-417") == []

    owner, body = _candidate(client, headers, personal=False)
    record = _post(client, f"{MEMORY}/candidates", owner, body, 201)
    reviewer = headers(sub="reviewer", permissions=["memory:review"])
    result = _post(
        client,
        f"{MEMORY}/{record['memory_id']}/verify",
        reviewer,
        {
            "expected_version": 2,
            "decision": "reject",
        },
    )
    assert result["status"] == "rejected"
    audit = client.get(
        f"{MEMORY}/{record['memory_id']}/versions", headers=reviewer
    ).json()
    assert audit["review_content"] is None
    assert audit["versions"][-1]["reason"] == "review_rejected"


def _index_job(admin, memory_id):
    with admin.connect() as conn:
        return conn.execute(
            text("""
            SELECT job_id FROM memory_outbox WHERE memory_id = :id AND action = 'index'
        """),
            {"id": memory_id},
        ).scalar_one()


def test_vector_retry_and_revocation(memory_service, headers, database):
    client, profile = memory_service
    _, admin = database
    owner, body = _candidate(client, headers, content="打印机 E-685")
    record = _post(client, f"{MEMORY}/candidates", owner, body, 201)
    memory_id = record["memory_id"]
    db = client.app.state.db
    job = _index_job(admin, memory_id)
    calls = []
    enabled = frozenset({"it"})

    def endpoint(request):
        calls.append(request)
        return (
            httpx.Response(503)
            if len(calls) == 1
            else httpx.Response(200, json={"embedding": [1, 0, 0]})
        )

    with httpx.Client(transport=httpx.MockTransport(endpoint)) as backend:
        with pytest.raises(ServiceError, match="EMBEDDING_UNAVAILABLE"):
            memory_jobs.process(db, backend, "alpha", job, profile, enabled)
        with admin.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT completed_at FROM memory_outbox WHERE job_id = :id"),
                    {"id": job},
                ).scalar_one()
                is None
            )
        memory_jobs.process(db, backend, "alpha", job, profile, enabled)
        memory_jobs.process(db, backend, "alpha", job, profile, enabled)
        assert len(calls) == 2
        hits = _search(client, owner, "unrelated semantic terms")
        assert [item["memory_id"] for item in hits] == [memory_id]
        _post(client, f"{MEMORY}/{memory_id}/revoke", owner, {"expected_version": 3})
        assert _search(client, owner, "E-685") == []
        with admin.begin() as conn:
            conn.execute(
                text("UPDATE memory_outbox SET completed_at = NULL WHERE job_id = :id"),
                {"id": job},
            )
            remove_job = conn.execute(
                text("""
                SELECT job_id FROM memory_outbox WHERE memory_id = :id AND action = 'remove'
            """),
                {"id": memory_id},
            ).scalar_one()
        memory_jobs.process(db, backend, "alpha", job, profile, enabled)
        memory_jobs.process(db, backend, "alpha", remove_job, profile, enabled)
        assert len(calls) == 2
    with admin.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM memory_embeddings WHERE memory_id = :id"),
                {"id": memory_id},
            ).scalar_one()
            == 0
        )


def test_redis_delivery(memory_service, headers, database):
    url = os.getenv("CONTEXT_TEST_REDIS_URL")
    if not url:
        pytest.skip("set CONTEXT_TEST_REDIS_URL for Redis delivery tests")
    client, profile = memory_service
    _, admin = database
    owner, body = _candidate(client, headers, tenant="beta", content="偏好使用 E-998")
    record = _post(client, f"{MEMORY}/candidates", owner, body, 201)
    db = client.app.state.db
    broker = RedisBroker(url=url, namespace=f"context-test-{uuid4().hex}")

    @dramatiq.actor(broker=broker, queue_name="test_memory", max_retries=0)
    def deliver(tenant, job_id):
        memory_jobs.process(
            db, client.app.state.rag, tenant, UUID(job_id), profile, frozenset({"it"})
        )

    worker = dramatiq.Worker(broker, worker_threads=1, worker_timeout=100)
    worker.start()
    try:
        with postgres.tenant_transaction(db, "beta") as conn:
            assert memory_jobs.dispatch(conn, "beta", True, deliver.send) == 1
        broker.join("test_memory", timeout=8000)
        with admin.connect() as conn:
            assert conn.execute(
                text("""
                SELECT model_version, dimensions FROM memory_embeddings WHERE memory_id = :id
            """),
                {"id": record["memory_id"]},
            ).one() == ("test-v1", 3)
    finally:
        worker.stop()
        broker.flush_all()


def test_context_rechecks_memory_before_commit(memory_service, headers, monkeypatch):
    client, _ = memory_service
    owner, body = _candidate(client, headers, content="重试 E-949")
    record = _post(client, f"{MEMORY}/candidates", owner, body, 201)
    provider = client.app.state.memory
    original = provider.search

    def search_then_revoke(identity, query, limit):
        result = original(identity, query, limit)
        with postgres.tenant_transaction(client.app.state.db, "alpha") as conn:
            memory_store.revoke(
                conn, identity, UUID(record["memory_id"]), record["version"], uuid4()
            )
        return result

    monkeypatch.setattr(provider, "search", search_then_revoke)
    error = _post(
        client,
        "/api/v1/context/build",
        owner,
        {
            "application_id": "it",
            "query": "E-949",
            "task_type": "general_chat",
            "include_memory": True,
        },
        409,
    )
    assert error["code"] == "MEMORY_VERSION_CONFLICT"
