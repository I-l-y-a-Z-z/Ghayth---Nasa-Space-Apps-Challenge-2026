"""Optional real-browser testing, isolated from the user's demo project."""

import os
from uuid import uuid4

import pytest


@pytest.mark.browser
def test_browser_planning_saved_plans_weather_and_mobile():
    url = os.getenv("AGRIX_BROWSER_TEST_URL")
    if not url:
        pytest.skip("Set AGRIX_BROWSER_TEST_URL to a running local demo API")
    playwright = pytest.importorskip("playwright.sync_api")
    project = "browser-check-" + uuid4().hex[:12]
    errors = []
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROME_EXECUTABLE", "/usr/bin/google-chrome"),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        # Exercise an independent project even though the UI's normal default is demo.
        page.route(
            "**/*",
            lambda route: route.continue_(
                headers={**route.request.headers, "x-project-id": project}
            ),
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url.rstrip("/") + "/testing", wait_until="networkidle")
        page.locator("#empty-seed-button").click()
        playwright.expect(page.locator(".plot-choice")).to_have_count(6)
        page.locator(".plot-choice").filter(has_text="balanced rainfed").click()
        playwright.expect(page.locator("#profile-status")).to_contain_text("Complete")

        page.locator("#suggest-button").click()
        playwright.expect(page.locator(".candidate-card")).to_have_count(2)
        playwright.expect(page.locator("#evaluation-status")).to_have_text("Fits these inputs")
        page.locator("#budget-input").fill("0")
        playwright.expect(page.locator("#save-plan-button")).to_be_disabled()
        page.locator("#suggest-button").click()
        playwright.expect(page.locator("#evaluation-status")).to_have_text("Doesn’t fit")
        playwright.expect(page.locator("#evaluation-reasons")).to_contain_text("budget")

        page.locator("#budget-input").fill("40000")
        page.locator("#evaluate-button").click()
        playwright.expect(page.locator("#evaluation-status")).to_have_text("Fits these inputs")
        plan_name = "Browser checked rotation " + uuid4().hex[:6]
        page.locator("#plan-name").fill(plan_name)
        page.locator("#save-plan-button").click()
        playwright.expect(page.locator("#toast")).to_contain_text("Plan saved")
        page.locator('.view-tab[data-view="saved"]').click()
        saved = page.locator(".saved-card").filter(has_text=plan_name)
        playwright.expect(saved).to_have_count(1)
        saved.get_by_role("button", name="Open plan").click()
        playwright.expect(page.locator("#plan-name")).to_have_value(plan_name)
        playwright.expect(page.locator("#budget-input")).to_have_value("40000.00")
        page.locator("#chat-locale").select_option("ary")
        page.locator("#chat-button").click()
        playwright.expect(page.locator("#chat-response")).to_contain_text("درهم")
        playwright.expect(page.locator("#chat-response")).to_have_attribute("dir", "rtl")

        page.locator('.view-tab[data-view="weather"]').click()
        playwright.expect(page.locator(".weather-sample")).to_have_count(3)
        playwright.expect(page.locator("#weather-samples svg")).to_have_count(3)
        power = page.locator(".weather-sample").filter(has_text="NASA POWER")
        power.locator("select").select_option("PRECTOTCORR")
        playwright.expect(power).to_contain_text("46.43 mm")
        page.screenshot(path="/tmp/agrix-weather-browser.png", full_page=True)

        page.locator('.view-tab[data-view="planner"]').click()
        page.locator(".plot-choice").filter(has_text="soil test to confirm").click()
        playwright.expect(page.locator("#profile-status")).to_contain_text("Partial")
        page.locator("#suggest-button").click()
        playwright.expect(page.locator("#evaluation-status")).to_have_text("Needs confirmation")
        page.locator("#save-draft-button").click()
        playwright.expect(page.locator("#toast")).to_contain_text("unevaluated draft")

        page.locator("#new-plot-button").click()
        page.locator("#new-plot-name").fill("Browser test field")
        page.locator("#new-plot-area").fill("1")
        page.locator("#create-plot-button").click()
        playwright.expect(page.locator("#plot-title")).to_have_text("Browser test field")
        playwright.expect(page.locator(".plot-choice")).to_have_count(7)
        assert not page.locator("#error-banner").is_visible(), page.locator(
            "#error-message"
        ).inner_text()

        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path="/tmp/agrix-mobile-browser.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert not errors, errors
        browser.close()
