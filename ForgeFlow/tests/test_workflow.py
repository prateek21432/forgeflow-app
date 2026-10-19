"""Exercise the real graph/tools/tests/Git/demo with deterministic model responses."""

import json

import pytest
from sqlalchemy import select

from app.agents.developer import DeveloperAgent, RunCancelled
from app.agents.tester import TesterAgent as IndependentTester
from app.config import settings
from app.database import Checkpoint, GitCommit, Project, Run, User
from app.database import TestRun as RecordedTestRun
from app.graph import Workflow, execute_run
from app.schemas import AcceptanceSuite, DeveloperResponse, Plan, ValidationDecision
from app.services.browser_service import browser_service
from app.services.demo_service import demos
from app.services.git_service import GitError, GitService
from app.services.llm_gateway import gateway
from app.tools.runtime import WorkspaceTools

BROKEN_APP = """from fastapi import FastAPI
from fastapi.responses import HTMLResponse
app = FastAPI()
@app.get('/', response_class=HTMLResponse)
def home():
    return '<h1>Task tracker</h1>'
@app.get('/api/tasks')
def tasks():
    return {'tasks': None}
"""
FIXED_APP = BROKEN_APP.replace("{'tasks': None}", "{'tasks': []}")
TEST_CODE = """from fastapi.testclient import TestClient
from backend.main import app
client = TestClient(app)
def test_home():
    assert client.get('/').status_code == 200
def test_task_list():
    assert client.get('/api/tasks').json()['tasks'] == []
"""


@pytest.fixture(autouse=True)
def scripted_browser(monkeypatch):
    # Real browser execution is covered independently in test_browser.py.
    async def verify(*args):
        return {"status": "UNVERIFIED", "cases": [], "limitations": ["Scripted workflow test"]}

    monkeypatch.setattr(browser_service, "verify", verify)


async def create_run(db, name="first", request="Build a task tracker"):
    user = User(username="arpit", password_hash="unused")
    db.add(user)
    db.flush()
    path = settings.workspace_root / name
    path.mkdir(exist_ok=True)
    remote = await GitService().prepare(path, "user/arpit")
    project = Project(
        owner_id=user.id,
        name=name,
        slug=name,
        workspace_path=str(path),
        branch="user/arpit",
        remote_url=remote,
    )
    db.add(project)
    db.flush()
    run = Run(project_id=project.id, request=request)
    db.add(run)
    db.flush()
    project.active_run_id = run.id
    db.commit()
    return project.id, run.id, path


def plan():
    return Plan(
        summary="Build a task tracker",
        tasks=["Implement a task list"],
        acceptance_criteria=["The task API returns a list"],
        risk="low",
        risk_reason="New project",
        affected_files=["backend/main.py"],
    )


@pytest.mark.asyncio
async def test_real_failure_repair_checkpoint_push_and_incremental_change(db, monkeypatch):
    project_id, run_id, path = await create_run(db, "repair-case")
    developer_calls = []

    async def model(system, prompt, schema):
        if schema is Plan:
            return plan()
        if schema is AcceptanceSuite:
            return AcceptanceSuite(
                explanation="Check the list contract", test_code=TEST_CODE, covered_criteria=[1]
            )
        if schema is ValidationDecision:
            return ValidationDecision(accepted=True, summary="Task list verified.")
        context = json.loads(prompt)
        developer_calls.append(context)
        count = len(developer_calls)
        if count == 1:
            return DeveloperResponse(
                summary="Create initial app",
                actions=[
                    {
                        "tool": "write_file",
                        "arguments": {"path": "backend/main.py", "content": BROKEN_APP},
                    },
                    {
                        "tool": "write_file",
                        "arguments": {"path": "tests/test_app.py", "content": TEST_CODE},
                    },
                ],
                finished=True,
            )
        if count == 2:
            assert context["feedback"]["status"] == "FAIL"
            assert "None" in context["feedback"]["output"]
            return DeveloperResponse(
                summary="Inspect failure",
                actions=[{"tool": "read_file", "arguments": {"path": "backend/main.py"}}],
            )
        assert context["known_files"]["backend/main.py"] == BROKEN_APP
        return DeveloperResponse(
            summary="Fix the list",
            actions=[
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "backend/main.py",
                        "old_text": "{'tasks': None}",
                        "new_text": "{'tasks': []}",
                    },
                }
            ],
            finished=True,
        )

    monkeypatch.setattr(gateway, "generate_structured", model)
    try:
        await execute_run(project_id, run_id)
        db.expire_all()
        run = db.get(Run, run_id)
        assert run.status == "completed", run.error
        records = list(
            db.scalars(
                select(RecordedTestRun)
                .where(RecordedTestRun.run_id == run_id)
                .order_by(RecordedTestRun.id)
            )
        )
        assert [r.status for r in records] == ["FAIL", "PASS"]
        assert await demos.healthy(project_id)
        cp = db.scalar(select(Checkpoint).where(Checkpoint.run_id == run_id))
        assert cp and cp.pushed
        git = db.scalar(select(GitCommit).where(GitCommit.checkpoint_id == cp.id))
        assert git.push_status == "local_mirror"

        original_test = (path / "tests/test_app.py").read_text(encoding="utf-8")
        next_run = Run(project_id=project_id, request="Add a wishlist endpoint")
        db.add(next_run)
        db.flush()
        db.get(Project, project_id).active_run_id = next_run.id
        db.commit()
        step = 0
        wishlist_test = (
            TEST_CODE
            + "\ndef test_wishlist():\n    assert client.get('/api/wishlist').json() == []\n"
        )

        async def incremental_model(system, prompt, schema):
            nonlocal step
            if schema is Plan:
                value = plan()
                value.summary = "Add wishlist"
                value.acceptance_criteria = ["Wishlist returns a list"]
                return value
            if schema is AcceptanceSuite:
                return AcceptanceSuite(
                    explanation="New feature and regression",
                    test_code=wishlist_test,
                    covered_criteria=[1],
                )
            if schema is ValidationDecision:
                return ValidationDecision(
                    accepted=True, summary="Wishlist added; prior list still works."
                )
            step += 1
            if step == 1:
                return DeveloperResponse(
                    summary="Read current backend",
                    actions=[{"tool": "read_file", "arguments": {"path": "backend/main.py"}}],
                )
            return DeveloperResponse(
                summary="Add wishlist",
                actions=[
                    {
                        "tool": "write_file",
                        "arguments": {
                            "path": "backend/main.py",
                            "content": FIXED_APP
                            + "\n@app.get('/api/wishlist')\ndef wishlist():\n    return []\n",
                        },
                    }
                ],
                finished=True,
            )

        monkeypatch.setattr(gateway, "generate_structured", incremental_model)
        await execute_run(project_id, next_run.id)
        db.expire_all()
        assert db.get(Run, next_run.id).status == "completed", db.get(Run, next_run.id).error
        assert (path / "tests/test_app.py").read_text(encoding="utf-8") == original_test
        assert db.scalar(select(Checkpoint).where(Checkpoint.run_id == next_run.id)).pushed
        assert db.query(Checkpoint).count() == 2
    finally:
        await demos.stop(project_id)


@pytest.mark.asyncio
async def test_unlimited_steps_and_repairs_still_allow_cancellation(db, monkeypatch):
    project_id, run_id, path = await create_run(db, "unlimited-case")
    monkeypatch.setattr(settings, "max_iterations", -1)
    monkeypatch.setattr(settings, "max_agent_steps", -1)
    workflow = Workflow(project_id, run_id)
    assert workflow.after_test({"iteration": 10000, "results": {"status": "FAIL"}}) == "develop"
    assert workflow.after_validation({"iteration": 10000, "accepted": False}) == "develop"
    calls = 0

    async def model(system, prompt, schema):
        nonlocal calls
        calls += 1
        assert json.loads(prompt)["steps_remaining"] is None
        if calls == 5:
            db.get(Run, run_id).cancel_requested = True
            db.commit()
        return DeveloperResponse(
            summary="Implementing",
            actions=[
                {
                    "tool": "write_file",
                    "arguments": {"path": f"part_{calls}.txt", "content": "progress"},
                }
            ],
            finished=False,
        )

    monkeypatch.setattr(gateway, "generate_structured", model)
    with pytest.raises(RunCancelled):
        await DeveloperAgent().implement(project_id, run_id, path, "Continue", {}, {})
    assert calls == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("working", [True, False])
async def test_unchanged_saved_code_requires_real_validation(db, monkeypatch, working):
    project_id, run_id, path = await create_run(db, f"saved-{working}")
    monkeypatch.setattr(settings, "max_iterations", 1)
    files = WorkspaceTools(path)
    files.write_file("backend/main.py", FIXED_APP if working else BROKEN_APP)
    files.write_file("tests/test_app.py", TEST_CODE)
    original_commit = await GitService().checkpoint(
        path, "user/arpit", "Saved work", files.digest()
    )

    async def model(system, prompt, schema):
        if schema is Plan:
            return plan()
        if schema is AcceptanceSuite:
            return AcceptanceSuite(
                explanation="Check saved work", test_code=TEST_CODE, covered_criteria=[1]
            )
        if schema is ValidationDecision:
            assert working
            evidence = json.loads(prompt)["tests"]["platform_evidence"]
            assert evidence["preview"]["http_status"] == 200
            assert evidence["preview"]["url"].startswith("http://127.0.0.1:")
            assert "after" in evidence["checkpoint"]
            return ValidationDecision(accepted=True, summary="Saved code verified.")
        return DeveloperResponse(summary="No edits needed", actions=[], finished=True)

    monkeypatch.setattr(gateway, "generate_structured", model)
    try:
        await execute_run(project_id, run_id)
        db.expire_all()
        run = db.get(Run, run_id)
        assert run.status == ("completed" if working else "paused"), run.error
        assert run.state["changes"] == []
        checkpoint = db.scalar(select(Checkpoint).where(Checkpoint.run_id == run_id))
        if working:
            assert checkpoint.commit_hash == original_commit
            assert checkpoint.pushed
        else:
            assert checkpoint is None
    finally:
        await demos.stop(project_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [True, False])
async def test_developer_hands_off_instead_of_repeating_work(db, monkeypatch, empty):
    project_id, run_id, path = await create_run(db, f"handoff-{empty}")
    monkeypatch.setattr(settings, "max_agent_steps", -1)
    calls = 0

    async def model(system, prompt, schema):
        nonlocal calls
        calls += 1
        assert "platform Tester owns" in system
        return DeveloperResponse(
            summary="Waiting for platform tools",
            finished=False,
            actions=[] if empty else [{"tool": "list_files", "arguments": {}}],
        )

    monkeypatch.setattr(gateway, "generate_structured", model)
    await DeveloperAgent().implement(project_id, run_id, path, "Verify", {}, {})
    assert calls == (1 if empty else 2)


@pytest.mark.asyncio
async def test_unavailable_platform_capability_does_not_loop(db, monkeypatch):
    project_id, run_id, path = await create_run(db, "platform-blocker")
    WorkspaceTools(path).write_file("backend/main.py", FIXED_APP)
    state = {
        "request": "Verify",
        "plan": {},
        "changes": [],
        "results": {"status": "PASS", "source_digest": WorkspaceTools(path).digest()},
    }
    workflow = Workflow(project_id, run_id)

    async def review(*args):
        return ValidationDecision(
            accepted=False,
            requires_code_changes=False,
            summary="Browser evidence unavailable",
            missing_requirements=["Browser evidence"],
        )

    monkeypatch.setattr(workflow.supervisor, "validate", review)
    with pytest.raises(RuntimeError, match="platform capability"):
        await workflow.validate(state)
    assert project_id in demos.running
    await demos.stop(project_id)


@pytest.mark.asyncio
async def test_capability_pause_persists_evidence_and_keeps_preview(db, monkeypatch):
    from app.graph import resume_details

    project_id, run_id, path = await create_run(db, "paused-preview")
    WorkspaceTools(path).write_file("backend/main.py", FIXED_APP)

    async def review(*args):
        return ValidationDecision(accepted=False, requires_code_changes=False,
                                  summary="Missing visual evidence")

    class Graph:
        def __init__(self, workflow):
            self.workflow = workflow

        async def astream(self, state, config):
            update = {"plan": plan().model_dump(), "changes": [],
                      "results": {"status": "PASS", "source_digest": WorkspaceTools(path).digest()}}
            yield {"test": update}
            state.update(update)
            self.workflow.current_node = "validate"
            await self.workflow.validate(state)

    monkeypatch.setattr(Workflow, "graph", lambda self, entry: Graph(self))
    from app.agents.supervisor import SupervisorAgent
    monkeypatch.setattr(SupervisorAgent, "validate", review)
    try:
        await execute_run(project_id, run_id)
        db.expire_all()
        run = db.get(Run, run_id)
        assert run.status == "paused"
        assert run.state["verification_pending"] is True
        assert run.state["browser_results"]["status"] == "UNVERIFIED"
        assert resume_details(run)[0] == "test"
        assert db.get(Project, project_id).demo_status == "running"
        assert await demos.healthy(project_id)
        assert db.scalar(select(Checkpoint).where(Checkpoint.run_id == run_id)) is None
    finally:
        await demos.stop(project_id)


@pytest.mark.asyncio
async def test_retry_exhaustion_never_creates_checkpoint(db, monkeypatch):
    project_id, run_id, path = await create_run(db, "failure-case")
    monkeypatch.setattr(settings, "max_iterations", 2)
    count = 0

    async def model(system, prompt, schema):
        nonlocal count
        if schema is Plan:
            return plan()
        if schema is AcceptanceSuite:
            return AcceptanceSuite(
                explanation="Must return a list", test_code=TEST_CODE, covered_criteria=[1]
            )
        count += 1
        actions = []
        if count == 1:
            actions = [
                {
                    "tool": "write_file",
                    "arguments": {"path": "backend/main.py", "content": BROKEN_APP},
                },
                {
                    "tool": "write_file",
                    "arguments": {"path": "tests/test_app.py", "content": TEST_CODE},
                },
            ]
        elif count == 2:
            return DeveloperResponse(
                summary="Inspect",
                actions=[{"tool": "read_file", "arguments": {"path": "backend/main.py"}}],
            )
        else:
            actions = [
                {
                    "tool": "write_file",
                    "arguments": {
                        "path": "backend/main.py",
                        "content": BROKEN_APP + "\n# A change that does not fix the bug\n",
                    },
                }
            ]
        return DeveloperResponse(summary="Work", actions=actions, finished=True)

    monkeypatch.setattr(gateway, "generate_structured", model)
    await execute_run(project_id, run_id)
    db.expire_all()
    assert db.get(Run, run_id).status == "paused"
    assert "retry limit" in db.get(Run, run_id).error
    assert db.query(Checkpoint).count() == 0
    assert db.get(Project, project_id).active_run_id is None


@pytest.mark.asyncio
async def test_changed_files_after_validation_block_git(db):
    _, _, path = await create_run(db, "changed-case")
    tools = WorkspaceTools(path)
    tools.write_file("main.py", "print('original')")
    digest = tools.digest()
    tools.write_file("main.py", "print('changed')")
    with pytest.raises(GitError, match="after validation"):
        await GitService().checkpoint(path, "user/arpit", "test", digest)


@pytest.mark.asyncio
async def test_files_changed_after_tests_cannot_be_validated(db, monkeypatch):
    project_id, run_id, path = await create_run(db, "changed-during-review")
    files = WorkspaceTools(path)
    files.write_file("backend/main.py", FIXED_APP)
    tested_digest = files.digest()
    files.write_file("backend/main.py", BROKEN_APP)

    async def no_model_call(*args, **kwargs):
        raise AssertionError("Stale tests must be rejected before requesting a model review.")

    monkeypatch.setattr(gateway, "generate_structured", no_model_call)
    result = await Workflow(project_id, run_id).validate(
        {"results": {"source_digest": tested_digest}}
    )
    assert not result["accepted"]
    assert "after testing" in result["feedback"]["error"]


@pytest.mark.asyncio
async def test_push_failure_keeps_checkpoint_but_never_claims_completion(db, monkeypatch):
    project_id, run_id, path = await create_run(db, "push-failure")

    async def model(system, prompt, schema):
        if schema is Plan:
            return plan()
        if schema is AcceptanceSuite:
            return AcceptanceSuite(
                explanation="Check API", test_code=TEST_CODE, covered_criteria=[1]
            )
        if schema is ValidationDecision:
            return ValidationDecision(accepted=True, summary="Verified app")
        return DeveloperResponse(
            summary="Implement app",
            actions=[
                {
                    "tool": "write_file",
                    "arguments": {"path": "backend/main.py", "content": FIXED_APP},
                },
                {
                    "tool": "write_file",
                    "arguments": {"path": "tests/test_app.py", "content": TEST_CODE},
                },
            ],
            finished=True,
        )

    async def failed_push(*args, **kwargs):
        raise GitError("Remote is unavailable")

    monkeypatch.setattr(gateway, "generate_structured", model)
    monkeypatch.setattr(GitService, "push", failed_push)
    await execute_run(project_id, run_id)
    db.expire_all()
    assert db.get(Run, run_id).status == "failed"
    checkpoint = db.scalar(select(Checkpoint).where(Checkpoint.run_id == run_id))
    assert checkpoint is not None and not checkpoint.pushed
    commit = db.scalar(select(GitCommit).where(GitCommit.checkpoint_id == checkpoint.id))
    assert commit.push_status == "failed"
    assert "unavailable" in commit.error
    assert not await demos.healthy(project_id)


@pytest.mark.asyncio
async def test_resume_reruns_tests_and_hands_browser_evidence_to_review(db, monkeypatch):
    project_id, previous_id, path = await create_run(db, "resume-browser")
    files = WorkspaceTools(path)
    files.write_file("backend/main.py", FIXED_APP)
    files.write_file("tests/test_app.py", TEST_CODE)
    previous = db.get(Run, previous_id)
    previous.status = "failed"
    previous.state = {
        "plan": plan().model_dump(),
        "before": {},
        "changes": [],
        "results": {"status": "PASS", "source_digest": files.digest()},
    }
    (IndependentTester().check_dir(previous_id) / "test_acceptance.py").write_text(TEST_CODE)
    resumed = Run(
        project_id=project_id, request=previous.request, state={"resume_from": previous_id}
    )
    db.add(resumed)
    db.flush()
    db.get(Project, project_id).active_run_id = resumed.id
    db.commit()
    calls = []

    async def browser(*args):
        return {"status": "PASS", "cases": [{"name": "Real interaction", "status": "PASS"}]}

    async def model(system, prompt, schema):
        calls.append(schema)
        assert schema is ValidationDecision  # No replanning or development calls.
        assert '"browser_interactions": {"status": "PASS"' in prompt
        return ValidationDecision(accepted=True, summary="Verified saved app")

    monkeypatch.setattr(browser_service, "verify", browser)
    monkeypatch.setattr(gateway, "generate_structured", model)
    await execute_run(project_id, resumed.id)
    db.expire_all()
    run = db.get(Run, resumed.id)
    assert run.status == "completed", run.error
    assert calls == [ValidationDecision]
    assert run.state["browser_results"]["status"] == "PASS"
    assert db.query(RecordedTestRun).filter_by(run_id=resumed.id).count() == 1
    assert db.query(Checkpoint).filter_by(run_id=resumed.id).count() == 1
    assert files.digest() == previous.state["results"]["source_digest"]
    await demos.stop(project_id)


@pytest.mark.asyncio
async def test_browser_failure_prevents_checkpoint_approval(db, monkeypatch):
    project_id, run_id, path = await create_run(db, "browser-failure")
    files = WorkspaceTools(path)
    files.write_file("backend/main.py", FIXED_APP)

    async def browser(*args):
        return {"status": "FAIL", "cases": [{"name": "Broken button", "status": "FAIL"}]}

    async def no_model(*args):
        raise AssertionError("Browser failures must go to repair before model approval.")

    monkeypatch.setattr(browser_service, "verify", browser)
    monkeypatch.setattr(gateway, "generate_structured", no_model)
    result = await Workflow(project_id, run_id).validate(
        {
            "request": "Task app",
            "plan": plan().model_dump(),
            "changes": [],
            "results": {"status": "PASS", "source_digest": files.digest()},
        }
    )
    assert not result["accepted"]
    assert result["feedback"]["browser_results"]["status"] == "FAIL"
    assert not await demos.healthy(project_id)
    assert db.query(Checkpoint).count() == 0


@pytest.mark.asyncio
async def test_resume_rejects_source_changes_before_any_model_call(db, monkeypatch):
    project_id, previous_id, path = await create_run(db, "stale-resume")
    files = WorkspaceTools(path)
    files.write_file("backend/main.py", FIXED_APP)
    previous = db.get(Run, previous_id)
    previous.status = "failed"
    previous.state = {
        "plan": plan().model_dump(),
        "results": {"status": "PASS", "source_digest": files.digest()},
    }
    resumed = Run(
        project_id=project_id, request=previous.request, state={"resume_from": previous_id}
    )
    db.add(resumed)
    db.flush()
    db.get(Project, project_id).active_run_id = resumed.id
    db.commit()
    files.write_file("backend/main.py", BROKEN_APP)

    async def no_model(*args):
        raise AssertionError("Stale verification must never reach the model.")

    monkeypatch.setattr(gateway, "generate_structured", no_model)
    await execute_run(project_id, resumed.id)
    db.expire_all()
    assert db.get(Run, resumed.id).status == "failed"
    assert "Source changed" in db.get(Run, resumed.id).error
    assert db.query(Checkpoint).count() == 0


@pytest.mark.asyncio
async def test_budget_pause_preserves_files_and_resumes_without_replanning(db, monkeypatch):
    project_id, run_id, path = await create_run(db, "budget-resume")
    monkeypatch.setattr(settings, "max_model_calls_per_run", 2)
    monkeypatch.setattr(settings, "max_tokens_per_run", -1)
    phases = []
    developer_calls = 0

    async def model(system, prompt, schema):
        nonlocal developer_calls
        gateway.reserve_call(system + prompt)
        phases.append(schema)
        if schema is Plan:
            return plan()
        if schema is AcceptanceSuite:
            return AcceptanceSuite(
                explanation="Verify API", test_code=TEST_CODE, covered_criteria=[1]
            )
        if schema is ValidationDecision:
            return ValidationDecision(accepted=True, summary="Saved app verified")
        developer_calls += 1
        if developer_calls == 1:
            return DeveloperResponse(
                summary="Save implementation",
                actions=[
                    {
                        "tool": "write_file",
                        "arguments": {"path": "backend/main.py", "content": FIXED_APP},
                    },
                    {
                        "tool": "write_file",
                        "arguments": {"path": "tests/test_app.py", "content": TEST_CODE},
                    },
                ],
                finished=False,
            )
        assert (path / "backend/main.py").read_text() == FIXED_APP
        return DeveloperResponse(summary="Saved implementation is ready", finished=True)

    monkeypatch.setattr(gateway, "generate_structured", model)
    await execute_run(project_id, run_id)
    db.expire_all()
    first = db.get(Run, run_id)
    assert first.status == "paused"
    assert first.state["resume_node"] == "develop"
    assert first.state["model_usage"]["calls"] == 2
    assert first.state["changes"]
    digest = WorkspaceTools(path).digest()
    assert db.query(Checkpoint).count() == 0

    previous_id = run_id
    for expected in ("paused", "completed"):
        resumed = Run(
            project_id=project_id, request=first.request, state={"resume_from": previous_id}
        )
        db.add(resumed)
        db.flush()
        db.get(Project, project_id).active_run_id = resumed.id
        db.commit()
        await execute_run(project_id, resumed.id)
        db.expire_all()
        current = db.get(Run, resumed.id)
        assert current.status == expected, current.error
        assert current.state["model_usage"]["calls"] <= 2
        assert WorkspaceTools(path).digest() == digest
        previous_id = resumed.id
    assert phases.count(Plan) == 1
    assert phases.count(AcceptanceSuite) == 1
    assert db.query(Checkpoint).count() == 1
    await demos.stop(project_id)
