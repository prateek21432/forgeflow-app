import asyncio
import sys
import time

import psutil
import pytest

from app.agents.tester import TesterAgent as AgentTester
from app.config import settings
from app.database import Project, Run, User
from app.services.demo_service import DemoService
from app.tools.runtime import (
    WorkspaceTools,
    execute,
    make_snapshot,
    remove_snapshot,
    run_in_snapshot,
)


@pytest.mark.asyncio
async def test_timeout_captures_output(tmp_path):
    result = await execute(
        [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(15)"], tmp_path, 1
    )
    assert result.timed_out and result.exit_code == 124
    assert "started" in result.stdout


@pytest.mark.asyncio
async def test_unready_preview_respects_deadline_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "demo_start_timeout", 1)
    tools = WorkspaceTools(tmp_path)
    tools.write_file("backend/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    service = DemoService()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="did not become ready"):
        await service.start(99, tmp_path)
    assert time.monotonic() - started < 8
    assert not service.running


@pytest.mark.asyncio
async def test_cancellation_stops_the_child_process(tmp_path):
    marker = tmp_path / "child.pid"
    script = "import os,time,pathlib; pathlib.Path('child.pid').write_text(str(os.getpid())); time.sleep(30)"
    task = asyncio.create_task(execute([sys.executable, "-c", script], tmp_path, 40))
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.02)
    assert marker.exists()
    child_pid = int(marker.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not psutil.pid_exists(child_pid)


@pytest.mark.asyncio
async def test_no_collected_tests_is_failure(db, tmp_path):
    user = User(username="tester", password_hash="unused")
    db.add(user)
    db.flush()
    project = Project(
        owner_id=user.id,
        name="empty",
        slug="empty",
        workspace_path=str(tmp_path),
        branch="user/tester",
    )
    db.add(project)
    db.flush()
    run = Run(project_id=project.id, request="empty")
    db.add(run)
    db.commit()
    result = await AgentTester().test(project.id, run.id, tmp_path, include_acceptance=False)
    assert result["status"] == "FAIL"
    assert result["passed"] == 0
    assert result["failed"] == 1
    assert result["cases"][0]["name"] == "test_runner"
    assert "without a usable test report" in result["output"]


@pytest.mark.asyncio
async def test_docker_execution_is_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "execution_mode", "docker")
    tools = WorkspaceTools(tmp_path)
    tools.write_file(
        "tests/test_isolation.py",
        """import os
from pathlib import Path
def test_container():
    assert os.getuid() != 0
    assert 'OPENROUTER_API_KEY' not in os.environ
    assert not Path('/workspace/.git').exists()
    assert not Path('/workspace/.env').exists()
    Path('inside-container.txt').write_text('only in snapshot')
""",
    )
    (tmp_path / ".env").write_text("do-not-copy", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    snapshot = make_snapshot(tmp_path)
    try:
        result = await run_in_snapshot(snapshot, ["python", "-m", "pytest", "-q"], 30)
        assert result.exit_code == 0, result.stderr + result.stdout
        assert not (tmp_path / "inside-container.txt").exists()
    finally:
        remove_snapshot(snapshot)
