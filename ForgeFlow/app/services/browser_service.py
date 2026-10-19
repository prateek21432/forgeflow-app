"""Plan UI scenarios centrally, then execute them through a controlled worker."""

import hashlib
import json
import os
import sys
from pathlib import Path

from ..config import ROOT, settings
from ..schemas import BrowserSuite
from ..tools.runtime import WorkspaceTools, child_environment, execute
from .llm_gateway import gateway


class BrowserService:
    async def verify(
        self, run_id: int, workspace: Path, request: str, plan: dict, url: str
    ) -> dict:
        directory = settings.runtime_root / "checks" / str(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        tools = WorkspaceTools(workspace)
        digest = tools.digest()
        key = hashlib.sha256(
            json.dumps({"browser_schema": 2, "source": digest, "plan": plan}, sort_keys=True).encode()
        ).hexdigest()
        cache = directory / f"browser-plan-{key}.json"
        if cache.exists():
            suite = BrowserSuite.model_validate_json(cache.read_text(encoding="utf-8"))
        else:
            suite = await gateway.generate_structured(
                "Write concise browser interaction scenarios for the supplied application and requirements. "
                "Use ONLY the declarative BrowserStep actions in the schema; no Python or JavaScript code. "
                "Each scenario starts on a fresh root page. Inspect source for correct CSS selectors. "
                "fill enters a string, click clicks, press sends a key, focus focuses; text checks substring; "
                "attribute checks exact attribute value; focused checks keyboard focus; same_url checks no navigation. "
                "Use viewport and no_overflow for a narrow screen. Use explicit visible/text/hidden assertions "
                "and in_viewport to assert at least half of an element is inside the viewport (visible alone does not). "
                "Use scrolled with minimum_scroll after anchor navigation. css checks a computed CSS property: "
                "attribute is the property name (e.g. fill, outline-style, animation-duration), value is its exact "
                "computed value (colors usually rgb). Check rendered SVG fills, not only CSS variable attributes. "
                "Each scenario supports javascript_enabled=false and reduced_motion='reduce' before page load. "
                "Use these only when relevant to planned requirements; avoid adding unrelated acceptance conditions. "
                "after interactions; a click alone is not proof. Cover success, repeated use, invalid inputs clearing "
                "stale results, and keyboard access when relevant. Do not put nonnumeric text into type=number "
                "using fill; use empty/zero/negative cases and leave API-only cases to pytest. "
                "Do not test checkpoint/Git operations. Report unsupported checks (such as uploads/audio) in "
                "limitations; never replace actual interaction assertions with source inspection. "
                "External network and downloads are blocked. Keep scenarios small and avoid duplicates.",
                json.dumps({"request": request, "plan": plan, "source": tools.context()}),
                BrowserSuite,
            )
            cache.write_text(suite.model_dump_json(), encoding="utf-8")
        return await self.run(run_id, url, suite, digest)

    async def run(self, run_id: int, url: str, suite: BrowserSuite, digest: dict) -> dict:
        directory = settings.runtime_root / "checks" / str(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        request_file, report_file = (
            directory / "browser-request.json",
            directory / "browser-result.json",
        )
        request_file.write_text(
            json.dumps(
                {"url": url, "suite": suite.model_dump(), "channel": settings.browser_channel}
            ),
            encoding="utf-8",
        )
        report_file.unlink(missing_ok=True)
        env = child_environment()
        # Playwright needs these locations to find an installed browser on Windows.
        # Keep provider credentials and the platform environment out of its process.
        for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA", "PLAYWRIGHT_BROWSERS_PATH"):
            if name in os.environ:
                env[name] = os.environ[name]
        result = await execute(
            [sys.executable, "-m", "app.tools.browser_worker", str(request_file), str(report_file)],
            ROOT,
            settings.browser_timeout,
            env=env,
        )
        if result.timed_out or result.exit_code or not report_file.exists():
            report = {
                "status": "UNAVAILABLE",
                "cases": [],
                "error": "Browser worker timed out or failed to start. " + result.stderr[-1500:],
            }
        else:
            report = json.loads(report_file.read_text(encoding="utf-8"))
        report["source_digest"] = digest
        report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report


browser_service = BrowserService()
