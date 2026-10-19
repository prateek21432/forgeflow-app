"""Browser smoke check and creation of a private local presentation account."""

import json
import secrets
from pathlib import Path

from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parent.parent
artifacts = root / "artifacts"
artifacts.mkdir(exist_ok=True)
access_path = root / ".runtime/presentation_access.json"
if access_path.exists():
    access = json.loads(access_path.read_text(encoding="utf-8"))
else:
    access = {"username": "arpit", "password": secrets.token_urlsafe(18)}
    access_path.write_text(json.dumps(access), encoding="utf-8")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1050}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8791/login")
    page.screenshot(path=str(artifacts / "login.png"), full_page=True)
    if not access.get("project_id"):
        page.get_by_role("tab", name="Create account").click()
        page.locator("#new-username").fill(access["username"])
        page.locator("#new-password").fill(access["password"])
        page.get_by_role("button", name="Create workspace").click()
    if "/dashboard" not in page.url:
        page.get_by_role("tab", name="Sign in", exact=True).click()
        page.locator("#username").fill(access["username"])
        page.locator("#password").fill(access["password"])
        page.get_by_role("button", name="Open workspace").click()
    page.wait_for_url("**/dashboard")
    page.screenshot(path=str(artifacts / "dashboard.png"), full_page=True)
    if access.get("project_url"):
        page.goto(access["project_url"])
    else:
        page.locator("#project-name").fill("Juniper Bookshop")
        page.locator("#new-project button[type=submit]").click()
    page.wait_for_url("**/projects/*")
    page.wait_for_timeout(1200)
    page.screenshot(path=str(artifacts / "project.png"), full_page=True)
    access["project_url"] = page.url
    access["project_id"] = int(page.url.rsplit("/", 1)[1])
    access_path.write_text(json.dumps(access), encoding="utf-8")
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(artifacts / "project-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "Mobile horizontal overflow"
    )
    assert not errors, errors
    print("Browser checks passed; project:", access["project_id"])
    browser.close()
