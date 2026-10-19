import asyncio
import io
import re
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.database import Checkpoint, Project, Run
from app.main import app
from app.services.git_service import GitService
from app.services.project_export import exportable


def register(client, username):
    page = client.get("/login")
    csrf = re.search('name="csrf-token" content="([^"]+)"', page.text).group(1)
    page = client.post(
        "/register",
        data={
            "username": username,
            "password": "A-valid-test-password!",
            "csrf_token": csrf,
        },
    )
    return re.search('name="csrf-token" content="([^"]+)"', page.text).group(1)


def test_zip_uses_completed_commit_and_enforces_ownership(db):
    with TestClient(app) as owner:
        csrf = register(owner, "zipowner")
        created = owner.post(
            "/projects", data={"name": "Download app", "csrf_token": csrf}, follow_redirects=False
        )
        location = created.headers["location"]
        project_id = int(location.rsplit("/", 1)[1])
        project = db.get(Project, project_id)
        workspace = Path(project.workspace_path)
        assert owner.get(location + "/download").status_code == 409
        assert owner.get(location + "/api/state").json()["download_url"] is None
        (workspace / "backend").mkdir()
        (workspace / "assets").mkdir()
        (workspace / "data").mkdir()
        (workspace / "backend/main.py").write_bytes(b"print('completed version')\n")
        (workspace / "README.md").write_text("Run python backend/main.py")
        (workspace / "assets/logo.png").write_bytes(b"example-binary-asset")
        (workspace / ".env").write_text("EXAMPLE_SECRET=do-not-export")
        (workspace / "data/private.db").write_bytes(b"runtime-data")

        async def commit_files():
            git = GitService()
            await git.command(workspace, ["add", "-f", "."])
            await git.command(workspace, ["commit", "-m", "Export test fixture"])
            return (await git.command(workspace, ["rev-parse", "HEAD"])).stdout.strip()

        commit = asyncio.run(commit_files())
        run = Run(project_id=project_id, request="Build", status="completed")
        db.add(run)
        db.flush()
        db.add(
            Checkpoint(
                project_id=project_id,
                run_id=run.id,
                description="Completed",
                test_count=1,
                passed_tests=1,
                commit_hash=commit,
                branch=project.branch,
            )
        )
        db.commit()
        # Later failed edits must not leak into the completed download.
        (workspace / "backend/main.py").write_text("unvalidated change")
        (workspace / "assets/logo.png").unlink()
        response = owner.get(location + "/download")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "attachment" in response.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            root = project.slug + "/"
            assert archive.read(root + "backend/main.py") == b"print('completed version')\n"
            assert archive.read(root + "assets/logo.png") == b"example-binary-asset"
            assert root + "README.md" in archive.namelist()
            assert all(
                ".env" not in n and "/data/" not in n and "/.git/" not in n
                for n in archive.namelist()
            )
        assert owner.get(location + "/api/state").json()["download_url"] == location + "/download"
        assert not list((settings.runtime_root / "downloads").iterdir())
        with TestClient(app) as stranger:
            assert stranger.get(location + "/download").status_code == 401
            register(stranger, "zipvisitor")
            assert stranger.get(location + "/download").status_code == 404


def test_export_rejects_private_and_unsafe_paths():
    for name in [
        "../escape.py",
        "/absolute.py",
        "C:/secret",
        "a\\b",
        ".env",
        "backend/.env.local",
        ".git/config",
        "uploads/song.mp3",
        ".ssh/id_rsa",
        "keys/server.pem",
        "node_modules/module.js",
    ]:
        assert not exportable(name)
    assert exportable("assets/logo.png")
    assert exportable("README.md")


def test_paused_draft_download_and_preview_survive_server_restart(db):
    with TestClient(app) as owner:
        csrf = register(owner, "draftowner")
        location = owner.post("/projects", data={"name":"Saved draft", "csrf_token":csrf}, follow_redirects=False).headers["location"]
        project = db.get(Project, int(location.rsplit('/',1)[1]))
        workspace = Path(project.workspace_path)
        (workspace / 'backend').mkdir()
        (workspace / 'backend/main.py').write_text("from fastapi import FastAPI\napp=FastAPI()\n@app.get('/')\ndef home(): return {'version':'latest draft'}\n")
        (workspace / 'assets').mkdir()
        (workspace / 'assets/image.png').write_bytes(b'latest-binary-asset')
        (workspace / '.env').write_text('SECRET=must-not-export')
        project.status = 'paused'
        db.commit()
        result = owner.get(location+'/api/state').json()
        assert result['draft_download_url'] == location+'/download-draft'
        assert result['download_url'] is None
        assert result['can_start_preview'] and result['preview_is_draft']
        archive = owner.get(result['draft_download_url'])
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zip:
            assert b'latest draft' in zip.read(project.slug+'/backend/main.py')
            assert zip.read(project.slug+'/assets/image.png') == b'latest-binary-asset'
            assert '"validated": false' in zip.read('FORGEFLOW_EXPORT.json').decode()
            assert not any('.env' in n for n in zip.namelist())
        response = owner.post(location+'/demo/start', headers={'x-csrf-token':csrf})
        assert response.status_code == 200 and response.json()['draft']
        assert owner.get(location+'/api/state').json()['demo_status'] == 'running'
        assert db.query(Checkpoint).filter_by(project_id=project.id).count() == 0
        project.active_run_id = 999
        db.commit()
        assert owner.get(location+'/download-draft').status_code == 409
        assert owner.post(location+'/demo/start', headers={'x-csrf-token':csrf}).status_code == 409
        project.active_run_id = None
        db.commit()
        with TestClient(app) as visitor:
            assert visitor.get(location+'/download-draft').status_code == 401
            register(visitor,'draftvisitor')
            assert visitor.get(location+'/download-draft').status_code == 404
    # Application shutdown stopped processes; persisted source still opens as a draft.
    with TestClient(app) as restarted:
        page = restarted.get('/login')
        csrf = re.search('name="csrf-token" content="([^"]+)"',page.text)[1]
        page = restarted.post('/login',data={'username':'draftowner','password':'A-valid-test-password!','csrf_token':csrf})
        csrf = re.search('name="csrf-token" content="([^"]+)"',page.text)[1]
        assert restarted.post(location+'/demo/start',headers={'x-csrf-token':csrf}).status_code == 200
        assert restarted.get(location+'/download-draft').status_code == 200
