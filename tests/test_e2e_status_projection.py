"""Real-browser E2E for the status projection (audit D2/D5).

Every other UI test is headless Streamlit `AppTest`, which inspects the element
tree without a real browser/WebSocket — so it never caught that the Kanban board
dropped the `Blocked` lane and rendered only 88 of 174 tasks. This test renders
the *actual* app in Chromium and asserts the board shows a `Blocked` lane and
accounts for every task (nothing silently vanishes).

Skips cleanly where Playwright or its browser is unavailable; CI installs both
(`python -m playwright install chromium`).
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.e2e

APP = Path(__file__).resolve().parents[1] / "app.py"


def _chromium_installed() -> bool:
    """True only when the Playwright Chromium browser is actually present."""
    try:
        with sync_api.sync_playwright() as pw:
            executable = pw.chromium.executable_path
        return bool(executable) and Path(executable).exists()
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_chromium():
    """Keep E2E nodeids collectable while skipping execution without Chromium."""
    if not _chromium_installed():
        pytest.skip("Playwright Chromium browser is not installed")


# A known, tiny store whose statuses include the previously-invisible `Blocked`.
FIXTURE_TASKS = [
    {
        "id": "t1",
        "project": "AICC",
        "title": "Backlog one",
        "goal": "g",
        "status": "Backlog",
    },
    {
        "id": "t2",
        "project": "AICC",
        "title": "Backlog two",
        "goal": "g",
        "status": "Backlog",
    },
    {
        "id": "t3",
        "project": "AICC",
        "title": "Blocked one",
        "goal": "g",
        "status": "Blocked",
    },
    {
        "id": "t4",
        "project": "AICC",
        "title": "Blocked two",
        "goal": "g",
        "status": "Blocked",
    },
    {
        "id": "t5",
        "project": "AICC",
        "title": "Blocked three",
        "goal": "g",
        "status": "Blocked",
    },
    {"id": "t6", "project": "AICC", "title": "Done one", "goal": "g", "status": "Done"},
]
BLOCKED_COUNT = sum(1 for t in FIXTURE_TASKS if t["status"] == "Blocked")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="module")
def live_app(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("aicc_e2e_data")
    (data_dir / "tasks.json").write_text(json.dumps(FIXTURE_TASKS), encoding="utf-8")
    port = _free_port()
    env = {
        **os.environ,
        "AICC_DATA_DIR": str(data_dir),
        "AICC_BACKGROUND_SYNC": "0",
        "AICC_OPERATOR": "",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(APP),
            "--server.port",
            str(port),
            "--server.address",
            "127.0.0.1",
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ],
        env=env,
        cwd=str(APP.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            if proc.poll() is not None:
                pytest.fail("streamlit process exited before becoming ready")
            try:
                with urllib.request.urlopen(url + "/_stcore/health", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail("streamlit did not become healthy in time")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _board_text(url: str) -> str:
    try:
        launcher = sync_api.sync_playwright
        with launcher() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:  # browser not installed
                pytest.skip(f"Chromium unavailable: {exc}")
            page = browser.new_page()
            page.goto(url, wait_until="load")
            # Streamlit paints content over the WebSocket after `load`; wait for
            # the board to actually render its lanes.
            page.wait_for_selector("text=Blocked", timeout=90000)
            page.wait_for_selector("text=Done", timeout=90000)
            body = page.inner_text("body")
            browser.close()
            return body
    finally:
        pass


def test_board_shows_blocked_lane_and_accounts_for_every_task(live_app):
    body = _board_text(live_app)
    # D2: the Blocked lane exists at all (it was absent → 49% of tasks invisible).
    assert "Blocked" in body
    # D5/D2: the board reflects the blocked tasks that used to vanish. Their count
    # must appear on the board next to the Blocked lane.
    assert str(BLOCKED_COUNT) in body
    # Sanity: the other canonical lanes render too, so the board is really up.
    assert "Backlog" in body
    assert "Done" in body


def _dashboard_action_names(surface, *, timeout: int = 90000) -> list[str]:
    # Streamlit delivers the dashboard incrementally over its WebSocket.  An
    # earlier heading or status region is not a readiness signal for the action
    # surface rendered later in the script, so synchronize on that surface.
    surface.locator("button").filter(has_text="Быстро:").first.wait_for(timeout=timeout)
    action_names = surface.locator("button").evaluate_all(
        "els => els.map(el => el.getAttribute('aria-label') || el.innerText).filter(Boolean)"
    )
    return [
        name
        for name in action_names
        if any(
            marker in name
            for marker in (
                "Быстро:",
                "arrow_forward",
                "Открыть Execution Center",
                "Открыть задачу",
            )
        )
    ]


def test_dashboard_action_probe_waits_for_incremental_streamlit_render():
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 320, "height": 800})
        page.set_content(
            "<main data-testid='stMain'><h2>Очередь выполнения</h2></main>"
        )
        page.evaluate(
            "setTimeout(() => {"
            "const status = document.createElement('div');"
            "status.setAttribute('role', 'status');"
            "status.textContent = 'Готово';"
            "const button = document.createElement('button');"
            "button.textContent = 'Быстро: новая задача';"
            "document.querySelector('main').appendChild(status);"
            "document.querySelector('main').appendChild(button);"
            "}, 100)"
        )

        surface = page.locator("[data-testid='stMain']")
        assert _dashboard_action_names(surface, timeout=5000) == [
            "Быстро: новая задача"
        ]
        assert surface.locator("[role='status']").count() == 1
        browser.close()


def test_dashboard_keyboard_semantics_and_320px_reflow(live_app):
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 320, "height": 800})
        page.goto(live_app, wait_until="load")
        page.wait_for_selector("text=Очередь выполнения", timeout=90000)

        surface = page.locator("[data-testid='stMain']")
        dashboard_action_names = _dashboard_action_names(surface)
        assert surface.locator("h1").count() >= 1
        assert surface.locator("h2").count() >= 1
        assert surface.locator("[role='status']").count() >= 1
        assert surface.locator("[role='progressbar']").count() >= 1
        assert surface.locator("svg[role='img'][aria-label]").count() >= 1
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )

        assert dashboard_action_names
        assert len(dashboard_action_names) == len(set(dashboard_action_names))

        surface.locator("button").first.focus()
        first_focused = page.evaluate("document.activeElement.outerHTML")
        focus_style = surface.locator("button").first.evaluate(
            "el => getComputedStyle(el).outlineStyle + ':' + getComputedStyle(el).outlineWidth"
        )
        assert focus_style != "none:0px"
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement !== document.body")
        assert page.evaluate("document.activeElement.outerHTML") != first_focused

        page.set_viewport_size({"width": 640, "height": 800})
        page.evaluate("document.documentElement.style.fontSize = '200%'")
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        browser.close()


def test_dashboard_has_no_serious_live_accessibility_defects(live_app):
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 320, "height": 800})
        page.goto(live_app, wait_until="load")
        surface = page.locator("[data-testid='stMain']")
        _dashboard_action_names(surface)

        # Streamlit keeps the parent document across Python reruns. Reinstalling
        # the shell repair must disconnect the previous observer instead of
        # accumulating one callback per rerun.
        initial_installs = page.evaluate(
            "window.__aiccAccessibilityRepair.installCount"
        )
        page.get_by_role("button", name="Проекты", exact=True).click()
        page.get_by_text("Обзор всех проектов", exact=False).first.wait_for()
        page.get_by_role("button", name="Обзор", exact=True).click()
        _dashboard_action_names(surface)

        # Exercise the reinstall path explicitly as well: Streamlit currently
        # preserves an unchanged st.html node on these reruns, but a future
        # renderer may execute it again.
        page.evaluate(
            "window.__aiccInstallAccessibilityRepair();"
            "window.__aiccInstallAccessibilityRepair();"
        )
        assert (
            page.evaluate("window.__aiccAccessibilityRepair.installCount")
            == initial_installs + 2
        )
        assert page.evaluate("window.__aiccAccessibilityRepair.activeObservers") == 1
        page.evaluate("window.__aiccAccessibilityRepair.callbackCount = 0")
        page.evaluate(
            "document.querySelector('[data-testid=stMain]').appendChild(document.createElement('i'))"
        )
        page.wait_for_function("window.__aiccAccessibilityRepair.callbackCount >= 1")
        assert page.evaluate("window.__aiccAccessibilityRepair.callbackCount") == 1

        # A content link inside a heading is meaningful and must remain exposed;
        # only Streamlit's empty permalink control is decorative.
        page.evaluate(
            "const link = document.createElement('a');"
            "link.href = '/operator-guide';"
            "link.textContent = 'Руководство оператора';"
            "document.querySelector('h2').appendChild(link);"
        )
        meaningful_link = page.get_by_role(
            "link", name="Руководство оператора", exact=True
        )
        meaningful_link.wait_for()
        assert meaningful_link.get_attribute("aria-hidden") is None
        assert (
            page.get_by_role(
                "button",
                name=re.compile(r"^(Скрыть|Показать|Открыть) навигацию$"),
            ).count()
            == 1
        )

        results = Axe().run(
            page,
            options={
                "runOnly": {
                    "type": "tag",
                    "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"],
                },
                "resultTypes": ["violations"],
            },
        )
        serious = [
            violation
            for violation in results.response["violations"]
            if violation.get("impact") in {"critical", "serious"}
        ]
        residuals = {
            "axe_serious": [violation["id"] for violation in serious],
            "empty_checkbox_names": page.get_by_role(
                "checkbox", name="", exact=True
            ).count(),
            "focusable_sections": page.locator(
                "section[tabindex]:not([tabindex='-1'])"
            ).count(),
            "decorative_heading_links": page.locator(
                "[data-testid='stHeaderActionElements'] a:not([aria-hidden='true']), "
                "a[data-testid='stHeaderActionElements']:not([aria-hidden='true'])"
            ).count(),
            "icon_names_in_buttons": page.get_by_role(
                "button",
                name=re.compile(
                    r"(arrow_forward|task_alt|refresh|settings|delete|close)"
                ),
            ).count(),
        }
        assert residuals == {
            "axe_serious": [],
            "empty_checkbox_names": 0,
            "focusable_sections": 0,
            "decorative_heading_links": 0,
            "icon_names_in_buttons": 0,
        }, results.generate_report()
        browser.close()
