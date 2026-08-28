import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finchvox.access_control import AccessControlPolicy
from finchvox.ui_routes import register_ui_routes


ADMIN_HEADERS = {"X-Finchvox-Role": "admin"}
ASTL_HEADERS = {
    "X-Finchvox-Role": "tenant",
    "X-Finchvox-Tenant": "astl.dev.family",
}
OTHER_HEADERS = {
    "X-Finchvox-Role": "tenant",
    "X-Finchvox-Tenant": "other.example",
}


def _attribute(key: str, value: str) -> dict:
    return {"key": key, "value": {"string_value": value}}


def _create_session(
    data_dir: Path,
    session_id: str,
    *,
    tenant_id: str | None = None,
    source: str | None = None,
) -> None:
    session_dir = data_dir / "sessions" / session_id
    session_dir.mkdir(parents=True)
    attributes = []
    if tenant_id:
        attributes.append(_attribute("finchvox.tenant.id", tenant_id))
    if source:
        attributes.append(_attribute("finchvox.session.source", source))

    span = {
        "name": "test-span",
        "start_time_unix_nano": 1_000_000_000,
        "end_time_unix_nano": 2_000_000_000,
        "attributes": attributes,
    }
    (session_dir / f"trace_{session_id}.jsonl").write_text(json.dumps(span) + "\n")


@pytest.fixture
def access_client(temp_data_dir):
    _create_session(temp_data_dir, "astl-session", tenant_id="astl.dev.family")
    _create_session(temp_data_dir, "owner-session", tenant_id="leasing.yytech.by")
    _create_session(temp_data_dir, "unknown-session")

    app = FastAPI()
    register_ui_routes(
        app,
        temp_data_dir,
        access_control=AccessControlPolicy(enabled=True),
    )
    return TestClient(app)


def test_missing_or_invalid_access_scope_is_rejected(access_client):
    assert access_client.get("/api/sessions").status_code == 403
    assert (
        access_client.get(
            "/api/sessions",
            headers={
                "X-Finchvox-Role": "tenant",
                "X-Finchvox-Tenant": "invalid tenant",
            },
        ).status_code
        == 403
    )


def test_admin_sees_all_sessions(access_client):
    response = access_client.get("/api/sessions", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert {session["session_id"] for session in response.json()["sessions"]} == {
        "astl-session",
        "owner-session",
        "unknown-session",
    }


def test_tenant_list_contains_only_matching_sessions(access_client):
    response = access_client.get("/api/sessions", headers=ASTL_HEADERS)

    assert response.status_code == 200
    assert response.json()["total_count"] == 1
    assert [session["session_id"] for session in response.json()["sessions"]] == [
        "astl-session"
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/sessions/owner-session",
        "/api/sessions/owner-session/trace",
        "/api/sessions/owner-session/raw",
        "/api/sessions/owner-session/logs",
        "/api/sessions/owner-session/conversation",
        "/api/sessions/owner-session/exceptions",
        "/api/sessions/owner-session/audio",
        "/api/sessions/owner-session/audio/status",
        "/api/sessions/owner-session/metrics",
        "/api/sessions/owner-session/download",
        "/api/sessions/owner-session/environment",
    ],
)
def test_tenant_cannot_open_another_tenants_session(access_client, path):
    response = access_client.get(path, headers=ASTL_HEADERS)

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"


def test_tenant_can_open_own_session(access_client):
    response = access_client.get(
        "/api/sessions/astl-session/trace", headers=ASTL_HEADERS
    )

    assert response.status_code == 200
    assert len(response.json()["spans"]) == 1


def test_tenant_cannot_upload_sessions(access_client):
    response = access_client.post(
        "/api/sessions/upload",
        headers=ASTL_HEADERS,
        files={"file": ("session.zip", b"not-a-zip", "application/zip")},
    )

    assert response.status_code == 403


def test_legacy_source_mapping_assigns_tenant(temp_data_dir):
    _create_session(temp_data_dir, "legacy-1c", source="1c")
    app = FastAPI()
    register_ui_routes(
        app,
        temp_data_dir,
        access_control=AccessControlPolicy(
            enabled=True,
            legacy_tenant_source_map={"1c": "astl.dev.family"},
        ),
    )
    client = TestClient(app)

    response = client.get("/api/sessions", headers=ASTL_HEADERS)

    assert response.status_code == 200
    assert [session["session_id"] for session in response.json()["sessions"]] == [
        "legacy-1c"
    ]


def test_tenant_id_header_is_case_insensitive(access_client):
    response = access_client.get(
        "/api/sessions",
        headers={
            "X-Finchvox-Role": "TENANT",
            "X-Finchvox-Tenant": "ASTL.DEV.FAMILY.",
        },
    )

    assert response.status_code == 200
    assert response.json()["total_count"] == 1


def test_disabled_access_control_preserves_standalone_behavior(temp_data_dir):
    _create_session(temp_data_dir, "standalone")
    app = FastAPI()
    register_ui_routes(app, temp_data_dir)
    client = TestClient(app)

    assert client.get("/api/sessions").status_code == 200
    assert client.get("/api/sessions").json()["total_count"] == 1


def test_access_control_policy_loads_from_environment(monkeypatch):
    monkeypatch.setenv("FINCHVOX_ACCESS_CONTROL_ENABLED", "true")
    monkeypatch.setenv(
        "FINCHVOX_LEGACY_TENANT_SOURCE_MAP",
        '{"1c": "ASTL.DEV.FAMILY"}',
    )

    policy = AccessControlPolicy.from_env()

    assert policy.enabled is True
    assert policy.legacy_tenant_source_map == {"1c": "astl.dev.family"}
