"""Attach the ZIP's saved project folders to a newly registered local account."""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__" and "--local" in sys.argv:
    # Match manage.py --local before the database engine is constructed.
    os.environ.update(DATABASE_URL="sqlite:///" + (ROOT / "platform-local.db").as_posix(),
                      EXECUTION_MODE="local", RUNTIME_ROOT=str(ROOT / ".runtime/local"))

# Bootstrap above also supports running this script directly from the extracted ZIP.
from app.config import settings  # noqa: E402
from app.database import Project, SessionLocal, User, init_db  # noqa: E402
from app.services.git_service import GitService  # noqa: E402


async def import_projects(username: str, manifest: Path) -> int:
    entries = json.loads(manifest.read_text(encoding="utf-8"))["projects"]
    settings.prepare()
    init_db()
    count = 0
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        if not user:
            raise ValueError(
                "Register this username in the website first, then run the import again."
            )
        for entry in entries:
            folder = entry["folder"]
            if not re.fullmatch(r"[a-z0-9_-]+", folder):
                raise ValueError("Invalid project folder in the sharing manifest.")
            path = (settings.workspace_root / folder).resolve()
            if not path.is_relative_to(settings.workspace_root) or not path.is_dir():
                raise ValueError("A shared project folder is missing or outside the workspace.")
            existing = db.scalar(select(Project).where(Project.workspace_path == str(path)))
            if existing:
                if existing.owner_id != user.id:
                    raise ValueError("A shared folder already belongs to another local account.")
                continue
            branch = "user/" + user.username
            remote = await GitService().prepare(path, branch)
            db.add(
                Project(
                    owner_id=user.id,
                    name=str(entry["name"])[:120],
                    slug=folder,
                    workspace_path=str(path),
                    branch=branch,
                    remote_url=remote,
                    status="ready",
                    current_task="Imported source. Validate it here before creating a new checkpoint.",
                )
            )
            db.commit()
            count += 1
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="Your registered ForgeFlow username")
    parser.add_argument("--local", action="store_true", help="Import into the SQLite installation used by manage.py --local")
    args = parser.parse_args()
    imported = asyncio.run(import_projects(args.username, ROOT / "shared-projects.json"))
    print(f"Imported {imported} project(s). Refresh the dashboard. No AI requests were made.")
