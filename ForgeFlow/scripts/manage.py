"""Start or stop this local installation without touching unrelated services."""

import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime"
PORT = 8791
URL = f"http://127.0.0.1:{PORT}"
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402


def command(arguments: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:1200])
    return result


def owned_server(process: psutil.Process) -> bool:
    try:
        args = process.cmdline()
        is_uvicorn = "uvicorn" in args or (args and Path(args[0]).stem.lower() == "uvicorn")
        return Path(process.cwd()).resolve() == ROOT and is_uvicorn and "app.main:app" in args
    except (psutil.Error, OSError):
        return False


def listeners() -> list[psutil.Process]:
    found = []
    for connection in psutil.net_connections(kind="tcp"):
        if (
            connection.status == psutil.CONN_LISTEN
            and connection.laddr.port == PORT
            and connection.pid
        ):
            try:
                process = psutil.Process(connection.pid)
                if not any(previous.pid == process.pid for previous in found):
                    found.append(process)
            except psutil.NoSuchProcess:
                continue
    return found


def healthy() -> bool:
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            data = client.get(URL + "/health").json()
            database = "sqlite" if settings.database_url.startswith("sqlite") else "postgresql"
            return (
                data.get("status") == "ok"
                and data.get("database") == database
                and data.get("execution") == settings.execution_mode
            )
    except (httpx.HTTPError, ValueError):
        return False


def start(open_browser: bool) -> None:
    existing = listeners()
    if existing:
        if all(owned_server(process) for process in existing) and healthy():
            print("ForgeFlow is already running at " + URL)
            if open_browser:
                webbrowser.open(URL)
            return
        raise RuntimeError(f"Port {PORT} is occupied. The existing program has been left running.")
    if not (ROOT / ".env").is_file():
        raise RuntimeError("Run scripts/setup.py and configure .env first.")
    prepare_services()
    RUNTIME.mkdir(exist_ok=True)
    with (
        (RUNTIME / "server.stdout.log").open("wb") as output,
        (RUNTIME / "server.stderr.log").open("wb") as errors,
    ):
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(PORT),
            ],
            cwd=ROOT,
            stdout=output,
            stderr=errors,
            env=os.environ.copy(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    (RUNTIME / "server.pid").write_text(str(process.pid), encoding="utf-8")
    for _ in range(30):
        if healthy():
            print("ForgeFlow is ready: " + URL)
            if open_browser:
                webbrowser.open(URL)
            return
        if process.poll() is not None:
            break
        time.sleep(1)
    raise RuntimeError("Startup did not complete. Read .runtime/server.stderr.log for details.")


def prepare_services() -> None:
    """Local mode never calls Docker; it uses the configured database directly."""
    if settings.execution_mode == "local":
        print("Starting without Docker. Generated code runs directly on this laptop.", flush=True)
        print("Use this mode only with trusted projects and prompts.", flush=True)
        return
    if settings.execution_mode != "docker":
        raise RuntimeError("EXECUTION_MODE must be docker or local.")
    print("Checking Docker and the runner...", flush=True)
    command(["docker", "info"], 15)
    try:
        command(["docker", "image", "inspect", settings.sandbox_image], 15)
    except RuntimeError:
        print("Building the runner for the first time...", flush=True)
        command(
            ["docker", "build", "-f", "Dockerfile.runner", "-t", settings.sandbox_image, "."], 600
        )
    if settings.database_url.startswith("postgresql"):
        command(["docker", "compose", "up", "-d", "--wait", "database"], 60)


def use_local_mode() -> None:
    """Select a separate SQLite database without editing the user's .env."""
    global RUNTIME
    RUNTIME = ROOT / ".runtime" / "local"
    RUNTIME.mkdir(parents=True, exist_ok=True)
    settings.execution_mode = "local"
    settings.database_url = "sqlite:///" + (ROOT / "platform-local.db").as_posix()
    settings.runtime_root = RUNTIME
    os.environ.update(
        EXECUTION_MODE="local",
        DATABASE_URL=settings.database_url,
        RUNTIME_ROOT=str(RUNTIME),
    )


def stop() -> None:
    servers = listeners()
    foreign = [process for process in servers if not owned_server(process)]
    if foreign:
        locations = []
        for process in foreign:
            try:
                locations.append(f"PID {process.pid}: {process.cwd()}")
            except (psutil.Error, OSError):
                locations.append(f"PID {process.pid}: folder could not be inspected")
        raise RuntimeError(
            f"Nothing was stopped. Port {PORT} belongs to another installation or process. "
            "Run the stop command from the folder that started the server. " + "; ".join(locations)
        )
    for process in servers:
        try:
            # Stop the owning launcher/reloader too, so it cannot restart its worker.
            parent = process.parent()
            while parent is not None and owned_server(parent):
                process, parent = parent, parent.parent()
            children = process.children(recursive=True)
            process.terminate()
            _, alive = psutil.wait_procs([process, *children], timeout=5)
            for child in alive:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(alive, timeout=3)
        except psutil.NoSuchProcess:
            continue
    # A success message is only valid after the listening socket has disappeared.
    for _ in range(20):
        if not listeners():
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"Port {PORT} is still listening. Shutdown could not be verified.")
    # Container names come from this installation's private process registry.
    registry = RUNTIME / "demos.json"
    if registry.exists():
        for entry in json.loads(registry.read_text(encoding="utf-8")):
            name = entry.get("container", "")
            if settings.execution_mode == "docker" and name.startswith("forgeflow-demo-"):
                try:
                    command(["docker", "rm", "-f", name], 15)
                except RuntimeError as exc:
                    print("Preview cleanup:", exc)
    if servers:
        print(f"ForgeFlow stopped. Nothing is listening on {URL}.")
    else:
        print(f"ForgeFlow was already stopped: nothing is listening on {URL}.")
    print("Database data and saved projects are retained. Refresh the browser to verify.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop"])
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8791, help="Local port for this installation")
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run without Docker using a separate SQLite database; leave .env unchanged",
    )
    arguments = parser.parse_args()
    if not 1024 <= arguments.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    PORT = arguments.port
    URL = f"http://127.0.0.1:{PORT}"
    if arguments.local:
        use_local_mode()
    try:
        start(not arguments.no_browser) if arguments.action == "start" else stop()
    except (OSError, RuntimeError, subprocess.TimeoutExpired, psutil.Error) as error:
        print("Could not complete the action:", error)
        raise SystemExit(1)
