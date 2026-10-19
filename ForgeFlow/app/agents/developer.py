"""A bounded model/tool loop. Existing files must be read before modification."""

import json
from itertools import count
from pathlib import Path

from ..config import settings
from ..database import Run, SessionLocal, ToolCall
from ..schemas import DeveloperResponse
from ..security import redact
from ..services.events import bus
from ..services.git_service import GitService
from ..services.llm_gateway import ModelBudgetError, gateway
from ..tools.runtime import WorkspaceTools, make_snapshot, remove_snapshot, run_in_snapshot
from .supervisor import STACK_RULES


class RunCancelled(RuntimeError):
    pass


def check_cancelled(run_id: int) -> None:
    with SessionLocal() as db:
        row = db.get(Run, run_id)
        if row and row.cancel_requested:
            raise RunCancelled("Stopped at the user's request.")


class DeveloperAgent:
    async def implement(
        self,
        project_id: int,
        run_id: int,
        workspace: Path,
        request: str,
        plan: dict,
        feedback: dict,
    ) -> str:
        files = WorkspaceTools(workspace)
        tools = files.langchain_tools()
        inspected = set()
        known_files: dict[str, str] = {}
        observations = []
        seen_steps = set()
        definitions = [
            {"name": t.name, "description": t.description, "arguments": t.args}
            for t in tools.values()
        ]
        definitions.append(
            {"name": "run_command", "arguments": {"command": ["python", "-m", "pytest", "-q"]}}
        )
        system = (
            "You are the Developer. Use the supplied tools to create or edit an actual application. "
            "Return a DeveloperResponse JSON with a short progress summary, actions, and finished. "
            "For actions use {tool: name, arguments: object}. Use at most 12 actions per response. "
            + (
                f"Your entire JSON response must fit within {settings.llm_max_tokens} output tokens. "
                if settings.llm_max_tokens != -1
                else "Keep your JSON response within the provider's output limit. "
            )
            + "Leave room for JSON escaping and closing brackets. Write only one or two small complete files "
            "per response when the output allowance is small; use finished=false to continue. "
            "Never squeeze the whole application into a response that may be truncated. "
            "Read an existing file before writing, editing, or deleting it. Keep existing architecture and features. "
            "Work incrementally and add meaningful tests. Use readable code, descriptive names, short functions, "
            "and brief comments explaining non-obvious decisions. No placeholders, fake buttons, or TODO features. "
            "Use write_file for complete new files, edit_file for exact single replacements. "
            "Inspect read results before constructing edits; do not guess unseen file contents. "
            "known_files contains the latest content after your successful operations. "
            "If an exact edit fails, read the file again and use its current text, or carefully write the complete corrected file. "
            "When feedback includes failed tests, diagnose and repair the implementation; never weaken acceptance tests. "
            "Set finished=true only when your requested edits are complete; the independent Tester will verify them. "
            "Your scope is implementation only. The platform Tester owns independent acceptance tests; "
            "the Supervisor owns preview startup; the Git service owns checkpoints and pushes. "
            "You do not need those tools. Once implementation is ready, set finished=true and hand off, "
            "even when testing, preview, or checkpoint work remains. Never rerun an unchanged passing "
            "command to wait for another agent. If blocked on a platform capability, report it once and hand off. "
            + STACK_RULES
            + "\nTools: "
            + json.dumps(definitions)
        )
        for step in count():
            if settings.max_agent_steps != -1 and step >= settings.max_agent_steps:
                break
            check_cancelled(run_id)
            response = await gateway.generate_structured(
                system,
                json.dumps(
                    {
                        "request": request,
                        "plan": plan,
                        "feedback": feedback,
                        "file_list": files.list_files(),
                        "observations": observations[-24:],
                        "known_files": known_files,
                        "steps_remaining": None
                        if settings.max_agent_steps == -1
                        else settings.max_agent_steps - step,
                    }
                ),
                DeveloperResponse,
            )
            await bus.emit(project_id, "Developer", "agent_update", response.summary, run_id)
            check_cancelled(run_id)
            step_key = json.dumps(
                {"source": files.digest(), "actions": [a.model_dump() for a in response.actions]},
                sort_keys=True,
            )
            if not response.actions or step_key in seen_steps:
                await bus.emit(
                    project_id,
                    "Developer",
                    "agent_update",
                    "No new implementation action. Handing saved work to the Tester.",
                    run_id,
                )
                return response.summary
            seen_steps.add(step_key)
            step_succeeded = True
            for action in response.actions:
                check_cancelled(run_id)
                arguments = action.arguments
                success = True
                try:
                    path = arguments.get("path", "")
                    if action.tool in {"write_file", "edit_file", "delete_file"}:
                        target = files.guard.path(path, write=True)
                        if target.exists() and path not in inspected:
                            raise ValueError("Read this existing file before modifying it.")
                    if action.tool == "run_command":
                        command = files.guard.command(arguments.get("command", []))
                        await bus.emit(
                            project_id, "Developer", "command_started", " ".join(command), run_id
                        )
                        if command[0] == "git":
                            git_arguments = command[1:]
                            if command[1] == "diff":
                                git_arguments = [
                                    "diff",
                                    "--no-ext-diff",
                                    "--no-textconv",
                                    *command[2:],
                                ]
                            result = await GitService().command(
                                workspace, git_arguments, allow_failure=True
                            )
                        else:
                            snapshot = make_snapshot(workspace)
                            try:
                                result = await run_in_snapshot(
                                    snapshot, command, settings.command_timeout
                                )
                            finally:
                                remove_snapshot(snapshot)
                        output = json.dumps(result.to_dict())
                        success = result.exit_code == 0
                        await bus.emit(
                            project_id,
                            "Developer",
                            "command_finished",
                            f"Command finished with exit code {result.exit_code}.",
                            run_id,
                            {"command": command, "output": result.stdout + "\n" + result.stderr},
                        )
                    else:
                        output = str(tools[action.tool].invoke(arguments))
                        if action.tool in {"read_file", "write_file"}:
                            inspected.add(path)
                        if action.tool in {"read_file", "write_file", "edit_file"}:
                            known_files.pop(path, None)
                            known_files[path] = files.read_file(path)
                            while len(json.dumps(known_files)) > 55000 and len(known_files) > 1:
                                known_files.pop(next(iter(known_files)))
                        if action.tool == "delete_file":
                            known_files.pop(path, None)
                        if action.tool in {"write_file", "edit_file", "delete_file"}:
                            await bus.emit(project_id, "Developer", "file_modified", output, run_id)
                except Exception as exc:
                    output = f"{type(exc).__name__}: {exc}"
                    success = False
                output = redact(output)
                safe_arguments = dict(arguments)
                # Keep file content in Git/diffs rather than duplicating it in tool logs.
                for key in ("content", "old_text", "new_text"):
                    if key in safe_arguments:
                        safe_arguments[key] = f"[{len(str(safe_arguments[key]))} characters]"
                with SessionLocal() as db:
                    db.add(
                        ToolCall(
                            run_id=run_id,
                            agent="Developer",
                            name=action.tool,
                            arguments=safe_arguments,
                            result=output[:20000],
                            success=success,
                        )
                    )
                    db.commit()
                observations.append(
                    {
                        "tool": action.tool,
                        "arguments": safe_arguments,
                        "result": "Current content is in known_files."
                        if action.tool == "read_file" and success and path in known_files
                        else output[:18000],
                    }
                )
                if not success:
                    step_succeeded = False
                    message = (
                        "Command failed; inspect its terminal output."
                        if action.tool == "run_command"
                        else output
                    )
                    await bus.emit(project_id, "Developer", "tool_error", message, run_id)
            if response.finished and step_succeeded:
                return response.summary
        raise ModelBudgetError(
            "Developer step allowance reached. Files are saved; use Resume saved work to continue."
        )
