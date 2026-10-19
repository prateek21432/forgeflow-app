"""Download the immutable source from a completed checkpoint."""

import asyncio
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from ..config import settings
from ..tools.guardrails import Guardrails
from .git_service import GitService


def exportable(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or ":" in name or ".." in path.parts:
        return False
    excluded = Guardrails.PROTECTED | {"data", "uploads", "logs", ".pytest_cache"}
    for part in path.parts:
        lower = part.lower()
        if (
            lower in excluded
            or lower in Guardrails.SECRET_NAMES
            or lower.startswith(".env")
            or lower.endswith((".pem", ".key", ".db", ".sqlite", ".sqlite3", ".log", ".pyc"))
        ):
            return False
    return bool(path.parts)


def filter_archive(source: Path, target: Path, folder: str) -> None:
    with (
        zipfile.ZipFile(source) as original,
        zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as output,
    ):
        for member in original.infolist():
            if member.is_dir() or not exportable(member.filename):
                continue
            if stat.S_ISLNK(member.external_attr >> 16):
                continue
            entry = zipfile.ZipInfo(folder + "/" + member.filename, date_time=member.date_time)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = member.external_attr
            with original.open(member) as src, output.open(entry, "w") as dst:
                shutil.copyfileobj(src, dst)


async def build_project_zip(workspace: Path, commit: str, name: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
        raise ValueError("Invalid checkpoint commit.")
    folder = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-")[:80] or "project"
    root = settings.runtime_root / "downloads"
    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="export-", dir=root))
    archive = temporary / (folder + ".zip")
    try:
        raw = temporary / ".checkpoint-source.zip"
        await GitService().command(
            workspace, ["archive", "--format=zip", f"--output={raw}", commit]
        )
        await asyncio.to_thread(filter_archive, raw, archive, folder)
        raw.unlink()
        return archive, temporary
    except BaseException:
        shutil.rmtree(temporary)
        raise


async def build_draft_zip(workspace: Path, name: str, status: str) -> tuple[Path, Path]:
    """Export saved work, including uncommitted files, without calling it validated."""
    folder = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-")[:80] or "project"
    root = settings.runtime_root / "downloads"
    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="draft-", dir=root))
    archive = temporary / (folder + "-draft.zip")

    def write_archive():
        guard = Guardrails(workspace)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for directory, folders, files in os.walk(workspace, followlinks=False):
                folders[:] = [name for name in folders if
                              not (Path(directory) / name).is_symlink()
                              and exportable((Path(directory) / name).relative_to(workspace).as_posix())]
                for filename in files:
                    path = Path(directory) / filename
                    relative = path.relative_to(workspace).as_posix()
                    if path.is_symlink() or not exportable(relative):
                        continue
                    source = guard.path(relative)
                    output.write(source, folder + "/" + relative)
            output.writestr("FORGEFLOW_EXPORT.json", json.dumps({
                "kind": "draft", "status": status, "validated": False,
                "notice": "Latest saved source. Tests or visual review may be incomplete or failing. "
                "Secrets, uploads, databases and generated dependencies are excluded.",
            }, indent=2))
    try:
        await asyncio.to_thread(write_archive)
        return archive, temporary
    except BaseException:
        shutil.rmtree(temporary)
        raise
