"""Independent acceptance tests, preserved regression tests, and JUnit results."""

import ast
import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from ..config import settings
from ..database import SessionLocal, TestRun
from ..schemas import AcceptanceSuite
from ..services.llm_gateway import ModelError, gateway
from ..tools.runtime import WorkspaceTools, make_snapshot, remove_snapshot, run_in_snapshot
from .supervisor import STACK_RULES


def validate_acceptance_code(code: str) -> None:
    """Reject known process-launch patterns before executing generated QA code.

    This is a correctness check; Docker remains the isolation boundary.
    """
    tree = ast.parse(code)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for name in node.names:
                target = (
                    f"{node.module}.{name.name}" if isinstance(node, ast.ImportFrom) else name.name
                )
                aliases[name.asname or name.name] = target
                if target.split(".")[0] in {"subprocess", "multiprocessing"}:
                    raise ModelError(
                        "Acceptance tests must not launch processes or nested test runners."
                    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = ast.unparse(node.func)
            head, _, tail = target.partition(".")
            target = aliases.get(head, head) + ("." + tail if tail else "")
            if (
                target == "pytest.main"
                or target in {"os.system", "os.popen"}
                or target.startswith("os.spawn")
            ):
                raise ModelError(
                    "Acceptance tests must not launch processes or nested test runners."
                )


class TesterAgent:
    def check_dir(self, run_id: int) -> Path:
        directory = settings.runtime_root / "checks" / str(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def preserve_regression_tests(self, workspace: Path, run_id: int) -> int:
        count = 0
        for name in WorkspaceTools(workspace).list_files():
            if name.startswith("tests/") and name.endswith(".py"):
                target = self.check_dir(run_id) / "regression" / name.removeprefix("tests/")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(workspace / name, target)
                count += 1
        return count

    async def prepare_acceptance(
        self, workspace: Path, run_id: int, request: str, plan: dict
    ) -> None:
        criteria = plan["acceptance_criteria"]
        # Keep each answer small instead of requesting the entire suite in one JSON.
        # Calls still pass through the shared per-run call and token budgets.
        batch_size = (
            4 if settings.llm_max_tokens == -1 else max(1, min(4, settings.llm_max_tokens // 750))
        )
        source = WorkspaceTools(workspace).context()
        cache_directory = self.check_dir(run_id) / "acceptance-cache"
        cache_directory.mkdir(exist_ok=True)
        suites = []
        for start in range(0, len(criteria), batch_size):
            assigned = list(range(start + 1, min(start + batch_size, len(criteria)) + 1))
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "source": source,
                        "request": request,
                        "plan": plan,
                        "assigned": assigned,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            cache = cache_directory / f"{fingerprint}.json"
            suite = (
                AcceptanceSuite.model_validate_json(cache.read_text(encoding="utf-8"))
                if cache.exists()
                else await gateway.generate_structured(
                    "You are an independent QA engineer. Write executable pytest tests for ONLY the assigned criteria. "
                    "Use fastapi.testclient.TestClient and import app from backend.main. Use a fresh temporary database "
                    "when the app supports configuration. Inspect routes and payloads in the source. "
                    "Test observable outcomes, edge cases, user isolation, and visible HTML/JS controls. "
                    "Do not skip tests, monkeypatch functionality under test, mock success, or weaken requirements to match broken code. "
                    "For incremental requests also check prior features. Return one self-contained Python test module and "
                    "covered_criteria as the original 1-based indices of the assigned criteria. "
                    "If a criterion requires browser or platform capabilities unavailable to pytest, "
                    "put its index in unverified_criteria and explain the limitation. Every assigned index "
                    "must appear exactly once across covered_criteria and unverified_criteria. "
                    "Never claim source-text checks verify actual browser playback. If all assigned checks "
                    "are unavailable, return a comment-only test module, not a fake passing test. "
                    + (
                        f"The entire JSON must fit in {settings.llm_max_tokens} output tokens. "
                        if settings.llm_max_tokens != -1
                        else "Keep the JSON within the provider's output limit. "
                    )
                    + "Keep the explanation to one sentence. Use concise fixtures and parametrized tests to avoid repetition. "
                    "Do not test unrelated criteria in this batch. Each module must be self-contained. "
                    "The runner executes pytest and enforces all tests passing. NEVER launch pytest, subprocesses, "
                    "or shell commands from a test. Do not write a test that runs the test suite. "
                    "Tests are stored outside the project source: use Path.cwd() for the project root, never __file__. "
                    "Test documented meaning rather than exact wording (for example, 0-9 documents all digits). "
                    + STACK_RULES,
                    json.dumps(
                        {
                            "request": request,
                            "summary": plan.get("summary", ""),
                            "source": source,
                            "assigned_criteria": {str(i): criteria[i - 1] for i in assigned},
                        }
                    ),
                    AcceptanceSuite,
                )
            )
            covered, unverified = set(suite.covered_criteria), set(suite.unverified_criteria)
            if covered & unverified or covered | unverified != set(assigned):
                raise ModelError(
                    f"Acceptance batch must account for criteria {assigned}; "
                    f"received covered={sorted(covered)}, unverified={sorted(unverified)}."
                )
            validate_acceptance_code(suite.test_code)
            cache.write_text(suite.model_dump_json(), encoding="utf-8")
            suites.append(suite)
        if not suites:
            raise ValueError("No acceptance criteria were supplied.")
        directory = self.check_dir(run_id)
        acceptance = directory / "acceptance"
        acceptance.mkdir(exist_ok=True)
        for index, suite in enumerate(suites):
            (acceptance / f"test_batch_{index:02d}.py").write_text(
                suite.test_code, encoding="utf-8"
            )
        (directory / "coverage.json").write_text(
            json.dumps([suite.model_dump() for suite in suites], indent=2), encoding="utf-8"
        )
        # Publish the completion marker only after ALL batches have been validated.
        (directory / "test_acceptance.py").write_text(
            "# Complete independent suite lives in acceptance/.\n", encoding="utf-8"
        )

    async def test(
        self,
        project_id: int,
        run_id: int,
        workspace: Path,
        phase: str = "validation",
        include_acceptance: bool = True,
    ) -> dict:
        snapshot = make_snapshot(workspace)
        source_digest = WorkspaceTools(snapshot).digest()
        report_dir = snapshot / ".forgeflow"
        report_dir.mkdir()
        paths = []
        if (snapshot / "tests").exists():
            paths.append("tests")
        checks = self.check_dir(run_id)
        if include_acceptance:
            if not (checks / "test_acceptance.py").exists():
                raise ValueError("No independent acceptance suite exists.")
            if (checks / "acceptance").is_dir():
                shutil.copytree(checks / "acceptance", report_dir / "acceptance")
                paths.append(".forgeflow/acceptance")
            else:
                shutil.copy2(checks / "test_acceptance.py", report_dir / "test_acceptance.py")
                paths.append(".forgeflow/test_acceptance.py")
            if (checks / "regression").exists():
                shutil.copytree(checks / "regression", report_dir / "regression")
                paths.append(".forgeflow/regression")
        try:
            command = [
                "python",
                "-m",
                "pytest",
                *paths,
                "-q",
                "--import-mode=importlib",
                # Large upload fixtures belong on the disposable workspace disk,
                # not the container's small RAM-backed /tmp filesystem.
                "--basetemp=.forgeflow/pytest-tmp",
                "-o",
                "addopts=",
                "--junitxml=.forgeflow/results.xml",
            ]
            result = await run_in_snapshot(snapshot, command, settings.command_timeout)
            cases = []
            report = report_dir / "results.xml"
            if report.exists() and report.stat().st_size < 5_000_000:
                root = ET.parse(report).getroot()
                for case in root.iter("testcase"):
                    failure = case.find("failure")
                    if failure is None:
                        failure = case.find("error")
                    skipped = case.find("skipped")
                    status = (
                        "FAIL" if failure is not None else "SKIP" if skipped is not None else "PASS"
                    )
                    cases.append(
                        {
                            "name": case.attrib.get("classname", "")
                            + "::"
                            + case.attrib.get("name", ""),
                            "status": status,
                            "reason": (failure.text or failure.attrib.get("message", ""))[:4000]
                            if failure is not None
                            else "",
                            "seconds": case.attrib.get("time", "0"),
                        }
                    )
            if not cases:
                reason = (
                    f"Test runner exited with code {result.exit_code} without a usable test report. "
                    "Individual test counts are unavailable."
                )
                if result.exit_code == 137:
                    reason += (
                        " Process was killed; check resource limits and recursive process launches."
                    )
                if result.timed_out:
                    reason += " Execution timed out."
                cases.append(
                    {"name": "test_runner", "status": "FAIL", "reason": reason, "seconds": "0"}
                )
            passed = sum(c["status"] == "PASS" for c in cases)
            failed = sum(c["status"] == "FAIL" for c in cases)
            skipped = sum(c["status"] == "SKIP" for c in cases)
            accepted = result.exit_code == 0 and passed > 0 and failed == 0 and skipped == 0
            output = result.stdout + "\n" + result.stderr
            if cases[0]["name"] == "test_runner":
                output += "\n" + cases[0]["reason"]
            limitations = []
            coverage = checks / "coverage.json"
            if include_acceptance and coverage.exists():
                metadata = json.loads(coverage.read_text(encoding="utf-8"))
                for suite in metadata if isinstance(metadata, list) else [metadata]:
                    if suite.get("unverified_criteria"):
                        limitations.append(
                            {
                                "criteria": suite["unverified_criteria"],
                                "reason": suite["explanation"],
                            }
                        )
                if limitations:
                    output += "\nUNVERIFIED REQUIREMENTS (not passing tests):\n" + json.dumps(
                        limitations
                    )
            summary = {
                "status": "PASS" if accepted else "FAIL",
                "total": len(cases),
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "cases": cases,
                "output": output,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "source_digest": source_digest,
                "unverified_requirements": limitations,
            }
            with SessionLocal() as db:
                db.add(
                    TestRun(
                        project_id=project_id,
                        run_id=run_id,
                        phase=phase,
                        status=summary["status"],
                        total=len(cases),
                        passed=passed,
                        failed=failed,
                        skipped=skipped,
                        cases=cases,
                        output=output,
                    )
                )
                db.commit()
            return summary
        finally:
            remove_snapshot(snapshot)
