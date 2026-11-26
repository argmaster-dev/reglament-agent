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


def test_role_selection_authenticates_first_user_request(monkeypatch, tmp_path):
    from fastapi import Depends
    from reglament_agent.chat.session import COOKIE_NAME, verify_identity

    browser = client(monkeypatch, tmp_path, "configured-password")
    from chainlit import auth
    from chainlit.auth import cookie, get_current_user
    from chainlit.config import config

    monkeypatch.setattr(cookie, "_cookie_secure", True)
    monkeypatch.setattr(cookie, "_cookie_samesite", "none")
    monkeypatch.setattr(config.code, "header_auth_callback", lambda headers: None)
    monkeypatch.setattr(auth, "get_data_layer", lambda: None)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    @browser.app.get("/user")
    async def user(current_user=Depends(get_current_user)):
        return current_user

    with browser:
        assert browser.get("/user").status_code == 401
        previous = None
        for role, field in (("employee", {"department": "Техническая дирекция"}),
                            ("expert", {"password": "configured-password"})):
            response = browser.post(f"/start/{role}", data=field)
            assert response.status_code == 303
            assert response.headers["location"] == "/"
            auth_cookie = next(line for line in response.headers.get_list("set-cookie")
                               if line.startswith("access_token="))
            assert "HttpOnly" in auth_cookie and "Secure" in auth_cookie
            identity = verify_identity(browser.cookies.get(COOKIE_NAME), "test-secret")
            assert identity and identity.email != previous

            first_user_request = browser.get("/user")
            assert first_user_request.status_code == 200
            assert first_user_request.json()["identifier"] == identity.email
            assert first_user_request.json()["metadata"] == {
                "role": role, "department": field.get("department"),
            }
            previous = identity.email

        assert browser.get("/start").status_code == 200
        assert browser.get("/user").status_code == 401
        refusal = browser.post("/start/expert", data={"password": "wrong"})
        assert refusal.headers["location"] == "/start?error=1"
        assert "set-cookie" not in refusal.headers
        assert browser.get("/user").status_code == 401
