"""Project files and bounded subprocesses. No shell strings reach the OS."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil
from langchain_core.tools import StructuredTool

from ..config import settings
from ..security import redact
from .guardrails import GuardrailError, Guardrails

MAX_FILE_BYTES = 200_000
IGNORED_SUFFIXES = {".pyc", ".db", ".sqlite", ".sqlite3", ".log", ".png", ".jpg", ".zip"}


class WorkspaceTools:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.guard = Guardrails(root)

    def list_files(self) -> list[str]:
        """List readable source files, excluding secrets and generated artifacts."""
        found = []
        for directory, folders, files in os.walk(self.root, followlinks=False):
            folders[:] = sorted(
                name
                for name in folders
                if not name.startswith(".") and name not in self.guard.PROTECTED
            )
            for name in sorted(files):
                path = Path(directory) / name
                relative = path.relative_to(self.root).as_posix()
                try:
                    safe = self.guard.path(relative)
                    if safe.suffix in IGNORED_SUFFIXES or safe.stat().st_size > MAX_FILE_BYTES:
                        continue
                    found.append(relative)
                except (GuardrailError, OSError):
                    continue
                if len(found) >= 1000:
                    return found
        return found

    def read_file(self, path: str) -> str:
        """Read one UTF-8 project file."""
        target = self.guard.path(path)
        if target.stat().st_size > MAX_FILE_BYTES:
            raise GuardrailError("File is too large; use a smaller source file.")
        return target.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> str:
        """Create or replace a UTF-8 source file within the project."""
        if len(content.encode()) > MAX_FILE_BYTES:
            raise GuardrailError("File content exceeds the size limit.")
        target = self.guard.path(path, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Saved {target.relative_to(self.root).as_posix()}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        """Replace one exact occurrence, refusing ambiguous edits."""
        content = self.read_file(path)
        if not old_text or content.count(old_text) != 1:
            raise GuardrailError("old_text must occur exactly once. Read the file and try again.")
        return self.write_file(path, content.replace(old_text, new_text, 1))

    def delete_file(self, path: str) -> str:
        """Delete a single file. Directories cannot be recursively deleted."""
        target = self.guard.path(path, write=True)
        if not target.is_file():
            raise GuardrailError("delete_file only accepts an existing file.")
        target.unlink()
        return f"Deleted {path}"

    def create_directory(self, path: str) -> str:
        """Create a directory within this project."""
        self.guard.path(path, write=True).mkdir(parents=True, exist_ok=True)
        return f"Created {path}"

    def search_code(self, query: str) -> list[dict]:
        """Find literal text in readable source files."""
        if not query or len(query) > 200:
            raise GuardrailError("Search must contain 1 to 200 characters.")
        matches = []
        for name in self.list_files():
            try:
                lines = self.read_file(name).splitlines()
            except UnicodeError:
                continue
            for number, line in enumerate(lines, 1):
                if query.lower() in line.lower():
                    matches.append({"path": name, "line": number, "text": line[:250]})
                    if len(matches) == 60:
                        return matches
        return matches

    def digest(self) -> dict[str, str]:
        return {
            name: hashlib.sha256(self.guard.path(name).read_bytes()).hexdigest()
            for name in self.list_files()
        }

    def context(self, limit: int = 55000) -> str:
        names = self.list_files()
        parts = ["Project files: " + json.dumps(names)]
        size = len(parts[0])
        # Include small, relevant source files. The agent can read anything omitted.
        for name in sorted(names, key=lambda n: (not n.endswith(".py"), n)):
            try:
                content = self.read_file(name)
            except UnicodeError:
                continue
            if size + len(content) > limit:
                continue
            parts.append(f"\n--- {name} ---\n{content}")
            size += len(content)
        return "".join(parts)

    def langchain_tools(self) -> dict:
        functions = [
            self.read_file,
            self.write_file,
            self.edit_file,
            self.delete_file,
            self.create_directory,
            self.list_files,
            self.search_code,
        ]
        return {f.__name__: StructuredTool.from_function(f) for f in functions}


@dataclass
class CommandResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def child_environment() -> dict[str, str]:
    allowed = ["PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT"]
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(
        {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "GIT_TERMINAL_PROMPT": "0"}
    )
    return env


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            child.kill()
        parent.kill()
        psutil.wait_procs([parent, *children], timeout=5)
    except psutil.NoSuchProcess:
        pass


async def execute(
    args: list[str], cwd: Path, timeout: int = 120, env: dict | None = None
) -> CommandResult:
    stopping = threading.Event()
    processes: list[subprocess.Popen] = []

    def run() -> CommandResult:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env or child_environment(),
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        processes.append(process)
        if stopping.is_set():
            stop_process(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            code, timed_out = process.returncode, False
        except subprocess.TimeoutExpired:
            stop_process(process)
            stdout, stderr = process.communicate()
            code, timed_out = 124, True
        return CommandResult(
            args,
            code,
            redact(stdout.decode(errors="replace")[-60000:]),
            redact(stderr.decode(errors="replace")[-20000:]),
            timed_out,
        )

    task = asyncio.create_task(asyncio.to_thread(run))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling an await does not stop its worker thread or child process.
        stopping.set()
        if processes:
            await asyncio.to_thread(stop_process, processes[0])
        await asyncio.gather(task, return_exceptions=True)
        raise


def make_snapshot(workspace: Path) -> Path:
    destination = settings.runtime_root / "executions" / uuid.uuid4().hex
    destination.mkdir(parents=True)
    files = WorkspaceTools(workspace)
    for name in files.list_files():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(files.guard.path(name), target)
    return destination


def remove_snapshot(path: Path) -> None:
    resolved = path.resolve()
    parent = (settings.runtime_root / "executions").resolve()
    if resolved != parent and resolved.is_relative_to(parent):
        shutil.rmtree(resolved, ignore_errors=True)


def container_options(snapshot: Path, name: str) -> list[str]:
    # Linux bind mounts retain ownership. Match the non-root host service user so
    # pytest and the preview can write their disposable files inside the snapshot.
    identity = []
    if hasattr(os, "getuid") and os.getuid() != 0:
        identity = ["--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp"]
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--label",
        "forgeflow.managed=true",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--memory=512m",
        "--cpus=1",
        "--pids-limit=128",
        "--read-only",
        *identity,
        "--tmpfs",
        "/tmp:rw,size=128m",
        "--mount",
        f"type=bind,source={snapshot},target=/workspace",
        "--workdir",
        "/workspace",
    ]


async def run_in_snapshot(snapshot: Path, args: list[str], timeout: int = 120) -> CommandResult:
    if settings.execution_mode == "docker":
        name = "forgeflow-test-" + uuid.uuid4().hex[:12]
        command = container_options(snapshot, name) + [
            "--network=none",
            settings.sandbox_image,
            *args,
        ]
        try:
            return await execute(command, snapshot, timeout)
        finally:
            await execute(["docker", "rm", "-f", name], snapshot, 10)
    command = [sys.executable, *args[1:]] if args[0] == "python" else args
    env = child_environment()
    env["PYTHONPATH"] = str(snapshot)
    return await execute(command, snapshot, timeout, env)
