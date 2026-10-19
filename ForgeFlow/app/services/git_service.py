"""Deterministic Git operations with protected branches and verified commits."""

import base64
import os
import re
from pathlib import Path

from ..config import settings
from ..tools.guardrails import Guardrails
from ..tools.runtime import WorkspaceTools, child_environment, execute


class GitError(RuntimeError):
    pass


class GitService:
    async def command(self, workspace: Path, arguments: list[str], allow_failure: bool = False):
        env = child_environment()
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        env["GIT_TERMINAL_PROMPT"] = "0"
        token = settings.github_token.get_secret_value()
        if token and arguments[0] in {"push", "ls-remote"}:
            credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}",
                }
            )
        hooks = settings.runtime_root / "empty-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        result = await execute(
            ["git", "-c", f"core.hooksPath={hooks}", "-c", "core.autocrlf=false", *arguments],
            workspace,
            60,
            env,
        )
        if result.exit_code and not allow_failure:
            raise GitError((result.stderr or result.stdout).strip()[:3000])
        return result

    async def prepare(self, workspace: Path, branch: str, remote: str = "") -> str:
        Guardrails.branch(branch)
        if not (workspace / ".git").exists():
            await self.command(workspace, ["init", "-b", "main"])
        await self.command(workspace, ["config", "user.name", "ForgeFlow"])
        await self.command(workspace, ["config", "user.email", "forgeflow@local.invalid"])
        existing = await self.command(
            workspace, ["show-ref", "--verify", f"refs/heads/{branch}"], True
        )
        await self.command(
            workspace,
            ["checkout", branch] if existing.exit_code == 0 else ["checkout", "-b", branch],
        )
        if not remote:
            mirror = settings.runtime_root / "remotes" / f"{workspace.name}.git"
            mirror.parent.mkdir(parents=True, exist_ok=True)
            if not mirror.exists():
                await self.command(mirror.parent, ["init", "--bare", str(mirror)])
            remote = str(mirror)
        self.validate_remote(remote)
        current = await self.command(workspace, ["remote", "get-url", "origin"], True)
        action = "set-url" if current.exit_code == 0 else "add"
        await self.command(workspace, ["remote", action, "origin", remote])
        return remote

    def validate_remote(self, remote: str) -> None:
        if re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", remote):
            return
        path = Path(remote).resolve()
        if path.is_relative_to((settings.runtime_root / "remotes").resolve()):
            return
        raise GitError(
            "Remote must be a GitHub HTTPS repository or a platform-managed local mirror."
        )

    async def checkpoint(
        self, workspace: Path, branch: str, description: str, validated_digest: dict[str, str]
    ) -> str:
        Guardrails.branch(branch)
        if WorkspaceTools(workspace).digest() != validated_digest:
            raise GitError("Files changed after validation. Retest before checkpointing.")
        actual = await self.command(workspace, ["branch", "--show-current"])
        if actual.stdout.strip() != branch:
            raise GitError("Current branch does not match the project's protected user branch.")
        files = WorkspaceTools(workspace).list_files()
        await self.command(workspace, ["add", "-u"])
        # Include source even when a generated ignore rule accidentally hides it.
        if files:
            await self.command(workspace, ["add", "-f", "--", *files])
        staged = await self.command(workspace, ["diff", "--cached", "--quiet"], True)
        if staged.exit_code not in {0, 1}:
            raise GitError("Could not inspect staged changes.")
        if staged.exit_code == 1:
            await self.command(workspace, ["commit", "-m", description[:200]])
        # A fresh validation can reference the existing commit without fabricating
        # a source change or an empty commit.
        head = await self.command(workspace, ["rev-parse", "HEAD"])
        commit = head.stdout.strip()
        if not re.fullmatch("[a-f0-9]{40,64}", commit):
            raise GitError("Git did not return a valid commit ID.")
        return commit

    async def push(self, workspace: Path, branch: str, commit: str, remote: str) -> str:
        Guardrails.branch(branch)
        self.validate_remote(remote)
        external = remote.startswith("https://")
        if external and not settings.enable_git_push:
            return "disabled"
        actual = await self.command(workspace, ["rev-parse", "HEAD"])
        if actual.stdout.strip() != commit:
            raise GitError("HEAD differs from the validated commit.")
        await self.command(workspace, ["push", "origin", f"HEAD:refs/heads/{branch}"])
        result = await self.command(workspace, ["ls-remote", "origin", f"refs/heads/{branch}"])
        if result.stdout.split()[0] != commit:
            raise GitError("Remote branch did not match the validated commit.")
        return "github" if external else "local_mirror"
