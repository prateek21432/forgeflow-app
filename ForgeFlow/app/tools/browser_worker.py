"""Execute declarative UI checks in a fresh browser; never execute model code."""

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from ..schemas import BrowserSuite


def run_checks(url: str, suite: BrowserSuite, channel: str) -> dict:
    target = urlsplit(url)
    if target.scheme != "http" or target.hostname != "127.0.0.1" or not target.port:
        raise ValueError("Browser checks require a loopback preview URL.")
    origin = (target.scheme, target.netloc)
    cases = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=channel or None, headless=True)
        try:
            for scenario in suite.scenarios:
                context = browser.new_context(
                    service_workers="block", accept_downloads=False,
                    java_script_enabled=scenario.javascript_enabled,
                    reduced_motion=scenario.reduced_motion,
                )

                # Generated pages cannot fetch the control app or unrelated hosts.
                def restrict(route):
                    parsed = urlsplit(route.request.url)
                    if (parsed.scheme, parsed.netloc) == origin:
                        route.continue_()
                    else:
                        route.abort()

                context.route("**/*", restrict)
                context.route_web_socket("**/*", lambda socket: socket.close())
                page = context.new_page()
                context.on("page", lambda popup: popup.close() if popup != page else None)
                page.set_default_timeout(4000)
                page.set_default_navigation_timeout(10000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                case = {
                    "name": scenario.name,
                    "status": "PASS",
                    "reason": "",
                    "steps": [step.model_dump(exclude_defaults=True) for step in scenario.steps],
                    "completed_steps": 0,
                    "javascript_enabled": scenario.javascript_enabled,
                    "reduced_motion": scenario.reduced_motion,
                }
                try:
                    response = page.goto(url)
                    if not response or response.status != 200:
                        raise AssertionError("Preview page did not return HTTP 200.")
                    initial_url = page.url
                    for step in scenario.steps:
                        locator = page.locator(step.selector) if step.selector else None
                        if step.action == "fill":
                            locator.fill(step.value)
                        elif step.action == "click":
                            locator.click()
                        elif step.action == "press":
                            locator.press(step.value)
                        elif step.action == "focus":
                            locator.focus()
                        elif step.action == "visible":
                            expect(locator).to_be_visible(timeout=4000)
                        elif step.action == "hidden":
                            expect(locator).to_be_hidden(timeout=4000)
                        elif step.action == "text":
                            expect(locator).to_contain_text(step.value, timeout=4000)
                        elif step.action == "attribute":
                            expect(locator).to_have_attribute(
                                step.attribute, step.value, timeout=4000
                            )
                        elif step.action == "focused":
                            expect(locator).to_be_focused(timeout=4000)
                        elif step.action == "in_viewport":
                            expect(locator).to_be_in_viewport(ratio=0.5, timeout=4000)
                        elif step.action == "css":
                            expect(locator).to_have_css(step.attribute, step.value, timeout=4000)
                        elif step.action == "scrolled":
                            # Page timers may be disabled in no-JavaScript scenarios.
                            deadline = time.monotonic() + 4
                            while page.evaluate("window.scrollY") < step.minimum_scroll:
                                if time.monotonic() >= deadline:
                                    raise AssertionError(f"Page did not scroll at least {step.minimum_scroll}px.")
                                page.wait_for_timeout(100)
                        elif step.action == "same_url":
                            assert page.url == initial_url, "Form navigated away from the preview."
                        elif step.action == "viewport":
                            page.set_viewport_size({"width": step.width, "height": step.height})
                        elif step.action == "no_overflow":
                            assert page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth"
                            ), "Horizontal overflow."
                        case["completed_steps"] += 1
                    assert not errors, "Browser errors: " + "; ".join(errors)
                except Exception as exc:
                    case.update(status="FAIL", reason=str(exc)[:3000])
                finally:
                    context.close()
                cases.append(case)
        finally:
            browser.close()
    return {
        "status": "FAIL"
        if any(c["status"] == "FAIL" for c in cases)
        else "PASS"
        if cases
        else "UNVERIFIED",
        "cases": cases,
        "limitations": suite.limitations,
        "method": "Playwright browser interactions",
    }


if __name__ == "__main__":
    request_file, report_file = map(Path, sys.argv[1:3])
    try:
        request = json.loads(request_file.read_text(encoding="utf-8"))
        result = run_checks(
            request["url"], BrowserSuite.model_validate(request["suite"]), request["channel"]
        )
    except Exception as exc:
        result = {"status": "UNAVAILABLE", "cases": [], "error": str(exc)[:3000]}
    report_file.write_text(json.dumps(result), encoding="utf-8")
