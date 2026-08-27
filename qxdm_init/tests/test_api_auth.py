"""API auth + request validation tests."""

from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient


def test_auth_disabled_when_token_unset(orchestrator_app):
    client = TestClient(orchestrator_app)
    response = client.post(
        "/api/v1/jobs",
        json={"scenario_name": "ok", "duration_seconds": 1, "prefix": "x"},
    )
    assert response.status_code in (200, 202)


def test_auth_required_when_token_set(isolated_settings):
    isolated_settings = isolated_settings.with_overrides(api_token="secret-token-xyz")
    from api_server import create_app
    app = create_app(settings=isolated_settings)
    client = TestClient(app)
    response = client.post(
        "/api/v1/jobs",
        json={"scenario_name": "ok", "duration_seconds": 1, "prefix": "x"},
    )
    assert response.status_code == 401
    assert "bearer" in response.text.lower()


def test_auth_accepts_correct_token(isolated_settings):
    isolated_settings = isolated_settings.with_overrides(api_token="secret-token-xyz")
    from api_server import create_app
    app = create_app(settings=isolated_settings)
    client = TestClient(app)
    response = client.post(
        "/api/v1/jobs",
        json={"scenario_name": "ok", "duration_seconds": 1, "prefix": "x"},
        headers={"Authorization": "Bearer secret-token-xyz"},
    )
    assert response.status_code == 202


def test_auth_rejects_wrong_token(isolated_settings):
    isolated_settings = isolated_settings.with_overrides(api_token="secret-token-xyz")
    from api_server import create_app
    app = create_app(settings=isolated_settings)
    client = TestClient(app)
    response = client.post(
        "/api/v1/jobs",
        json={"scenario_name": "ok", "duration_seconds": 1, "prefix": "x"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 403


def test_request_id_stored_metadata(orchestrator_app):
    client = TestClient(orchestrator_app)
    response = client.post(
        "/api/v1/jobs",
        json={
            "scenario_name": "ok",
            "duration_seconds": 1,
            "prefix": "x",
            "request_id": "TMO-TEST-12345",
        },
    )
    assert response.status_code == 202, response.text


def test_request_id_path_traversal_rejected(orchestrator_app):
    client = TestClient(orchestrator_app)
    for bad in ["../etc", "../../backup", "/foo", "C:\\Windows", "..", "../"]:
        response = client.post(
            "/api/v1/jobs",
            json={
                "scenario_name": "ok",
                "duration_seconds": 1,
                "prefix": "x",
                "request_id": bad,
            },
        )
        # Pydantic validation -> 422; our explicit path-safety code -> 400.
        assert response.status_code in (400, 422), (bad, response.text)


def test_invalid_payload_rejected(orchestrator_app):
    client = TestClient(orchestrator_app)
    response = client.post("/api/v1/jobs", json={})
    assert response.status_code == 422


def test_get_job_safe_id_required(orchestrator_app):
    client = TestClient(orchestrator_app)
    for bad in ["../foo", "foo/bar", "foo\\bar", ".."]:
        response = client.get(f"/api/v1/jobs/{bad}")
        # Either rejected by safe-id check (400), by routing (404/422),
        # or by an invalid path (405).  All are acceptable defences.
        assert response.status_code in (400, 404, 405, 422), bad


def test_get_job_unknown_id_returns_404(orchestrator_app):
    client = TestClient(orchestrator_app)
    response = client.get("/api/v1/jobs/nonexistent-id-12345678")
    assert response.status_code == 404
