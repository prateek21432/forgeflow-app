"""Start isolated previews, verify HTTP readiness, and clean up owned processes."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import settings
from ..tools.runtime import (
    child_environment,
    container_options,
    execute,
    make_snapshot,
    remove_snapshot,
    stop_process,
)


@dataclass
class Demo:
    project_id: int
    url: str
    snapshot: Path
    container: str = ""
    process: subprocess.Popen | None = None
    log_path: Path | None = None
    access_token: str = ""


class DemoService:
    def __init__(self):
        self.running: dict[int, Demo] = {}

    async def start(self, project_id: int, workspace: Path) -> str:
        await self.stop(project_id)
        snapshot = make_snapshot(workspace)
        if not (snapshot / "backend/main.py").is_file():
            remove_snapshot(snapshot)
            raise RuntimeError("The application has no backend/main.py entry point.")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        demo = Demo(project_id, f"http://127.0.0.1:{port}", snapshot)
        try:
            if settings.execution_mode == "docker":
                network = await execute(
                    ["docker", "network", "inspect", "forgeflow-preview-local"], snapshot, 10
                )
                if network.exit_code:
                    created = await execute(
                        # Internal bridges do not publish ports on this Docker Desktop version.
                        # Disable outbound NAT on a dedicated bridge instead. Host services
                        # remain reachable: this is not a complete network security boundary.
                        [
                            "docker",
                            "network",
                            "create",
                            "--opt",
                            "com.docker.network.bridge.enable_ip_masquerade=false",
                            "forgeflow-preview-local",
                        ],
                        snapshot,
                        10,
                    )
                    if created.exit_code:
                        raise RuntimeError(created.stderr)
                demo.container = f"forgeflow-demo-{project_id}-{snapshot.name[:8]}"
                command = container_options(snapshot, demo.container) + [
                    "-d",
                    "--network=forgeflow-preview-local",
                    "-p",
                    f"127.0.0.1:{port}:8000",
                    settings.sandbox_image,
                    "python",
                    "-m",
                    "uvicorn",
                    "backend.main:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                ]
                started = await execute(command, snapshot, 30)
                if started.exit_code:
                    raise RuntimeError(started.stderr)
            else:
                demo.log_path = snapshot / "server.log"
                env = child_environment()
                env["PYTHONPATH"] = str(snapshot)
                with demo.log_path.open("wb") as output:
                    demo.process = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "uvicorn",
                            "backend.main:app",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(port),
                        ],
                        cwd=snapshot,
                        env=env,
                        stdout=output,
                        stderr=output,
                        shell=False,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
            self.running[project_id] = demo
            self.save_registry()
            try:
                async with asyncio.timeout(settings.demo_start_timeout):
                    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                        while True:
                            try:
                                response = await client.get(demo.url + "/")
                                if response.status_code == 200:
                                    return demo.url
                            except httpx.HTTPError:
                                pass
                            await asyncio.sleep(0.5)
            except TimeoutError:
                pass
            logs = await self.logs(project_id)
            raise RuntimeError("Application did not become ready. " + logs[-4000:])
        except BaseException:
            self.running[project_id] = demo
            await self.stop(project_id)
            raise

    def save_registry(self) -> None:
        settings.runtime_root.mkdir(parents=True, exist_ok=True)
        entries = [
            {
                "project_id": d.project_id,
                "container": d.container,
                "snapshot": str(d.snapshot),
                "pid": d.process.pid if d.process else None,
            }
            for d in self.running.values()
        ]
        (settings.runtime_root / "demos.json").write_text(json.dumps(entries), encoding="utf-8")

    async def recover(self) -> None:
        registry = settings.runtime_root / "demos.json"
        if not registry.exists():
            return
        for entry in json.loads(registry.read_text(encoding="utf-8")):
            name = entry.get("container", "")
            if settings.execution_mode == "docker" and name.startswith("forgeflow-demo-"):
                await execute(["docker", "rm", "-f", name], settings.runtime_root, 10)
            # Never kill a stale PID without verifying its command and workspace.
            if entry.get("pid"):
                import psutil

                try:
                    process = psutil.Process(entry["pid"])
                    if Path(process.cwd()).resolve() == Path(
                        entry["snapshot"]
                    ).resolve() and "uvicorn" in " ".join(process.cmdline()):
                        for child in process.children(recursive=True):
                            child.kill()
                        process.kill()
                except (psutil.Error, OSError):
                    pass
            remove_snapshot(Path(entry["snapshot"]))
        self.save_registry()

    async def healthy(self, project_id: int) -> bool:
        demo = self.running.get(project_id)
        if not demo:
            return False
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                return (await client.get(demo.url + "/")).status_code == 200
        except httpx.HTTPError:
            return False

    async def logs(self, project_id: int) -> str:
        demo = self.running.get(project_id)
        if not demo:
            return "Demo is stopped."
        if demo.container:
            result = await execute(
                ["docker", "logs", "--tail", "120", demo.container], demo.snapshot, 10
            )
            return result.stdout + result.stderr
        return (
            demo.log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
            if demo.log_path
            else ""
        )

    async def stop(self, project_id: int) -> None:
        demo = self.running.pop(project_id, None)
        if not demo:
            return
        if demo.container:
            await execute(["docker", "rm", "-f", demo.container], demo.snapshot, 10)
        if demo.process:
            await asyncio.to_thread(stop_process, demo.process)
        remove_snapshot(demo.snapshot)
        self.save_registry()

    async def stop_all(self) -> None:
        for project_id in list(self.running):
            await self.stop(project_id)


demos = DemoService()
