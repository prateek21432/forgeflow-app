"""Planning and requirement review are model work; execution remains deterministic."""

import json
from pathlib import Path

from ..schemas import Plan, ValidationDecision
from ..services.llm_gateway import gateway
from ..tools.runtime import WorkspaceTools

STACK_RULES = """
The supported generated stack is Python FastAPI with backend/main.py exporting app,
HTML/CSS/vanilla JavaScript in frontend/, and pytest API tests in tests/.
Use only Python standard library, FastAPI, Pydantic, Jinja2, SQLAlchemy, itsdangerous,
python-multipart, httpx, and pytest. Serve the frontend through the same FastAPI app.
Use relative portable paths. Store demo data in SQLite, not global shared lists.
Keep small applications compact: a few cohesive backend modules, not dozens of tiny files.
For tiny single-page apps, prefer backend/main.py, frontend/index.html with embedded CSS/JS,
and one test module. Split modules only when complexity warrants it; do not add unnecessary features.
Use hashlib.pbkdf2_hmac or scrypt for password hashes; bcrypt/passlib are not supported.
Use itsdangerous.URLSafeTimedSerializer for sessions. Never hard-code a session secret.
Use a random process secret or an environment variable without exposing it in source.
Use the current Jinja2 call style: templates.TemplateResponse(request=request, name='index.html', context={...}).
Do not use EmailStr (email-validator is unavailable); validate ordinary string fields.
Authentication needs hashed passwords and per-user sessions; no real payment processing.
A checkout is a local simulated order, clearly labelled. Never claim a real payment occurred.
Preserve existing features and tests. Support unrelated web apps rather than fixed shop templates.
Requests requiring unsupported stacks or real third-party credentials must be reported honestly.
"""


class SupervisorAgent:
    async def plan(self, workspace: Path, request: str, history: list[dict]) -> Plan:
        return await gateway.generate_structured(
            "You are the planning supervisor for a local development platform. "
            + STACK_RULES
            + "Inspect supplied existing code before proposing changes. Assess likely affected files and regression risks. "
            "The risk assessment is a reasoned estimate, not a proven dependency graph. "
            "Write concrete observable acceptance criteria, including visible UI behavior. "
            "Do not include routine server startup or Git operations as product acceptance criteria.",
            json.dumps(
                {
                    "request": request,
                    "recent_history": history,
                    "project": WorkspaceTools(workspace).context(),
                }
            ),
            Plan,
        )

    async def validate(
        self, request: str, plan: dict, changes: list[dict], results: dict, workspace: Path
    ) -> ValidationDecision:
        return await gateway.generate_structured(
            "Review whether a web feature fulfills the user's request. Tests alone are insufficient. "
            "Inspect source, actual changes, and test evidence. Reject missing visible controls, fake authentication, "
            "silenced tests, and unsupported requests marked done. Be specific about missing requirements. "
            "This is the IMPLEMENTATION review before the platform creates a Git checkpoint. "
            "Checkpoint creation and push are scheduled AFTER approval: never require their IDs as "
            "a condition of approval, and never delegate them to the Developer. "
            "tests.platform_evidence contains freshly measured preview and storage results. "
            "tests.platform_evidence.browser_interactions contains actual Playwright scenario results. "
            "Use these to resolve browser requirements left unverified by pytest; do not discard this "
            "evidence merely because Developer tools cannot access it. Unlisted interactions and stated "
            "limitations are still unverified. "
            "The supplied independent test results are authoritative execution evidence from the platform, "
            "even though Developer tools cannot access those private suites. "
            "Distinguish unavailable browser evidence from a demonstrated code defect. "
            "Review against the user's request and the agreed plan; do not introduce new mandatory acceptance "
            "criteria at final review. Optional polish or no-JavaScript support is not a blocker unless requested "
            "or explicitly promised in the plan. Source inspection can support implementation claims, while "
            "only executed browser assertions establish browser verification. Do not demand extra proof for "
            "every CSS declaration when the requested behavior and planned acceptance checks already pass. "
            "Never claim browser actions were tested when they were not. Report such limitations explicitly. "
            "If rejecting solely for unavailable platform/browser capabilities, set requires_code_changes=false "
            "so the workflow reports a blocker instead of repeatedly requesting impossible code fixes. "
            "tests.unverified_requirements lists checks NOT performed. Assess them against the request: "
            "required but unavailable capabilities are a blocker; explicitly optional/conditional checks "
            "may remain unverified but must be disclosed in your summary. " + STACK_RULES,
            json.dumps(
                {
                    "request": request,
                    "plan": plan,
                    "changes": changes,
                    "tests": results,
                    "source": WorkspaceTools(workspace).context(),
                }
            ),
            ValidationDecision,
        )
