"""Real dashboard smoke test with an isolated DB, authenticated API, and fake seats.

Run directly after `python -m playwright install chromium`. No real agents or
external evidence providers are contacted. Artifacts go to output/playwright.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn
from playwright.sync_api import sync_playwright, expect


def main() -> None:
    output = ROOT / "output" / "playwright"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="conclave-browser-") as scratch:
        root = Path(scratch)
        token = "isolated-browser-test-token"
        os.environ["SWITCHBOARD_DATA_DIR"] = str(root)
        os.environ["CONCLAVE_API_TOKEN"] = token
        config_file = root / "config.yaml"
        config_file.write_text("openrouter:\n  enabled: false\nretention:\n  enabled: false\norchestration:\n  worker_poll_interval_seconds: 1\n", encoding="utf-8")
        os.environ["SWITCHBOARD_CONFIG"] = str(config_file)
        from app.agents.fake_adapter import FakeAdapter
        from app.services import agent_registry, evidence
        from app.database import init_database
        init_database(root / "switchboard.db")
        from app.main import app

        class BrowserFake(FakeAdapter):
            internal = False

            def __init__(self, name):
                super().__init__()
                self.name = name

            async def run_primary(self, ctx):
                if "[slow]" in ctx.task.user_request:
                    await asyncio.sleep(60)
                ctx.task.context.extra["fake_behavior"] = (
                    "ask_then_resolve" if "[pause]" in ctx.task.user_request else "resolve_immediately"
                )
                return await super().run_primary(ctx)

        def register_fakes(config):
            agent_registry.register(BrowserFake("review-one"))
            agent_registry.register(BrowserFake("review-two"))

        async def fetch_fixture(url, config):
            text = "Frozen browser fixture evidence. " * 600
            return {"url": url, "title": "Browser source", "publisher": "example.com",
                    "content_text": text, "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "quality": {"truncated": False}, "metadata": {}}

        agent_registry.init_registry = register_fakes
        evidence.fetch_url = fetch_fixture
        # Bind once and pass the socket to uvicorn to avoid a free-port race.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{port}"

        def request(path, payload=None):
            req = urllib.request.Request(url + path, headers={"X-Conclave-Token": token, "Content-Type": "application/json"},
                                         data=json.dumps(payload).encode() if payload is not None else None)
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)

        try:
            deadline = time.monotonic() + 20
            while not server.started and time.monotonic() < deadline:
                time.sleep(.05)
            assert server.started, "test service failed to start"
            project = request("/api/projects", {"name": "Browser project", "instructions": "Preserve dissent"})
            pid = project["id"]
            request("/api/evidence/fetch", {"project_id": pid, "urls": ["https://example.com/source"]})
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.add_init_script("window.showSaveFilePicker = undefined")
                page.set_default_timeout(15000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                # A configured token is required even for health. Enter it through the UI.
                expect(page.locator("#connection-settings")).to_have_attribute("open", "")
                page.locator("#connection-token").fill(token)
                page.get_by_role("button", name="Connect", exact=True).click()
                expect(page.locator("#connection-settings [role=status]")).to_have_text("Connected.")
                page.locator("#decision-project").select_option(pid)
                evidence_box = page.locator("#project-evidence-list input[type=checkbox]")
                expect(evidence_box).to_have_count(1)
                evidence_box.check()
                page.locator("#edit-decision-project").click()
                page.locator("#project-edit-description").fill("Edited through the browser")
                page.get_by_role("button", name="Save project", exact=True).click()
                expect(page.locator("#project-editor [role=status]")).to_have_text("Project saved.")
                assert request(f"/api/projects/{pid}")["project"]["description"] == "Edited through the browser"

                def submit(question):
                    page.locator("#mode").select_option("resolve")
                    page.locator('#agents-list input[value="review-one"]').check()
                    page.locator('#agents-list input[value="review-two"]').uncheck()
                    page.locator("#question").fill(question)
                    page.locator("#submit-btn").click()
                    expect(page.locator("#detail-id-code, .detail-id-code")).to_be_visible()

                submit("Browser decision")
                expect(page.locator("#task-feedback")).to_be_visible(timeout=20000)
                tid = page.locator(".detail-id-code").inner_text()
                expect(page.locator(".detail-usage")).not_to_contain_text("0 input")
                initial_runs = request(f"/api/tasks/{tid}")["compute_summary"]["agent_runs"]
                page.get_by_label("Did it change or refine your decision?").select_option("true")
                page.get_by_role("button", name="Save feedback", exact=True).click()
                expect(page.locator("#task-feedback [role=status]")).to_have_text("Feedback saved.")
                detail = request(f"/api/tasks/{tid}")
                assert detail["compute_summary"]["agent_runs"] == initial_runs
                assert detail["feedback"]["decision_changed"] is True
                assert detail["task"]["context"]["extra"]["evidence_excerpts"][0]["truncated"]
                page.locator("#download-format").select_option("md")
                with page.expect_download() as download:
                    page.locator("#download-detail-btn").click()
                download.value.save_as(output / "decision.md")
                page.screenshot(path=str(output / "decision-feedback.png"), full_page=True)
                page.locator("#followup-btn").click()
                expect(page.locator("#decision-project")).to_have_value(pid)
                expect(page.locator("#project-evidence-list input[type=checkbox]")).to_be_checked()

                submit("[pause] Clarification fixture")
                expect(page.locator("#answer-text")).to_be_visible(timeout=20000)
                page.locator("#answer-text").fill("Choose the simpler option")
                page.locator('#answer-form button[type="submit"]').click()
                expect(page.locator("#task-feedback")).to_be_visible(timeout=20000)
                page.locator("#start-new-btn").click()
                submit("[slow] Cancellation fixture")
                slow_id = page.locator(".detail-id-code").inner_text()
                deadline = time.monotonic() + 10
                while not request(f"/api/tasks/{slow_id}")["agent_runs"] and time.monotonic() < deadline:
                    time.sleep(.1)
                assert request(f"/api/tasks/{slow_id}")["agent_runs"], "slow fake never started"
                page.once("dialog", lambda dialog: dialog.accept())
                page.locator("#cancel-btn").click()
                expect(page.locator("#retry-task")).to_be_visible(timeout=15000)
                page.screenshot(path=str(output / "cancelled-task.png"), full_page=True)
                stopped = request(f"/api/tasks/{slow_id}")
                assert stopped["task"]["status"] == "cancelled"
                assert stopped["agent_runs"][0]["status"] == "cancelled"
                page.locator("#retry-task").click()
                expect(page.locator(".detail-id-code")).not_to_have_text(slow_id)
                retried_id = page.locator(".detail-id-code").inner_text()
                assert request(f"/api/tasks/{retried_id}")["task"]["parent_task_id"] == slow_id
                request(f"/api/tasks/{retried_id}/cancel", {})
                page.locator("#rail-usage").click()
                expect(page.locator("#value-metrics")).to_be_visible()
                expect(page.locator("#value-metrics tbody tr")).to_have_count(3)
                expect(page.locator("#value-metrics")).to_contain_text("Unknown")
                page.screenshot(path=str(output / "decision-value-metrics.png"), full_page=True)
                assert not errors, errors
                browser.close()
            print("Browser smoke passed: authentication, projects, evidence, submission, feedback, download, follow-up, clarification, cancellation.")
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            sock.close()
            assert not thread.is_alive(), "test service failed to stop"


if __name__ == "__main__":
    main()
