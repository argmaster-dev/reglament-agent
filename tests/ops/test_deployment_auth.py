from fastapi import FastAPI
from fastapi.testclient import TestClient

from reglament_agent.chat.start_page import install


def client(monkeypatch, tmp_path, password):
    monkeypatch.setenv("CHAINLIT_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("CHAT_EXPERT_PASSWORD", password)
    monkeypatch.setenv("CHAT_SECURE_COOKIES", "true")
    monkeypatch.delenv("CHAT_TRUSTED_HEADER", raising=False)
    app = FastAPI()
    install(app)
    return TestClient(app, base_url="https://site.example", follow_redirects=False)


def test_signed_expert_session_controls_mailpit(monkeypatch, tmp_path):
    with client(monkeypatch, tmp_path, "configured-password") as app:
        assert app.get("/_ops/mail-session").status_code == 403
        assert app.post("/start/employee").status_code == 303
        assert app.get("/_ops/mail-session").status_code == 403

        response = app.post("/start/expert", data={"password": "configured-password"})
        assert response.status_code == 303
        cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith("ra_session="))
        assert "Secure" in cookie and "HttpOnly" in cookie
        assert app.get("/_ops/mail-session").status_code == 204

        monkeypatch.setenv("CHAT_EXPERT_PASSWORD", "")
        assert app.get("/_ops/mail-session").status_code == 403


def test_expert_login_throttles_failed_attempts(monkeypatch, tmp_path):
    with client(monkeypatch, tmp_path, "configured-password") as app:
        for _ in range(5):
            response = app.post("/start/expert", data={"password": "wrong"})
            assert response.headers["location"] == "/start?error=1"
        response = app.post(
            "/start/expert", data={"password": "configured-password"}, headers={"X-Forwarded-For": "192.0.2.24"}
        )
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0
