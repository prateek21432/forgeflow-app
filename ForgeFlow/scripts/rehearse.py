"""Submit a real rehearsal request; poll saved events without making model calls."""

import json
import re
import sys
import time
from pathlib import Path

import httpx

root = Path(__file__).resolve().parent.parent
access = json.loads((root / ".runtime/presentation_access.json").read_text(encoding="utf-8"))
message = (
    sys.argv[1]
    if len(sys.argv) > 1
    else (
        "Build an e-commerce website with a product listing page, product details, cart, login and checkout. "
        "Make it a small bookstore named Juniper Books with clean HTML/CSS/JavaScript and a FastAPI backend. "
        "Include registration and hashed passwords, per-user cart quantity updates, a simulated checkout that "
        "creates a persistent order, visible error messages, and a responsive layout. No real payments or email integrations. "
        "Use SQLite and a small seeded catalogue. Keep the implementation compact, readable, and easy to demonstrate."
    )
)
with httpx.Client(
    base_url="http://127.0.0.1:8791", timeout=20, follow_redirects=True, trust_env=False
) as client:
    page = client.get("/login")
    csrf = re.search('name="csrf-token" content="([^"]+)"', page.text).group(1)
    page = client.post(
        "/login", data={**{k: access[k] for k in ("username", "password")}, "csrf_token": csrf}
    )
    csrf = re.search('name="csrf-token" content="([^"]+)"', page.text).group(1)
    base = "/projects/" + str(access["project_id"])
    response = client.post(
        base + "/chat", json={"message": message}, headers={"x-csrf-token": csrf}
    )
    response.raise_for_status()
    run_id = response.json()["run_id"]
    print("Started real model run", run_id, flush=True)
    cursor = 0
    deadline = time.monotonic() + 1900
    while time.monotonic() < deadline:
        for event in client.get(base + "/events", params={"after": cursor}).json():
            cursor = max(cursor, event["id"])
            if event["run_id"] == run_id:
                print(event["agent"], event["kind"], event["message"][:650], flush=True)
        state = client.get(base + "/api/state").json()
        run = next(r for r in state["runs"] if r["id"] == run_id)
        if not state["active_run_id"]:
            (root / "artifacts" / f"rehearsal-run-{run_id}.json").write_text(
                json.dumps(state, indent=2), encoding="utf-8"
            )
            print("FINAL:", run["status"], run["error"], flush=True)
            print("Demo:", state["demo_url"], flush=True)
            sys.exit(0 if run["status"] == "completed" else 1)
        time.sleep(3)
    raise SystemExit("Rehearsal polling reached its timeout. Check the dashboard.")
