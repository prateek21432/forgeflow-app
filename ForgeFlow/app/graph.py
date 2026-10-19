"""The real LangGraph workflow: plan, develop, test, review, checkpoint, preview."""

import asyncio
import shutil
import sys
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from .agents.developer import DeveloperAgent, RunCancelled, check_cancelled
from .agents.supervisor import SupervisorAgent
from .agents.tester import TesterAgent
from .config import settings
from .database import Checkpoint, GitCommit, Project, Run, SessionLocal, Task, utcnow
from .security import redact
from .services.browser_service import browser_service
from .services.change_report import capture_source, compare_sources
from .services.demo_service import demos
from .services.events import bus
from .services.git_service import GitService
from .services.llm_gateway import ModelBudgetError, ModelError, begin_budget, budget_usage
from .services.model_profiles import use_profile
from .tools.runtime import WorkspaceTools


class ProjectState(TypedDict, total=False):
    project_id: int
    run_id: int
    request: str
    model_profile: str
    plan: dict
    iteration: int
    feedback: dict
    results: dict
    changes: list[dict]
    before: dict[str, str]
    summary: str
    accepted: bool
    error: str
    demo_url: str
    implementation_unchanged: bool
    browser_results: dict
    resume_from: int
    resume_node: str
    resume_digest: dict[str, str]
    validated_checkpoint_id: int
    validated_digest: dict[str, str]


def resume_details(run: Run | None) -> tuple[str, dict] | None:
    """Select a safe continuation point; successful tests are always rerun."""
    if not run or run.status not in {"paused", "failed", "cancelled", "interrupted"}:
        return None
    saved = run.state or {}
    node = saved.get("resume_node")
    if node in {"plan", "develop", "test", "validate", "checkpoint", "failed"}:
        node = "plan" if not saved.get("plan") else node
        node = {"validate": "test", "checkpoint": "test", "failed": "develop"}.get(node, node)
        digest = saved.get("resume_digest")
    elif saved.get("plan") and saved.get("results", {}).get("status") == "PASS":
        node, digest = "test", saved["results"].get("source_digest")
    else:
        return None
    return (node, digest) if isinstance(digest, dict) else None


class VerificationPaused(RuntimeError):
    """Tests ran, but approval needs evidence the platform cannot yet provide."""

    def __init__(self, message: str, browser: dict, url: str):
        super().__init__(message)
        self.browser = browser
        self.url = url


class Workflow:
    def __init__(self, project_id: int, run_id: int):
        self.project_id, self.run_id = project_id, run_id
        self.current_node = "plan"
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            self.workspace = Path(project.workspace_path)
            self.branch = project.branch
            self.remote = project.remote_url
        self.supervisor = SupervisorAgent()
        self.developer = DeveloperAgent()
        self.tester = TesterAgent()
        self.git = GitService()

    async def stage(self, name: str, agent: str, message: str) -> None:
        check_cancelled(self.run_id)
        with SessionLocal() as db:
            run = db.get(Run, self.run_id)
            run.status, run.current_agent = name, agent
            project = db.get(Project, self.project_id)
            project.status = name
            db.commit()
        await bus.emit(self.project_id, agent, "agent_started", message, self.run_id)

    async def plan(self, state: ProjectState) -> dict:
        await self.stage(
            "planning",
            "Supervisor",
            "Inspecting the project and identifying what could be affected.",
        )
        await demos.stop(self.project_id)
        with SessionLocal() as db:
            project = db.get(Project, self.project_id)
            project.demo_status = "stopped"
            project.demo_url = None
            history = [
                {
                    "request": r.request,
                    "summary": r.summary,
                    "previous_test_failure": (r.state or {})
                    .get("results", {})
                    .get("output", "")[-5000:]
                    if (r.state or {}).get("results", {}).get("status") == "FAIL"
                    else "",
                }
                for r in db.scalars(
                    select(Run)
                    .where(Run.project_id == self.project_id, Run.id != self.run_id)
                    .order_by(Run.id.desc())
                    .limit(4)
                )
            ]
            db.commit()
        before = capture_source(self.workspace)
        plan = await self.supervisor.plan(self.workspace, state["request"], history)
        if not plan.supported:
            raise RuntimeError(
                plan.unsupported_reason or "This request is outside the supported stack."
            )
        # Validate predicted paths too, before they reach the report/UI.
        for path in plan.affected_files:
            WorkspaceTools(self.workspace).guard.path(path)
        has_tests = any(
            name.startswith("tests/") and name.endswith(".py")
            for name in WorkspaceTools(self.workspace).list_files()
        )
        baseline = None
        if has_tests:
            baseline = await self.tester.test(
                self.project_id, self.run_id, self.workspace, "baseline", False
            )
            await bus.emit(
                self.project_id,
                "Tester",
                "baseline_finished",
                f"Before changes: {baseline['passed']}/{baseline['total']} tests passed.",
                self.run_id,
            )
            if baseline["status"] == "PASS":
                self.tester.preserve_regression_tests(self.workspace, self.run_id)
            else:
                await bus.emit(
                    self.project_id,
                    "Tester",
                    "baseline_unvalidated",
                    "Existing tests do not pass. They remain editable; independent acceptance tests will verify the repair.",
                    self.run_id,
                )
        with SessionLocal() as db:
            for description in plan.tasks:
                db.add(Task(run_id=self.run_id, description=description))
            db.commit()
        await bus.emit(
            self.project_id,
            "Supervisor",
            "plan_created",
            plan.summary,
            self.run_id,
            plan.model_dump(),
        )
        return {
            "plan": plan.model_dump(),
            "before": before,
            "iteration": 0,
            "feedback": baseline if baseline and baseline["status"] == "FAIL" else {},
            "error": "",
        }

    async def develop(self, state: ProjectState) -> dict:
        await self.stage(
            "developing", "Developer", "Reading existing files and applying the requested changes."
        )
        iteration = state.get("iteration", 0) + 1
        with SessionLocal() as db:
            db.get(Run, self.run_id).iteration = iteration
            db.commit()
        await demos.stop(self.project_id)
        previous_digest = WorkspaceTools(self.workspace).digest()
        summary = await self.developer.implement(
            self.project_id,
            self.run_id,
            self.workspace,
            state["request"],
            state["plan"],
            state.get("feedback", {}),
        )
        unchanged = WorkspaceTools(self.workspace).digest() == previous_digest
        if unchanged:
            await bus.emit(
                self.project_id,
                "Developer",
                "agent_update",
                "No source edits were needed. Passing saved code to independent validation.",
                self.run_id,
            )
        changes = compare_sources(state["before"], capture_source(self.workspace))
        await bus.emit(
            self.project_id,
            "Developer",
            "changes_ready",
            f"{len(changes)} file(s) differ from the start of this request.",
            self.run_id,
            {"files": [{k: v for k, v in c.items() if k != "diff"} for c in changes]},
        )
        return {
            "iteration": iteration,
            "changes": changes,
            "summary": summary,
            "accepted": False,
            "implementation_unchanged": unchanged,
        }

    async def test(self, state: ProjectState) -> dict:
        await self.stage(
            "testing", "Tester", "Checking the new requirement and preserved existing features."
        )
        try:
            check_file = self.tester.check_dir(self.run_id) / "test_acceptance.py"
            if not check_file.exists():
                await self.tester.prepare_acceptance(
                    self.workspace, self.run_id, state["request"], state["plan"]
                )
            results = await self.tester.test(self.project_id, self.run_id, self.workspace)
        except ModelError:
            raise
        except Exception as exc:
            results = {
                "status": "FAIL",
                "total": 0,
                "passed": 0,
                "failed": 1,
                "skipped": 0,
                "cases": [],
                "output": redact(str(exc)),
            }
        await bus.emit(
            self.project_id,
            "Tester",
            "test_finished",
            f"{results['status']}: {results['passed']}/{results['total']} passed; "
            f"{results['failed']} failed; {results['skipped']} skipped.",
            self.run_id,
            results,
        )
        if (
            results["status"] != "PASS"
            and state.get("implementation_unchanged")
            and state.get("iteration", 0) > 1
        ):
            raise RuntimeError(
                "Independent tests still fail after an unchanged repair. "
                "Stopping repeated work; inspect the saved test failure report."
            )
        return {"results": results, "feedback": results}

    async def validate(self, state: ProjectState) -> dict:
        if WorkspaceTools(self.workspace).digest() != state["results"]["source_digest"]:
            return {
                "accepted": False,
                "feedback": {"error": "Source changed after testing; run tests again."},
            }
        await self.stage(
            "validating",
            "Supervisor",
            "Reviewing requirements and verifying that the application starts.",
        )
        try:
            url = await demos.start(self.project_id, self.workspace)
        except Exception as exc:
            if state.get("implementation_unchanged") and state.get("iteration", 0) > 1:
                raise RuntimeError(
                    "Preview startup is still blocked after an unchanged repair: "
                    + redact(str(exc))
                ) from exc
            return {"accepted": False, "feedback": {"startup_error": redact(str(exc))}}
        await bus.emit(
            self.project_id,
            "Supervisor",
            "demo_started",
            f"Application responded successfully at {url}",
            self.run_id,
        )
        disk = shutil.disk_usage(self.workspace)
        await bus.emit(
            self.project_id,
            "Tester",
            "browser_started",
            "Checking visible interactions in a real browser.",
            self.run_id,
        )
        browser = await browser_service.verify(
            self.run_id, self.workspace, state["request"], state["plan"], url
        )
        await bus.emit(
            self.project_id,
            "Tester",
            "browser_finished",
            f"Browser checks: {browser['status']}.",
            self.run_id,
            browser,
        )
        if browser["status"] == "FAIL":
            await demos.stop(self.project_id)
            if state.get("implementation_unchanged") and state.get("iteration", 0) > 1:
                raise RuntimeError(
                    "Browser checks still fail after an unchanged repair. See browser evidence."
                )
            return {
                "accepted": False,
                "feedback": {"browser_results": browser},
                "browser_results": browser,
            }
        evidence = dict(state["results"])
        evidence["platform_evidence"] = {
            "preview": {"url": url, "http_status": 200},
            "checkpoint": "Scheduled after implementation approval; owned by Git service.",
            "browser_interactions": browser,
            "workspace_storage": {"free_bytes": disk.free, "total_bytes": disk.total},
            "test_storage": "pytest fixtures use disposable .forgeflow/pytest-tmp on the workspace disk; multipart spooling uses container /tmp.",
        }
        review = await self.supervisor.validate(
            state["request"], state["plan"], state["changes"], evidence, self.workspace
        )
        if not review.accepted:
            if not review.requires_code_changes:
                raise VerificationPaused(
                    "Verification paused: a platform capability is needed. The preview remains available, "
                    "but no validated checkpoint was created. "
                    + "; ".join(review.missing_requirements or [review.summary]),
                    browser, url,
                )
            await demos.stop(self.project_id)
            if state.get("implementation_unchanged") and state.get("iteration", 0) > 1:
                raise RuntimeError(
                    "Validation remains blocked after an unchanged repair. "
                    + "; ".join(review.missing_requirements or [review.summary])
                )
            return {
                "accepted": False,
                "feedback": review.model_dump(),
                "browser_results": browser,
            }
        if WorkspaceTools(self.workspace).digest() != state["results"]["source_digest"]:
            await demos.stop(self.project_id)
            return {
                "accepted": False,
                "feedback": {"error": "Source changed during validation; run tests again."},
            }
        return {
            "accepted": True,
            "browser_results": browser,
            "demo_url": url,
            "summary": review.summary,
            "validated_digest": state["results"]["source_digest"],
        }

    async def checkpoint(self, state: ProjectState) -> dict:
        await self.stage(
            "checkpointing", "Git", "Saving the tested source to the project's user branch."
        )
        commit = await self.git.checkpoint(
            self.workspace, self.branch, state["request"], state["validated_digest"]
        )
        report = {
            "plan": state["plan"],
            "changes": state["changes"],
            "tests": state["results"],
            "summary": state["summary"],
            "demo_verified": True,
            "browser_results": state.get("browser_results"),
        }
        with SessionLocal() as db:
            cp = Checkpoint(
                project_id=self.project_id,
                run_id=self.run_id,
                description=state["request"],
                test_count=state["results"]["total"],
                passed_tests=state["results"]["passed"],
                commit_hash=commit,
                branch=self.branch,
                report=report,
            )
            db.add(cp)
            db.flush()
            record = GitCommit(
                project_id=self.project_id,
                checkpoint_id=cp.id,
                commit_hash=commit,
                branch=self.branch,
            )
            db.add(record)
            db.commit()
            cp_id, record_id = cp.id, record.id
        await bus.emit(
            self.project_id,
            "Git",
            "checkpoint_created",
            f"CP-{cp_id:03d} saved at {commit[:10]}",
            self.run_id,
        )
        try:
            push_status = await self.git.push(self.workspace, self.branch, commit, self.remote)
        except Exception as exc:
            with SessionLocal() as db:
                db.get(GitCommit, record_id).push_status = "failed"
                db.get(GitCommit, record_id).error = redact(str(exc))
                db.commit()
            raise RuntimeError("Local checkpoint saved, but push failed: " + str(exc)) from exc
        with SessionLocal() as db:
            db.get(GitCommit, record_id).push_status = push_status
            db.get(Checkpoint, cp_id).pushed = push_status in {"github", "local_mirror"}
            for task in db.scalars(select(Task).where(Task.run_id == self.run_id)):
                task.status = "completed"
            db.commit()
        await bus.emit(
            self.project_id,
            "Git",
            "git_push",
            {
                "local_mirror": "Verified backup to the local Git mirror.",
                "github": "Verified push to GitHub.",
                "disabled": "External push is disabled; the validated commit is saved locally.",
            }[push_status],
            self.run_id,
        )
        return {"validated_checkpoint_id": cp_id}

    async def failed(self, state: ProjectState) -> dict:
        raise ModelBudgetError(
            "Repair retry limit reached. Work is saved; use Resume saved work to continue. No successful checkpoint was created."
        )

    def after_test(self, state: ProjectState) -> str:
        if state["results"]["status"] == "PASS":
            return "validate"
        return (
            "develop"
            if settings.max_iterations == -1 or state["iteration"] < settings.max_iterations
            else "failed"
        )

    def after_validation(self, state: ProjectState) -> str:
        if state.get("accepted"):
            return "checkpoint"
        return (
            "develop"
            if settings.max_iterations == -1 or state["iteration"] < settings.max_iterations
            else "failed"
        )

    def graph(self, entry: str = "plan"):
        if entry not in {"plan", "develop", "test"}:
            raise ValueError("Unsupported workflow entry point.")
        graph = StateGraph(ProjectState)

        def tracked_node(name):
            async def invoke(state):
                self.current_node = name
                with SessionLocal() as db:
                    db.get(Run, self.run_id).state = {
                        **state,
                        "resume_node": name,
                        "resume_digest": WorkspaceTools(self.workspace).digest(),
                        "model_usage": budget_usage(),
                    }
                    db.commit()
                return await getattr(self, name)(state)

            return invoke

        for name in ("plan", "develop", "test", "validate", "checkpoint", "failed"):
            graph.add_node(name, tracked_node(name))
        graph.add_edge(START, entry)
        graph.add_edge("plan", "develop")
        graph.add_edge("develop", "test")
        graph.add_conditional_edges("test", self.after_test)
        graph.add_conditional_edges("validate", self.after_validation)
        graph.add_edge("checkpoint", END)
        graph.add_edge("failed", END)
        return graph.compile()


async def execute_run(project_id: int, run_id: int) -> None:
    with SessionLocal() as db:
        profile = (db.get(Run, run_id).state or {}).get("model_profile", "default")
    with use_profile(profile):
        await execute_selected_run(project_id, run_id)


async def execute_selected_run(project_id: int, run_id: int) -> None:
    begin_budget()
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        request = run.request
        resume_from = (run.state or {}).get("resume_from")
        profile = (run.state or {}).get("model_profile", "default")
    workflow = Workflow(project_id, run_id)
    state: ProjectState = {
        "project_id": project_id,
        "run_id": run_id,
        "request": request,
        "model_profile": profile,
    }
    status, error = "completed", ""
    try:
        entry = "plan"
        if resume_from:
            with SessionLocal() as db:
                previous = db.get(Run, resume_from)
                if not previous or previous.project_id != project_id:
                    raise ValueError(
                        "The previous verification run does not belong to this project."
                    )
                saved = previous.state or {}
                continuation = resume_details(previous)
                if continuation is None:
                    raise ValueError("No saved work is available to resume.")
                entry, digest = continuation
                if WorkspaceTools(workflow.workspace).digest() != digest:
                    raise ValueError(
                        "Source changed since verification. Submit a new request to test the changes."
                    )
                source = workflow.tester.check_dir(resume_from)
                destination = workflow.tester.check_dir(run_id)
                # Reuse the independent tests and scenarios, never their old results.
                for name in ("acceptance", "regression", "acceptance-cache"):
                    if (source / name).is_dir():
                        shutil.copytree(source / name, destination / name)
                for file in [
                    source / "test_acceptance.py",
                    source / "coverage.json",
                    *source.glob("browser-plan-*.json"),
                ]:
                    if file.is_file():
                        shutil.copy2(file, destination / file.name)
                state.update(
                    before=saved.get("before", capture_source(workflow.workspace)),
                    changes=saved.get("changes", []),
                    feedback=saved.get("feedback", {}),
                    iteration=0,
                    resume_from=resume_from,
                )
                if saved.get("plan"):
                    state["plan"] = saved["plan"]
                for description in saved.get("plan", {}).get("tasks", []):
                    db.add(Task(run_id=run_id, description=description))
                db.commit()
            await bus.emit(
                project_id,
                "Supervisor",
                "verification_resumed",
                f"Resuming saved work at {entry}. Existing files are retained; validation is required before completion.",
                run_id,
            )
        async with asyncio.timeout(
            None if settings.max_run_seconds == -1 else settings.max_run_seconds
        ):
            async for update in workflow.graph(entry).astream(
                state,
                config={
                    "recursion_limit": sys.maxsize
                    if settings.max_iterations == -1
                    else settings.max_iterations * 5 + 10
                },
            ):
                for value in update.values():
                    state.update(value)
                with SessionLocal() as db:
                    state["model_usage"] = budget_usage()
                    # Full node results are durable; private source snapshots stay out of browser APIs.
                    db.get(Run, run_id).state = dict(state)
                    db.commit()
        check_cancelled(run_id)
    except (RunCancelled, asyncio.CancelledError):
        status, error = "cancelled", "Run stopped. Unvalidated edits remain in the workspace."
    except ModelBudgetError as exc:
        status, error = "paused", redact(str(exc))
    except VerificationPaused as exc:
        status, error = "paused", redact(str(exc))
        state.update(browser_results=exc.browser, demo_url=exc.url, verification_pending=True)
    except TimeoutError:
        status, error = (
            "paused",
            "Time allowance reached. Work is saved; use Resume saved work to continue.",
        )
    except Exception as exc:
        status, error = "failed", redact(str(exc))
    keep_preview = status == "paused" and state.get("verification_pending", False)
    if status in {"paused", "failed"} and not keep_preview and workflow.current_node in {"test", "validate", "failed"}:
        if (workflow.workspace / "backend/main.py").is_file():
            try:
                state["demo_url"] = await demos.start(project_id, workflow.workspace)
                state["verification_pending"] = True
                keep_preview = True
            except Exception as exc:
                state["draft_preview_error"] = redact(str(exc))
    if status != "completed":
        if not keep_preview:
            await demos.stop(project_id)
        state["resume_node"] = workflow.current_node
        state["resume_digest"] = WorkspaceTools(workflow.workspace).digest()
        if state.get("before") is not None:
            state["changes"] = compare_sources(state["before"], capture_source(workflow.workspace))
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        project = db.get(Project, project_id)
        run.status, run.error, run.finished_at = status, error, utcnow()
        run.summary = state.get("summary", "") if status == "completed" else error
        state["model_usage"] = budget_usage()
        run.state = dict(state)
        project.status = status
        project.active_run_id = None
        project.demo_url = state.get("demo_url") if status == "completed" or keep_preview else None
        project.demo_status = "running" if status == "completed" or keep_preview else "stopped"
        db.commit()
    await bus.emit(
        project_id,
        "Supervisor",
        "run_completed"
        if status == "completed"
        else "run_paused"
        if status == "paused"
        else "error",
        state.get("summary", "Completed.") if status == "completed" else error,
        run_id,
    )
