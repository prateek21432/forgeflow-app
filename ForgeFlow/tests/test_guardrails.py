import pytest

from app.tools.guardrails import GuardrailError, Guardrails
from app.tools.runtime import WorkspaceTools


@pytest.mark.parametrize(
    "path",
    [
        "../../outside",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "C:\\Windows\\win.ini",
        ".env",
        ".env.local",
        ".git/config",
        "app/../.env",
        "file.txt:secret",
        ".forgeflow/test_acceptance.py",
        "secrets.pem",
        ".git./config",
        ".git /config",
        "NUL.txt",
        "frontend/COM1",
    ],
)
def test_unsafe_paths_are_blocked(tmp_path, path):
    with pytest.raises(GuardrailError):
        Guardrails(tmp_path).path(path)


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "python -c print(1)",
        "python main.py && whoami",
        "pytest --override-ini=pythonpath=../../",
        "powershell whoami",
        "git -c core.hooksPath=evil status",
        "npm install evil",
    ],
)
def test_arbitrary_commands_are_blocked(tmp_path, command):
    with pytest.raises(GuardrailError):
        Guardrails(tmp_path).command(command)


def test_real_edits_are_scoped_and_exact(tmp_path):
    tools = WorkspaceTools(tmp_path)
    tools.write_file("backend/main.py", "hello\nworld\n")
    tools.edit_file("backend/main.py", "world", "reader")
    assert tools.read_file("backend/main.py") == "hello\nreader\n"
    with pytest.raises(GuardrailError):
        tools.edit_file("backend/main.py", "missing", "x")
    assert tools.search_code("reader")[0]["line"] == 2
    tools.delete_file("backend/main.py")
    assert tools.list_files() == []


def test_user_branch_validation():
    assert Guardrails.branch("user/arpit") == "user/arpit"
    for branch in ["main", "user/../main", "user/Alice", "user/a --force", "refs/heads/main"]:
        with pytest.raises(GuardrailError):
            Guardrails.branch(branch)


def test_project_listing_does_not_follow_symlinks(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        (tmp_path / "linked.py").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires OS permissions.")
    assert WorkspaceTools(tmp_path).list_files() == []
