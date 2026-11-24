import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from app.settings import Settings

pytestmark = pytest.mark.postgres
BASE = "/api/v1"
PERMISSIONS = ["sessions:write", "context:build", "context:audit", "tasks:write"]


@pytest.fixture
def task_service(database, signing_key):
    url, _ = database
    _, public_key = signing_key
    mode = {"kind": "success"}
    waiting, resume = Event(), Event()

    def summarize(request: httpx.Request):
        inputs = json.loads(request.content)["notes"]
        if mode["kind"] == "timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        if mode["kind"] == "invalid":
            return httpx.Response(
                200,
                json={
                    "notes": [
                        {
                            "sequence": inputs[-1]["sequence"],
                            "content": "invented result",
                        },
                    ]
                },
            )
        if mode["kind"] == "wait":
            waiting.set()
            assert resume.wait(timeout=5)
        note = inputs[-1]
        return httpx.Response(
            200,
            json={
                "notes": [
                    {"sequence": note["sequence"], "content": note["content"][:48]},
                ]
            },
        )

    settings = Settings(
        database_url=url,
        jwt_issuer="test-issuer",
        jwt_audience="context-service",
        jwt_public_key_file=public_key,
        summary_url="http://localhost/summary",
        applications={
            "it": {
                "tenants": ["alpha", "beta"],
                "routes": ["task_resume", "general_chat"],
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
    rag = httpx.Client(transport=httpx.MockTransport(summarize))
    with TestClient(create_app(settings, rag_client=rag)) as client:
        yield client, mode, waiting, resume


def _post(client, path, headers, body, status=200):
    response = client.post(f"{BASE}{path}", headers=headers, json=body)
    assert response.status_code == status, response.text
    return response.json()


def _auth(headers, task, **claims):
    return headers(scopes=[f"task:{task}"], permissions=PERMISSIONS, **claims)


def _session(client, headers, task):
    return _post(
        client,
        "/sessions",
        _auth(headers, task),
        {
            "application_id": "it",
            "task_id": task,
        },
        201,
    )["session_id"]


def _event(client, headers, task, session, kind, content, **fields):
    return _post(
        client,
        f"/sessions/{session}/events",
        _auth(headers, task),
        {
            "kind": kind,
            "content": content,
            **fields,
        },
        201,
    )


def _restore(client, headers, task, session, version):
    return _post(
        client,
        f"/sessions/{session}/restore",
        _auth(headers, task),
        {
            "expected_version": version,
        },
    )


def _projection(response):
    return json.loads(response["messages"][1]["content"])["task_context"]


def test_restore_replays_across_sessions_and_protects_critical_data(
    task_service, headers, database
):
    client, *_ = task_service
    task = f"task-{uuid4().hex}"
    first, second = (_session(client, headers, task) for _ in range(2))
    _event(
        client,
        headers,
        task,
        first,
        "user_message",
        "Preserve the original instruction",
    )
    update = {
        "expected_version": 1,
        "status": "waiting_external",
        "goal": "Verify printer",
        "confirmed_facts": {"printer": "unit-77"},
        "assumptions": ["Unplugged"],
        "constraints": ["Do not restart automatically"],
        "pending_steps": ["Check power"],
    }
    _event(
        client,
        headers,
        task,
        first,
        "task_update",
        "State recorded",
        task_update=update,
        evidence_refs=["ticket-77"],
    )
    _event(
        client,
        headers,
        task,
        first,
        "tool_result",
        "UNKNOWN result must survive",
        operation={"expected_version": 2, "operation_id": "op-1", "outcome": "unknown"},
        evidence_refs=["trace-1"],
    )
    _event(
        client,
        headers,
        task,
        first,
        "tool_result",
        "RETRIEVABLE raw log",
        operation={
            "expected_version": 3,
            "operation_id": "scan",
            "outcome": "succeeded",
            "receipt": "scan-receipt",
            "reproducible": True,
        },
        evidence_refs=["scan-ref"],
    )
    _event(
        client,
        headers,
        task,
        second,
        "tool_result",
        "SECURITY evidence and error",
        operation={
            "expected_version": 4,
            "operation_id": "op-2",
            "outcome": "failed",
            "receipt": "failure-receipt",
            "error_code": "E-88",
            "security_evidence": True,
        },
        evidence_refs=["alarm-ref"],
    )
    _event(client, headers, task, second, "assistant_message", "Proposal only " * 20)
    compact = f"/sessions/{second}/compact"
    snapshot = _post(client, compact, _auth(headers, task), {"expected_version": 6})
    assert (
        _post(client, compact, _auth(headers, task), {"expected_version": 6})
        == snapshot
    )

    event = _event(
        client,
        headers,
        task,
        second,
        "task_update",
        "Next step refined",
        task_update={"expected_version": 6, "pending_steps": ["Call vendor"]},
    )
    assert event == {"sequence": 3, "task_version": 7}
    restored = _restore(client, headers, task, second, 7)
    meta = restored["restoration"]
    assert meta["snapshot"] == snapshot
    assert meta["replayed_events"] == 1
    assert meta["task"]["status"] == "waiting_external"
    assert meta["unconfirmed_operations"] == ["op-1"]
    assert restored["warnings"] == ["TASK_RECONCILIATION_REQUIRED"]
    state = _projection(restored)
    assert state["confirmed_facts"] == {"printer": "unit-77"}
    assert state["fact_evidence_refs"] == ["ticket-77"]
    assert state["assumptions"] == ["Unplugged"]
    assert state["constraints"] == ["Do not restart automatically"]
    assert state["pending_steps"] == ["Call vendor"]
    assert [note["content"] for note in state["retained_tool_results"]] == [
        "UNKNOWN result must survive",
        "SECURITY evidence and error",
    ]
    assert state["operations"]["op-2"]["error_code"] == "E-88"
    assert state["operations"]["scan"]["evidence_refs"] == ["scan-ref"]
    assert len(state["assistant_notes"][0]["content"]) == 48
    _, admin = database
    with admin.connect() as conn:
        assert (
            conn.execute(
                text("""
            SELECT payload ->> 'content' FROM session_events
            WHERE session_id = :id AND sequence = 4
        """),
                {"id": first},
            ).scalar_one()
            == "RETRIEVABLE raw log"
        )

    manifest = client.get(
        f"{BASE}/context/{restored['context_id']}/manifest",
        headers=_auth(headers, task),
    )
    assert manifest.json()["restoration"]["snapshot"] == snapshot
    _post(
        client, f"/sessions/{second}/restore", _auth(headers, task, sub="bob"), {}, 403
    )
    _post(
        client, f"/sessions/{second}/restore", headers(permissions=PERMISSIONS), {}, 403
    )
    _event(
        client,
        headers,
        task,
        first,
        "tool_result",
        "Result confirmed",
        operation={
            "expected_version": 7,
            "operation_id": "op-1",
            "outcome": "succeeded",
            "receipt": "confirmed-receipt",
        },
    )
    assert not _restore(client, headers, task, second, 8)["restoration"][
        "reconciliation_required"
    ]
    error = _post(
        client,
        f"/sessions/{first}/events",
        _auth(headers, task),
        {
            "kind": "tool_result",
            "content": "Another receipt",
            "operation": {
                "expected_version": 8,
                "operation_id": "op-1",
                "outcome": "succeeded",
                "receipt": "contradicts-previous",
            },
        },
        409,
    )
    assert error["code"] == "TASK_OPERATION_CONFLICT"


def test_invalid_or_late_snapshot_preserves_last_valid_cursor(
    task_service, headers, database
):
    client, mode, waiting, resume = task_service
    task = f"task-{uuid4().hex}"
    session = _session(client, headers, task)
    _event(
        client, headers, task, session, "assistant_message", "Original assistant note"
    )
    path = f"/sessions/{session}/compact"
    original = _post(client, path, _auth(headers, task), {"expected_version": 1})
    _event(client, headers, task, session, "assistant_message", "A second note")
    for outcome, status, code in (
        ("invalid", 422, "SNAPSHOT_VALIDATION_FAILED"),
        ("timeout", 504, "DEPENDENCY_TIMEOUT"),
    ):
        mode["kind"] = outcome
        assert (
            _post(client, path, _auth(headers, task), {"expected_version": 2}, status)[
                "code"
            ]
            == code
        )
    restore = _restore(client, headers, task, session, 2)
    assert restore["restoration"]["snapshot"] == original
    assert restore["restoration"]["replayed_events"] == 1
    mode["kind"] = "wait"
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            client.post,
            f"{BASE}{path}",
            headers=_auth(headers, task),
            json={"expected_version": 2},
        )
        try:
            assert waiting.wait(timeout=3)
            _event(
                client, headers, task, session, "user_message", "Concurrent instruction"
            )
        finally:
            resume.set()
        response = pending.result(timeout=5)
    assert response.status_code == 409, response.text
    restore = _restore(client, headers, task, session, 3)
    assert restore["restoration"]["snapshot"] == original
    assert restore["restoration"]["replayed_events"] == 2

    _, admin = database
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE task_snapshots SET digest = 'damaged' WHERE snapshot_id = :id"
            ),
            {"id": original["snapshot_id"]},
        )
    rebuild = _restore(client, headers, task, session, 3)
    assert rebuild["restoration"].get("snapshot") is None
    assert rebuild["restoration"]["replayed_events"] == 3


def test_restore_automatically_compacts_and_rejects_unrepresentable_critical_data(
    task_service, headers
):
    client, *_ = task_service
    task = f"task-{uuid4().hex}"
    session = _session(client, headers, task)
    for index in range(10):
        _event(
            client,
            headers,
            task,
            session,
            "assistant_message",
            f"Message-{index} " + "a" * 900,
        )
    restored = _restore(client, headers, task, session, 10)
    assert restored["restoration"]["snapshot"]["through_sequence"] == 10
    assert _projection(restored)["assistant_notes"][0]["sequence"] == 10
    _event(
        client,
        headers,
        task,
        session,
        "tool_result",
        "x" * 8000,
        operation={
            "expected_version": 10,
            "operation_id": "irretrievable",
            "outcome": "unknown",
        },
    )
    error = _post(client, f"/sessions/{session}/restore", _auth(headers, task), {}, 422)
    assert error["code"] == "CONTEXT_BUDGET_EXCEEDED"


def test_task_updates_use_one_version_across_sessions(task_service, headers):
    client, *_ = task_service
    task = f"task-{uuid4().hex}"
    sessions = [_session(client, headers, task) for _ in range(2)]
    payload = {
        "kind": "task_update",
        "content": "Record status",
        "task_update": {
            "expected_version": 0,
            "status": "active",
        },
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda session: client.post(
                    f"{BASE}/sessions/{session}/events",
                    headers=_auth(headers, task),
                    json=payload,
                ),
                sessions,
            )
        )
    assert sorted(response.status_code for response in responses) == [201, 409]
    first, second = [
        client.get(
            f"{BASE}/sessions/{session}/task", headers=_auth(headers, task)
        ).json()
        for session in sessions
    ]
    assert first == second
    assert first["version"] == 1


def test_upgrade_reuses_legacy_events_and_idempotency(
    task_service, headers, legacy_session
):
    client, *_ = task_service
    auth = {**_auth(headers, "legacy-task"), "Idempotency-Key": "legacy-event"}
    event = _post(
        client,
        f"/sessions/{legacy_session}/events",
        auth,
        {
            "kind": "tool_result",
            "content": "Legacy receipt unavailable",
        },
        201,
    )
    assert event == {"sequence": 1, "task_version": 1}
    restored = _restore(client, headers, "legacy-task", legacy_session, 1)
    assert restored["restoration"]["unclassified_tool_results"] == 1
    assert restored["restoration"]["reconciliation_required"]
    assert (
        _projection(restored)["retained_tool_results"][0]["content"]
        == "Legacy receipt unavailable"
    )


def test_automatic_compaction_failure_replays_original_when_it_fits(
    task_service, headers
):
    client, mode, *_ = task_service
    task = f"task-{uuid4().hex}"
    session = _session(client, headers, task)
    for index in range(6):
        _event(
            client,
            headers,
            task,
            session,
            "assistant_message",
            f"Message-{index} " + "a" * 900,
        )
    mode["kind"] = "invalid"
    restored = _restore(client, headers, task, session, 6)
    assert restored["status"] == "degraded"
    assert restored["warnings"] == ["SNAPSHOT_VALIDATION_FAILED"]
    assert restored["restoration"].get("snapshot") is None
    assert len(_projection(restored)["assistant_notes"]) == 6
