import re

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.parametrize("password", ["12345", "abcde"])
def test_five_character_password_registration_and_login(password):
    with TestClient(app) as client:
        csrf = token(client)
        response = client.post(
            "/register",
            data={"username": "simple", "password": password[:4], "csrf_token": csrf},
        )
        assert response.status_code == 400
        assert "at least 5 characters" in response.text
        response = client.post(
            "/register",
            data={"username": "simple", "password": password, "csrf_token": csrf},
        )
        assert response.status_code == 200
        csrf = re.search('name="csrf-token" content="([^"]+)"', response.text).group(1)
        client.post("/logout", data={"csrf_token": csrf})
        response = client.post(
            "/login",
            data={"username": "simple", "password": password, "csrf_token": token(client)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"


def token(client):
    page = client.get("/login")
    return re.search('name="csrf-token" content="([^"]+)"', page.text).group(1)


def register(client, name):
    response = client.post(
        "/register",
        data={"username": name, "password": "A-valid-test-password!", "csrf_token": token(client)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    return re.search('name="csrf-token" content="([^"]+)"', response.text).group(1)


def test_authentication_csrf_and_project_isolation():
    with TestClient(app) as owner:
        csrf = register(owner, "alice")
        assert owner.post("/projects", data={"name": "Unsafe"}).status_code == 403
        made = owner.post(
            "/projects", data={"name": "Alice app", "csrf_token": csrf}, follow_redirects=False
        )
        assert made.status_code == 303
        location = made.headers["location"]
        assert owner.get(location + "/api/state").status_code == 200
        assert owner.get(location + "/files?path=../../.env").status_code == 400
        assert owner.post(location + "/resume-verification").status_code == 403
        assert (
            owner.post(
                location + "/resume-verification", headers={"x-csrf-token": csrf}
            ).status_code
            == 409
        )
        assert (
            owner.post(
                location + "/chat",
                headers={"x-csrf-token": csrf, "origin": "http://evil.example"},
                json={"message": "Build a task app"},
            ).status_code
            == 403
        )
        with TestClient(app) as visitor:
            assert visitor.get(location + "/api/state").status_code == 401
            visitor_csrf = register(visitor, "bob")
            assert (
                visitor.post(
                    location + "/resume-verification", headers={"x-csrf-token": visitor_csrf}
                ).status_code
                == 404
            )
            assert visitor.get(location + "/api/state").status_code == 404
            assert visitor.get(location + "/files").status_code == 404
            try:
                with visitor.websocket_connect("/ws" + location):
                    raise AssertionError("Other user received WebSocket access")
            except Exception as exc:
                assert getattr(exc, "code", None) == 1008


def test_login_registration_errors_and_logout():
    with TestClient(app) as client:
        csrf = token(client)
        bad = client.post(
            "/register", data={"username": "bad name", "password": "short", "csrf_token": csrf}
        )
        assert bad.status_code == 400
        csrf = register(client, "arpit")
        client.post("/logout", data={"csrf_token": csrf})
        assert client.get("/dashboard", follow_redirects=False).status_code == 401
        csrf = token(client)
        assert (
            client.post(
                "/login", data={"username": "arpit", "password": "wrong", "csrf_token": csrf}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/login",
                data={
                    "username": "arpit",
                    "password": "A-valid-test-password!",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
