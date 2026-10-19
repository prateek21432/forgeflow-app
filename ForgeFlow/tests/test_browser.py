"""Real browser assertions, failure evidence, network restriction, and plan reuse."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import ValidationError

from app.schemas import BrowserScenario, BrowserStep, BrowserSuite
from app.services.browser_service import browser_service
from app.services.llm_gateway import gateway
from app.tools.browser_worker import run_checks


@pytest.fixture
def preview():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"""<html><meta name="viewport" content="width=device-width">
            <style>
            html { scroll-behavior: smooth; }
            #value:focus { outline: 3px solid rgb(255, 0, 0); }
            #art { fill: rgb(0, 0, 255); animation: pulse 2s infinite; }
            @keyframes pulse { from { opacity: .9; } to { opacity: 1; } }
            @media (prefers-reduced-motion: reduce) { #art { animation-duration: 0s; } }
            #details { margin-top: 1800px; }
            </style>
            <input id="value" aria-label="Value"><button id="calculate">Calculate</button>
            <svg width="80" height="80"><rect id="art" width="80" height="80"/></svg>
            <a href="#details" id="explore">Explore</a>
            <output id="result"></output><p id="network"></p>
            <script>
            document.querySelector('#calculate').onclick = () => {
              document.querySelector('#result').textContent = document.querySelector('#value').value;
              fetch('http://127.0.0.1:1/private').catch(() => {
                document.querySelector('#network').textContent = 'Blocked';
              });
            };
            </script><section id="details">Details</section></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


def scenario(expected="22.5"):
    return BrowserScenario(
        name="Visible keyboard calculation",
        steps=[
            BrowserStep(action="viewport", width=390),
            BrowserStep(action="fill", selector="#value", value="22.5"),
            BrowserStep(action="press", selector="#value", value="Tab"),
            BrowserStep(action="focused", selector="#calculate"),
            BrowserStep(action="press", selector="#calculate", value="Enter"),
            BrowserStep(action="text", selector="#result", value=expected),
            BrowserStep(action="text", selector="#network", value="Blocked"),
            BrowserStep(action="same_url"),
            BrowserStep(action="no_overflow"),
        ],
    )


@pytest.mark.asyncio
async def test_real_browser_reports_assertion_failure(preview):
    report = await browser_service.run(
        101,
        preview,
        BrowserSuite(scenarios=[scenario(), scenario("wrong result")]),
        {"main.py": "digest"},
    )
    assert report["status"] == "FAIL", report
    assert [case["status"] for case in report["cases"]] == ["PASS", "FAIL"]
    assert "wrong result" in report["cases"][1]["reason"]
    assert report["source_digest"] == {"main.py": "digest"}


@pytest.mark.parametrize(
    "url", ["https://example.com", "file:///etc/passwd", "http://localhost:8791"]
)
def test_non_preview_urls_rejected(url):
    with pytest.raises(ValueError, match="loopback"):
        run_checks(url, BrowserSuite(), "msedge")


def test_browser_schema_rejects_code_and_empty_checks():
    with pytest.raises(ValidationError):
        BrowserStep(action="evaluate", value="arbitrary code")
    with pytest.raises(ValidationError):
        BrowserStep(action="fill", value="no selector")
    with pytest.raises(ValidationError):
        BrowserScenario(name="No evidence", steps=[BrowserStep(action="click", selector="button")])


@pytest.mark.asyncio
async def test_visual_checks_without_javascript_and_with_reduced_motion(preview):
    suite = BrowserSuite(scenarios=[
        BrowserScenario(name="Actual rendering", javascript_enabled=False, reduced_motion="reduce", steps=[
            BrowserStep(action="in_viewport", selector="#explore"),
            BrowserStep(action="css", selector="#art", attribute="fill", value="rgb(0, 0, 255)"),
            BrowserStep(action="css", selector="#art", attribute="animation-duration", value="0s"),
            BrowserStep(action="focus", selector="#value"),
            BrowserStep(action="css", selector="#value", attribute="outline-style", value="solid"),
            BrowserStep(action="click", selector="#explore"),
            BrowserStep(action="scrolled", minimum_scroll=500),
            BrowserStep(action="in_viewport", selector="#details"),
        ]),
        BrowserScenario(name="Offscreen must fail", steps=[BrowserStep(action="in_viewport", selector="#details")]),
        BrowserScenario(name="Incorrect color must fail", steps=[BrowserStep(action="css", selector="#art", attribute="fill", value="rgb(255, 0, 0)")]),
    ])
    report = await browser_service.run(103, preview, suite, {})
    assert [case["status"] for case in report["cases"]] == ["PASS", "FAIL", "FAIL"], report
    assert report["cases"][0]["javascript_enabled"] is False


@pytest.mark.asyncio
async def test_browser_plan_reuse_requires_same_source(tmp_path, monkeypatch):
    from app.config import settings

    workspace = settings.workspace_root / "browser-cache"
    workspace.mkdir()
    (workspace / "index.html").write_text("<input>")
    calls = []

    async def model(*args):
        calls.append(args)
        return BrowserSuite(scenarios=[scenario()])

    async def runner(*args):
        return {"status": "PASS"}

    monkeypatch.setattr(gateway, "generate_structured", model)
    monkeypatch.setattr(browser_service, "run", runner)
    for _ in range(2):
        await browser_service.verify(102, workspace, "Calculate", {}, "http://127.0.0.1:1234")
    assert len(calls) == 1
    (workspace / "index.html").write_text("<input><output>")
    await browser_service.verify(102, workspace, "Calculate", {}, "http://127.0.0.1:1234")
    assert len(calls) == 2
