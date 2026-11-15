"""Opt-in Chrome integration checks for the React workspace and real HTTP API.

Run against a disposable development server; all writes use a fresh project ID.
"""

import json
import os
from uuid import uuid4

import pytest


@pytest.mark.browser
def test_react_workspace_planning_languages_saved_snapshot_and_mobile():
    url = os.getenv("AGRIX_FULL_BROWSER_TEST_URL")
    if not url:
        pytest.skip("Set AGRIX_FULL_BROWSER_TEST_URL to the React workspace URL")
    playwright = pytest.importorskip("playwright.sync_api")
    project = "full-ui-check-" + uuid4().hex[:12]
    errors = []
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROME_EXECUTABLE", "/usr/bin/google-chrome"),
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--use-gl=angle",
                "--use-angle=swiftshader",
            ],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route(
            "**/*",
            lambda route: (
                route.continue_(headers={**route.request.headers, "x-project-id": project})
                if route.request.url.startswith(url.rstrip("/"))
                else route.continue_()
            ),
        )
        seeded = page.request.post(
            url.rstrip("/") + "/demo/seed", headers={"X-Project-ID": project}
        )
        assert seeded.ok, seeded.text()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#locale").select_option("en")
        playwright.expect(page.locator(".plot-button")).to_have_count(6)
        page.locator(".plot-button").filter(has_text="balanced rainfed").click()
        playwright.expect(page.locator(".profile-section .status")).to_have_text("Complete")
        with page.expect_response("**/rotations/suggest") as suggested:
            page.locator("#compare-rotations").click()
        original = suggested.value.json()["candidates"][0]
        playwright.expect(page.locator('[data-testid="candidate"]')).to_have_count(2)
        playwright.expect(page.locator('[data-testid="evaluation-detail"]')).to_be_visible()
        with page.expect_response("**/rotations/evaluate") as evaluated:
            page.locator("#evaluate-sequence").click()
        custom = evaluated.value.json()
        assert custom["financials"] == original["financials"]
        assert custom["climate_scenarios"] == original["climate_scenarios"]
        assert custom["status"] == original["status"]
        page.screenshot(path="/tmp/agrix-full-desktop-plan.png", full_page=True)

        # Edits survive locale changes and require explicit reevaluation.
        budget = page.locator("#farm-budget_mad")
        budget.fill("40000")
        playwright.expect(page.locator("#save-plan")).to_be_disabled()
        page.locator("#locale").select_option("ary")
        playwright.expect(page.locator("html")).to_have_attribute("dir", "rtl")
        playwright.expect(budget).to_have_value("40000")
        page.locator("#locale").select_option("fr")
        playwright.expect(budget).to_have_value("40000")
        page.locator("#locale").select_option("en")
        page.locator("#evaluate-sequence").click()
        playwright.expect(page.locator("#save-plan")).to_be_enabled()
        plan_name = "Full UI checked rotation " + uuid4().hex[:6]
        page.locator("#plan-name").fill(plan_name)
        page.locator("#save-plan").click()
        playwright.expect(
            page.locator('[role="status"]').filter(has_text="Saved with its original inputs")
        ).to_be_visible()

        # Opening a saved snapshot after changing plots must restore its inputs.
        page.locator(".plot-button").filter(has_text="soil test to confirm").click()
        page.get_by_role("button", name="Saved plans", exact=False).click()
        saved = page.locator(".saved-card").filter(has_text=plan_name)
        saved.get_by_role("button", name="Reopen").click()
        playwright.expect(page.locator("#plan-name")).to_have_value(plan_name)
        playwright.expect(budget).to_have_value("40000.00")
        # The agent is developed separately; this UI exposes only its reserved space.
        playwright.expect(page.locator('[data-testid="agent-placeholder"]')).to_be_visible()
        assert page.locator("#chat-input").count() == 0

        page.get_by_role("button", name="NASA samples", exact=True).click()
        playwright.expect(page.locator(".weather-card")).to_have_count(3)
        playwright.expect(page.locator(".weather-card svg")).to_have_count(3)
        power = page.locator(".weather-card").filter(has_text="NASA POWER")
        power.locator("select").select_option("PRECTOTCORR")
        playwright.expect(power.locator(".weather-total")).to_contain_text("46.43")

        # Creating a live plot works through GeoJSON when WebGL/tiles are unavailable.
        page.get_by_role("button", name="New plot", exact=False).click()
        page.locator("#plot-name").fill("Browser confirmed live boundary")
        canvas = page.locator(".maplibregl-canvas")
        if canvas.count():
            page.locator(".map-tools").get_by_role("button", name="Draw boundary").click()
            canvas.click(position={"x": 180, "y": 130})
            canvas.click(position={"x": 240, "y": 130})
            canvas.click(position={"x": 240, "y": 200})
            page.locator(".map-tools").get_by_role("button", name="Finish drawing").click()
            playwright.expect(page.locator("#confirm-plot")).to_be_enabled()
            playwright.expect(page.locator(".vertex-marker")).to_have_count(3)
            area_before_drag = page.locator(".map-caption strong").inner_text()
            vertex = page.locator(".vertex-marker").first.bounding_box()
            assert vertex
            page.mouse.move(vertex["x"] + vertex["width"] / 2, vertex["y"] + vertex["height"] / 2)
            page.mouse.down()
            page.mouse.move(vertex["x"] + 35, vertex["y"] + 30, steps=8)
            page.mouse.up()
            playwright.expect(page.locator(".map-caption strong")).not_to_have_text(
                area_before_drag
            )
            page.locator(".map-tools").get_by_role("button", name="Undo").click()
            playwright.expect(page.locator(".map-caption strong")).to_have_text(area_before_drag)
        page.locator(".manual-boundary summary").click()
        polygon = {
            "type": "Polygon",
            "coordinates": [
                [
                    [-7.611, 32.99],
                    [-7.61, 32.99],
                    [-7.61, 32.991],
                    [-7.611, 32.991],
                    [-7.611, 32.99],
                ]
            ],
        }
        page.get_by_role("textbox", name="GeoJSON").fill(json.dumps(polygon))
        page.get_by_role("button", name="Apply boundary", exact=True).click()
        page.get_by_role("combobox", name="Farming system", exact=True).select_option("rainfed")
        page.locator("#confirm-plot").click()
        playwright.expect(page.locator(".plot-button")).to_have_count(7)
        playwright.expect(page.locator(".plot-button.selected")).to_contain_text(
            "Browser confirmed live boundary"
        )

        # A missing live layer remains visible and can be retried from the UI.
        playwright.expect(page.locator("#retry-analysis")).to_be_visible(timeout=20000)
        with page.expect_response("**/retry") as retried:
            page.locator("#retry-analysis").click()
        assert retried.value.ok
        playwright.expect(page.locator("#retry-analysis")).to_be_visible(timeout=20000)
        with page.expect_response("**/rotations/suggest") as live_suggestions:
            page.locator("#compare-rotations").click()
        live = live_suggestions.value.json()["candidates"]
        assert live
        assert all(candidate["synthetic"] is False for candidate in live)
        assert all(
            candidate["input_snapshot"]["catalog"]["synthetic"] is False for candidate in live
        )
        assert all(
            candidate["financials"]["whole_plot"]["expected_revenue"]["value"] is None
            for candidate in live
        )
        playwright.expect(page.locator(".demo-banner")).to_have_count(0)
        playwright.expect(page.locator('[data-testid="evaluation-detail"]')).to_be_visible()
        playwright.expect(page.locator('[data-testid="environmental-assessment"]')).to_be_visible()
        assert all(
            candidate["environmental"]["version"] == "environmental-screening-2.0.0"
            for candidate in live
        )
        ranking = [candidate["environmental"]["ranking_score"] for candidate in live]
        assert ranking == sorted(ranking, reverse=True)
        page.set_viewport_size({"width": 390, "height": 844})
        for locale in ("en", "fr", "ary"):
            page.locator("#locale").select_option(locale)
            page.screenshot(path=f"/tmp/agrix-full-mobile-{locale}.png", full_page=False)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert not errors, errors
        browser.close()
