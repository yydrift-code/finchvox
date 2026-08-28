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
    (temp_data_dir / "sessions" / "astl-session" / "audio.wav").write_bytes(
        b"RIFF-test-audio"
    )
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
    assert "log_count" in response.json()["sessions"][0]
    assert "tenant_id" in response.json()["sessions"][0]
    assert response.json()["data_dir"]


def test_tenant_list_contains_only_matching_sessions(access_client):
    response = access_client.get("/api/sessions", headers=ASTL_HEADERS)

    assert response.status_code == 200
    assert response.json()["total_count"] == 1
    assert [session["session_id"] for session in response.json()["sessions"]] == [
        "astl-session"
    ]
    assert "log_count" not in response.json()["sessions"][0]
    assert "tenant_id" not in response.json()["sessions"][0]
    assert response.json()["data_dir"] == ""


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (
            ADMIN_HEADERS,
            {
                "role": "admin",
                "tenant_id": None,
                "can_view_diagnostics": True,
                "can_upload_sessions": True,
            },
        ),
        (
            ASTL_HEADERS,
            {
                "role": "tenant",
                "tenant_id": "astl.dev.family",
                "can_view_diagnostics": False,
                "can_upload_sessions": False,
            },
        ),
    ],
)
def test_access_endpoint_exposes_ui_capabilities(access_client, headers, expected):
    response = access_client.get("/api/access", headers=headers)

    assert response.status_code == 200
    assert response.json() == expected


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
        "/api/sessions/owner-session/audio/download",
        "/api/sessions/owner-session/metrics",
        "/api/sessions/owner-session/download",
        "/api/sessions/owner-session/environment",
    ],
)
def test_tenant_cannot_open_another_tenants_session(access_client, path):
    response = access_client.get(path, headers=ASTL_HEADERS)

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"


def test_tenant_can_open_own_conversation_and_audio_status(access_client):
    response = access_client.get(
        "/api/sessions/astl-session/conversation", headers=ASTL_HEADERS
    )

    assert response.status_code == 200
    assert response.json()["messages"] == []
    assert response.json()["trace_start_time"] == 1_000_000_000

    audio_status = access_client.get(
        "/api/sessions/astl-session/audio/status", headers=ASTL_HEADERS
    )
    assert audio_status.status_code == 200

    audio_download = access_client.get(
        "/api/sessions/astl-session/audio/download", headers=ASTL_HEADERS
    )
    assert audio_download.status_code == 200
    assert audio_download.headers["content-type"] == "audio/wav"
    assert "attachment" in audio_download.headers["content-disposition"]


@pytest.mark.parametrize(
    "suffix",
    [
        "trace",
        "raw",
        "logs",
        "exceptions",
        "metrics",
        "download",
        "environment",
    ],
)
def test_tenant_cannot_open_own_diagnostics(access_client, suffix):
    response = access_client.get(
        f"/api/sessions/astl-session/{suffix}", headers=ASTL_HEADERS
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Diagnostic access is restricted"


def test_admin_can_open_diagnostics(access_client):
    response = access_client.get(
        "/api/sessions/owner-session/trace", headers=ADMIN_HEADERS
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
    assert client.get("/api/access").json()["can_view_diagnostics"] is True


def test_tenant_ui_hides_diagnostics_and_defaults_to_conversation():
    ui_dir = Path(__file__).parents[1] / "ui"
    detail_html = (ui_dir / "session_detail.html").read_text()
    detail_js = (ui_dir / "js" / "session_detail.js").read_text()
    sessions_html = (ui_dir / "sessions_list.html").read_text()

    assert 'x-show="canViewDiagnostics"' in detail_html
    assert "this.selectedView = 'conversation'" in detail_js
    assert 'x-show="canUploadSessions"' in sessions_html


def test_access_control_policy_loads_from_environment(monkeypatch):
    monkeypatch.setenv("FINCHVOX_ACCESS_CONTROL_ENABLED", "true")
    monkeypatch.setenv(
        "FINCHVOX_LEGACY_TENANT_SOURCE_MAP",
        '{"1c": "ASTL.DEV.FAMILY"}',
    )

    policy = AccessControlPolicy.from_env()

    assert policy.enabled is True
    assert policy.legacy_tenant_source_map == {"1c": "astl.dev.family"}
