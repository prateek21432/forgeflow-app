import asyncio
import re
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import settings
from app.main import app
from app.security import redact
from app.services.demo_service import demos
from app.services.llm_gateway import LLMGateway
from app.services.model_profiles import available_profiles, current_settings, use_profile
from app.services.preview_proxy import preview_url
from app.tools.runtime import WorkspaceTools


def profiles(monkeypatch):
    monkeypatch.setattr(settings, "gpt_enabled", True)
    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    monkeypatch.setattr(settings, "llm_model", "deepseek-chat")
    monkeypatch.setattr(settings, "llm_api_key", SecretStr("deepseek-private-test"))
    monkeypatch.setattr(settings, "llm_backup_api_key", SecretStr(""))
    monkeypatch.setattr(settings, "gpt_api_key", SecretStr("gpt-private-test"))
    monkeypatch.setattr(settings, "max_model_calls_per_run", -1)
    monkeypatch.setattr(settings, "max_tokens_per_run", -1)
    monkeypatch.setattr(settings, "llm_min_interval", 0)


@pytest.mark.asyncio
async def test_concurrent_profiles_keep_credentials_and_models_separate(monkeypatch):
    profiles(monkeypatch)
    calls = []

    async def post(self, url, **kwargs):
        await asyncio.sleep(0.01)
        calls.append((kwargs["json"]["model"], kwargs["headers"]["Authorization"]))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    gateway = LLMGateway()

    async def run(profile):
        with use_profile(profile):
            return await gateway.generate("system", "prompt")

    assert await asyncio.gather(run("default"), run("gpt")) == ["ok", "ok"]
    assert set(calls) == {
        ("deepseek-chat", "Bearer deepseek-private-test"),
        (settings.gpt_model, "Bearer gpt-private-test"),
    }
    assert current_settings() is settings
    assert "private-test" not in str(available_profiles())
    assert "gpt-private-test" not in redact("failure gpt-private-test")


def test_model_selection_persists_without_exposing_secrets(monkeypatch):
    profiles(monkeypatch)
    with TestClient(app) as client:
        page = client.get("/login")
        csrf = re.search('name="csrf-token" content="([^"]+)"', page.text)[1]
        page = client.post(
            "/register",
            data={"username": "profile-user", "password": "test-password-long", "csrf_token": csrf},
        )
        csrf = re.search('name="csrf-token" content="([^"]+)"', page.text)[1]
        response = client.post(
            "/settings/model", json={"profile": "gpt"}, headers={"x-csrf-token": csrf}
        )
        assert response.status_code == 200
        page = client.get("/settings")
        assert 'value="gpt" selected' in page.text
        assert "private-test" not in page.text
        assert (
            client.post(
                "/settings/model", json={"profile": "arbitrary"}, headers={"x-csrf-token": csrf}
            ).status_code
            == 422
        )


def test_closed_registration_is_enforced(monkeypatch):
    monkeypatch.setattr(settings, "registration_enabled", False)
    with TestClient(app) as client:
        page = client.get("/login")
        assert 'id="register"' not in page.text
        csrf = re.search('name="csrf-token" content="([^"]+)"', page.text)[1]
        response = client.post(
            "/register",
            data={"username": "outsider", "password": "long-password", "csrf_token": csrf},
        )
        assert response.status_code == 403


def test_disabled_gpt_is_hidden_and_rejected_even_with_key(monkeypatch):
    profiles(monkeypatch)
    monkeypatch.setattr(settings, "gpt_enabled", False)
    assert [p["id"] for p in available_profiles()] == ["default"]
    with pytest.raises(ValueError, match="not configured"):
        with use_profile("gpt"):
            pytest.fail("Disabled GPT must not execute")
    with TestClient(app) as client:
        page = client.get("/login")
        csrf = re.search('name="csrf-token" content="([^"]+)"', page.text)[1]
        page = client.post("/register", data={"username":"deepseek-user", "password":"12345", "csrf_token":csrf})
        csrf = re.search('name="csrf-token" content="([^"]+)"', page.text)[1]
        result = client.post("/settings/model", json={"profile":"gpt"}, headers={"x-csrf-token":csrf})
        assert result.status_code == 400
        assert 'value="gpt"' not in client.get("/settings").text


def test_preview_proxy_requires_live_token_and_forwards_app_routes(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "preview_origin", "https://testserver")
    monkeypatch.setattr(settings, "public_origin", "https://dashboard.test")
    tools = WorkspaceTools(tmp_path)
    tools.write_file(
        "backend/main.py",
        """from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
app=FastAPI()
@app.get('/')
def home(): return HTMLResponse('<h1>Preview works</h1>')
@app.post('/api/check')
async def check(request: Request):
    return {'body': await request.json(), 'cookie': request.headers.get('cookie','')}
""",
    )
    with TestClient(app, base_url="https://testserver") as client:
        asyncio.run(demos.start(999, tmp_path))
        try:
            assert client.get("/_preview/").status_code == 401
            url = preview_url(999)
            token = urlsplit(url).query
            assert (
                client.get("/_preview/__open?" + token, follow_redirects=False).status_code == 303
            )
            response = client.get("/_preview/")
            assert response.status_code == 200 and "Preview works" in response.text
            response = client.post("/_preview/api/check", json={"value": 12})
            assert response.status_code == 200
            assert response.json() == {"body": {"value": 12}, "cookie": ""}
            assert client.get("/_preview/", headers={"host": "localhost"}).status_code == 404
            asyncio.run(demos.stop(999))
            assert client.get("/_preview/").status_code == 401
        finally:
            asyncio.run(demos.stop_all())
