from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from openhachimi_agent.interface import cli as cli_module
from openhachimi_agent.interface.http import app, require_http_api_token


def test_new_route_is_registered():
    assert any(route.path == "/new" and "POST" in route.methods for route in app.routes)


def test_stop_route_is_registered():
    assert any(route.path == "/stop" and "POST" in route.methods for route in app.routes)


def _auth_test_client(token: str | None) -> TestClient:
    test_app = FastAPI()
    test_app.middleware("http")(require_http_api_token)
    test_app.state.config = SimpleNamespace(http_api_token=token)

    @test_app.get("/health")
    def health():
        return {"status": "ok"}

    @test_app.get("/state")
    def state():
        return {"status": "protected"}

    return TestClient(test_app)


def test_http_api_token_missing_rejects_protected_routes():
    with _auth_test_client(None) as client:
        response = client.get("/state")

    assert response.status_code == 503
    assert response.json() == {"detail": "HTTP API Token 未初始化"}


def test_http_api_token_rejects_missing_or_wrong_bearer_token():
    with _auth_test_client("secret-token") as client:
        missing = client.get("/state")
        wrong = client.get("/state", headers={"Authorization": "Bearer wrong-token"})

    assert missing.status_code == 401
    assert missing.json() == {"detail": "未授权"}
    assert wrong.status_code == 401
    assert wrong.json() == {"detail": "未授权"}


def test_http_api_token_accepts_matching_bearer_token():
    with _auth_test_client("secret-token") as client:
        response = client.get("/state", headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200
    assert response.json() == {"status": "protected"}


def test_http_api_token_does_not_protect_health():
    with _auth_test_client("secret-token") as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_cli_request_headers_uses_config_token_when_env_missing(monkeypatch):
    monkeypatch.delenv("OPENHACHIMI_HTTP_API_TOKEN", raising=False)
    cli_module._configured_http_api_token.cache_clear()
    monkeypatch.setattr(cli_module, "load_config", lambda: SimpleNamespace(http_api_token="config-token"))

    headers = cli_module._request_headers()

    assert headers["Authorization"] == "Bearer config-token"


def test_cli_request_headers_ignores_env_token(monkeypatch):
    monkeypatch.setenv("OPENHACHIMI_HTTP_API_TOKEN", "env-token")
    cli_module._configured_http_api_token.cache_clear()
    monkeypatch.setattr(cli_module, "load_config", lambda: SimpleNamespace(http_api_token="config-token"))

    headers = cli_module._request_headers()

    assert headers["Authorization"] == "Bearer config-token"
