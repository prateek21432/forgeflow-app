"""Check the configured provider with one small structured request."""

import asyncio
import sys
from pathlib import Path

from pydantic import BaseModel


class ConnectionCheck(BaseModel):
    ok: bool
    message: str


async def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.config import settings
    from app.services.llm_gateway import begin_budget, budget_usage, gateway

    settings.llm_max_tokens = 512
    settings.llm_retries = 1
    settings.max_model_calls_per_run = 1
    settings.llm_timeout = 45
    begin_budget()
    print(f"Checking {settings.llm_provider}: {settings.llm_model}", flush=True)
    result = await gateway.generate_structured(
        "Reply with JSON only.",
        'Return {"ok":true,"message":"Connection verified"}.',
        ConnectionCheck,
    )
    if not result.ok:
        raise RuntimeError("The provider did not confirm the check.")
    print("PASS: authenticated completion and structured response received.")
    print("Usage:", budget_usage())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(f"FAIL: {type(error).__name__}: {error}")
        raise SystemExit(1)
