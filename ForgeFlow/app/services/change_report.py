"""Compare real source snapshots; keep predictions distinct from observations."""

import difflib
from pathlib import Path

from ..tools.runtime import WorkspaceTools


def capture_source(workspace: Path) -> dict[str, str]:
    tools = WorkspaceTools(workspace)
    result = {}
    for name in tools.list_files():
        try:
            result[name] = tools.read_file(name)
        except UnicodeError:
            continue
    return result


def compare_sources(before: dict[str, str], after: dict[str, str]) -> list[dict]:
    changes = []
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name, ""), after.get(name, "")
        if old == new:
            continue
        lines = list(
            difflib.unified_diff(
                old.splitlines(),
                new.splitlines(),
                fromfile="before/" + name,
                tofile="after/" + name,
                lineterm="",
            )
        )
        changes.append(
            {
                "path": name,
                "kind": "added"
                if name not in before
                else "deleted"
                if name not in after
                else "modified",
                "added": sum(line.startswith("+") and not line.startswith("+++") for line in lines),
                "removed": sum(
                    line.startswith("-") and not line.startswith("---") for line in lines
                ),
                "diff": "\n".join(lines)[:40000],
            }
        )
    return changes
