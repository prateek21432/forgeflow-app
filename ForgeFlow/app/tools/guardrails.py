"""Validate paths and command arguments independently of model instructions."""

import re
import shlex
from pathlib import Path, PureWindowsPath


class GuardrailError(ValueError):
    pass


class Guardrails:
    PROTECTED = {".git", ".forgeflow", ".venv", "node_modules", "__pycache__", ".ssh", ".runtime"}
    SECRET_NAMES = {"id_rsa", "id_ed25519", "credentials.json", ".npmrc", ".pypirc"}

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    def path(self, value: str | Path, write: bool = False) -> Path:
        text = str(value).replace("\\", "/")
        if not text or "\x00" in text or ":" in text or PureWindowsPath(text).drive:
            raise GuardrailError("Use a relative project path.")
        candidate = Path(text)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise GuardrailError("Path escapes the project workspace.")
        for part in candidate.parts:
            name = part.lower()
            device = name.split(".", 1)[0]
            if part != part.rstrip(" .") or device in {
                "con",
                "prn",
                "aux",
                "nul",
                "conin$",
                "conout$",
                *[f"com{i}" for i in range(1, 10)],
                *[f"lpt{i}" for i in range(1, 10)],
            }:
                raise GuardrailError("Windows device names and path aliases are not allowed.")
            if (
                name in self.PROTECTED
                or name in self.SECRET_NAMES
                or name.startswith(".env")
                or name.endswith((".pem", ".key"))
            ):
                raise GuardrailError("Protected path.")
        resolved = (self.workspace / candidate).resolve()
        if resolved != self.workspace and not resolved.is_relative_to(self.workspace):
            raise GuardrailError("Path escapes the project workspace.")
        # Reject links even when they currently point inside the workspace.
        current = self.workspace
        for part in candidate.parts:
            current = current / part
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise GuardrailError("Symbolic links and junctions are not agent-editable.")
        if write and resolved == self.workspace:
            raise GuardrailError("Cannot modify the workspace root.")
        return resolved

    def command(self, command: str | list[str]) -> list[str]:
        args = shlex.split(command) if isinstance(command, str) else list(command)
        if not args or len(args) > 40 or any(not isinstance(a, str) or len(a) > 1000 for a in args):
            raise GuardrailError("Invalid command arguments.")
        joined = " ".join(args)
        if any(
            token in joined for token in ["&&", ";", "|", ">", "<", "`", "$(", "\n", "\r", "\x00"]
        ):
            raise GuardrailError("Shell composition is not allowed.")
        if args[:3] == ["python", "-m", "pytest"] or args[:1] == ["pytest"]:
            remaining = args[3:] if args[0] == "python" else args[1:]
            for item in remaining:
                if item in {"-q", "-v", "-x", "--disable-warnings"}:
                    continue
                if item.startswith("-"):
                    raise GuardrailError("This pytest option is not allowed.")
                self.path(item)
            return ["python", "-m", "pytest", *remaining]
        if args[:2] in [["git", "status"], ["git", "diff"], ["git", "log"]]:
            if len(args) > 2 and args[2:] not in [["--short"], ["--stat"], ["--oneline"]]:
                raise GuardrailError("Only read-only Git inspection options are allowed.")
            return args
        raise GuardrailError("Command is not supported. The first stack is FastAPI with pytest.")

    @staticmethod
    def branch(value: str) -> str:
        if not re.fullmatch(r"user/[a-z0-9][a-z0-9-]{0,39}", value):
            raise GuardrailError("Only a safely named user/<username> branch is permitted.")
        return value
