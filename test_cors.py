from importlib import reload

from fastapi.testclient import TestClient

import server


def make_client(monkeypatch, allowed_origins):
    monkeypatch.setenv("ALLOWED_ORIGINS", allowed_origins)
    reload(server)
    return TestClient(server.app)


def preflight(client, origin):
    return client.options(
        "/api/chat",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )


def test_cors_allowed_origin(monkeypatch):
    client = make_client(monkeypatch, "https://example.com")
    response = preflight(client, "https://example.com")

    assert response.status_code == 200
    assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com"
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"


def test_cors_disallowed_origin(monkeypatch):
    client = make_client(monkeypatch, "https://example.com")
    response = preflight(client, "https://malicious.com")

    assert response.headers.get("Access-Control-Allow-Origin") is None


def test_cors_multiple_origins(monkeypatch):
    client = make_client(monkeypatch, "https://example.com, https://another.com")

    response1 = preflight(client, "https://example.com")
    assert response1.headers.get("Access-Control-Allow-Origin") == "https://example.com"

    response2 = preflight(client, "https://another.com")
    assert response2.headers.get("Access-Control-Allow-Origin") == "https://another.com"


def test_cors_no_allowed_origins(monkeypatch):
    client = make_client(monkeypatch, "")
    response = preflight(client, "https://example.com")

    assert response.headers.get("Access-Control-Allow-Origin") is None
