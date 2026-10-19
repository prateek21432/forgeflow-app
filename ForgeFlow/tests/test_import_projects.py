import json

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import Project, User
from scripts.import_projects import import_projects


@pytest.mark.asyncio
async def test_shared_project_import_is_idempotent_and_unvalidated(db, tmp_path):
    db.add(User(username="friend", password_hash="unused"))
    db.commit()
    project = settings.workspace_root / "shared-calculator"
    project.mkdir()
    (project / "README.md").write_text("Saved calculator source")
    manifest = tmp_path / "shared-projects.json"
    manifest.write_text(json.dumps({"projects": [{"folder": project.name, "name": "Calculator"}]}))
    assert await import_projects("friend", manifest) == 1
    assert await import_projects("friend", manifest) == 0
    imported = db.scalar(select(Project))
    assert imported.status == "ready"
    assert imported.branch == "user/friend"
    assert imported.demo_url is None
    assert (project / "README.md").read_text() == "Saved calculator source"


@pytest.mark.asyncio
async def test_shared_manifest_cannot_escape_workspace(db, tmp_path):
    db.add(User(username="friend", password_hash="unused"))
    db.commit()
    manifest = tmp_path / "shared-projects.json"
    manifest.write_text(json.dumps({"projects": [{"folder": "../../escape", "name": "Bad"}]}))
    with pytest.raises(ValueError, match="Invalid project folder"):
        await import_projects("friend", manifest)
    assert db.scalar(select(Project)) is None
