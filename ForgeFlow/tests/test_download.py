import re

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import settings
from app.main import app


def test_public_setup_page_and_password_gated_zip(monkeypatch, tmp_path):
    archive = tmp_path / "shared.zip"
    archive.write_bytes(b"test archive")
    monkeypatch.setattr(settings, "share_zip_path", archive)
    monkeypatch.setattr(settings, "share_download_password", SecretStr("2992"))
    with TestClient(app) as client:
        page = client.get("/download")
        assert page.status_code == 200 and "without Docker" in page.text
        csrf = re.search('name="csrf-token" content="([^"]+)"', page.text)[1]
        assert client.get("/download/file", follow_redirects=False).status_code == 303
        assert client.post("/download/unlock", data={"password":"2992"}).status_code == 403
        wrong = client.post("/download/unlock", data={"password":"wrong", "csrf_token":csrf})
        assert "did not match" in wrong.text
        page = client.post("/download/unlock", data={"password":"2992", "csrf_token":csrf})
        assert "Download ForgeFlow" in page.text
        response = client.get("/download/file")
        assert response.status_code == 200 and response.content == b"test archive"
        assert "ForgeFlow-Final.zip" in response.headers["content-disposition"]
        assert client.get("/projects/8/api/state").status_code == 401
    with TestClient(app) as another:
        assert another.get("/download/file", follow_redirects=False).status_code == 303


def test_setup_page_works_without_a_hosted_zip(monkeypatch):
    monkeypatch.setattr(settings, "share_zip_path", None)
    monkeypatch.setattr(settings, "share_download_password", SecretStr(""))
    with TestClient(app) as client:
        assert "No distribution ZIP" in client.get("/download").text
        assert client.get("/download/file").status_code == 404
