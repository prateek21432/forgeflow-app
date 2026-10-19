import json

import pytest

from app.agents.tester import TesterAgent as AcceptanceTester
from app.agents.tester import validate_acceptance_code
from app.config import settings
from app.schemas import AcceptanceSuite
from app.services.llm_gateway import ModelBudgetError, ModelError, gateway


@pytest.mark.asyncio
async def test_acceptance_batches_cover_all_criteria_without_name_collisions(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_tokens", 3000)
    workspace = settings.workspace_root / "batch-test"
    workspace.mkdir()
    seen = []

    async def model(system, prompt, schema):
        assigned = list(map(int, json.loads(prompt)["assigned_criteria"]))
        seen.extend(assigned)
        assert len(assigned) <= 4
        return AcceptanceSuite(
            explanation="Batch checks",
            test_code=f"def test_requirement():\n    assert {assigned[0]} > 0\n",
            covered_criteria=assigned,
        )

    monkeypatch.setattr(gateway, "generate_structured", model)
    tester = AcceptanceTester()
    await tester.prepare_acceptance(
        workspace, 901, "Test requirements", {"acceptance_criteria": [str(i) for i in range(11)]}
    )
    assert seen == list(range(1, 12))
    files = list((tester.check_dir(901) / "acceptance").glob("test_*.py"))
    assert len(files) == 3
    assert (tester.check_dir(901) / "test_acceptance.py").exists()


@pytest.mark.asyncio
async def test_budget_pause_reuses_completed_acceptance_batches(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_tokens", 3000)
    workspace = settings.workspace_root / "cached-batches"
    workspace.mkdir()
    seen = []
    allow_second = False

    async def model(system, prompt, schema):
        assigned = list(map(int, json.loads(prompt)["assigned_criteria"]))
        if assigned[0] > 4 and not allow_second:
            raise ModelBudgetError("Allowance reached")
        seen.extend(assigned)
        return AcceptanceSuite(
            explanation="Check batch",
            test_code="def test_case():\n    assert True\n",
            covered_criteria=assigned,
        )

    monkeypatch.setattr(gateway, "generate_structured", model)
    tester = AcceptanceTester()
    plan = {"acceptance_criteria": [str(i) for i in range(8)]}
    with pytest.raises(ModelBudgetError):
        await tester.prepare_acceptance(workspace, 950, "Test app", plan)
    assert not (tester.check_dir(950) / "test_acceptance.py").exists()
    allow_second = True
    await tester.prepare_acceptance(workspace, 950, "Test app", plan)
    assert seen == list(range(1, 9))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["model", "coverage"])
async def test_incomplete_batches_never_publish_a_complete_suite(monkeypatch, failure):
    monkeypatch.setattr(settings, "llm_max_tokens", 3000)
    workspace = settings.workspace_root / f"incomplete-{failure}"
    workspace.mkdir()

    async def model(system, prompt, schema):
        assigned = list(map(int, json.loads(prompt)["assigned_criteria"]))
        if assigned[0] > 4 and failure == "model":
            raise ModelError("Response limit")
        return AcceptanceSuite(
            explanation="Batch checks",
            test_code="def test_requirement():\n    assert True\n",
            covered_criteria=[99] if failure == "coverage" else assigned,
        )

    monkeypatch.setattr(gateway, "generate_structured", model)
    tester = AcceptanceTester()
    run_id = 902 if failure == "model" else 903
    with pytest.raises((ModelError, ValueError)):
        await tester.prepare_acceptance(
            workspace,
            run_id,
            "Test requirements",
            {"acceptance_criteria": [str(i) for i in range(5)]},
        )
    assert not (tester.check_dir(run_id) / "test_acceptance.py").exists()


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess as sp\nsp.run(['pytest'])",
        "from subprocess import run as launch\nlaunch(['pytest'])",
        "import pytest as pt\npt.main([])",
        "from pytest import main as launch\nlaunch([])",
        "from os import system as launch\nlaunch('pytest')",
    ],
)
def test_nested_test_runners_are_rejected(code):
    with pytest.raises(ModelError, match="nested test runners"):
        validate_acceptance_code(code)


def test_regular_pytest_code_is_allowed():
    validate_acceptance_code(
        "import pytest\nfrom pathlib import Path\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_value(value):\n    assert value > 0\n"
    )


@pytest.mark.asyncio
async def test_unavailable_checks_are_recorded_as_unverified(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_tokens", 3000)
    workspace = settings.workspace_root / "unverified-checks"
    workspace.mkdir()

    async def model(system, prompt, schema):
        return AcceptanceSuite(
            explanation="Browser playback requires a separate browser check.",
            test_code="def test_api():\n    assert 2 + 2 == 4\n",
            covered_criteria=[1],
            unverified_criteria=[2],
        )

    monkeypatch.setattr(gateway, "generate_structured", model)
    tester = AcceptanceTester()
    await tester.prepare_acceptance(
        workspace,
        904,
        "Verify",
        {"acceptance_criteria": ["API works", "Check browser playback when available"]},
    )
    coverage = json.loads((tester.check_dir(904) / "coverage.json").read_text())
    assert coverage[0]["covered_criteria"] == [1]
    assert coverage[0]["unverified_criteria"] == [2]
