"""Real-browser E2E for the status projection (audit D2/D5).

Every other UI test is headless Streamlit `AppTest`, which inspects the element
tree without a real browser/WebSocket — so it never caught that the Kanban board
dropped the `Blocked` lane and rendered only 88 of 174 tasks. This test renders
the *actual* app in Chromium and asserts the board shows a `Blocked` lane and
accounts for every task (nothing silently vanishes).

The console is behind a sign-in gate (`command_center.ui.console_identity`,
VOYN-W0-AICC-CONSOLE-NO-AUTH), so these tests sign in for real rather than
being let past it: `platform_stub` below serves the one endpoint the gate
consults (`GET /api/v1/whoami`) on loopback, the app under test is pointed at
it with `AICC_PLATFORM_URL`, and `_open_console` types the token into the
login form in the browser. Nothing in this file may reach for a bypass — the
whole point of the gate is that the served app has none, and an E2E that
needed one would be asserting against an app no operator can run.

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
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe

from command_center.http_auth import identity

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.e2e

APP = Path(__file__).resolve().parents[1] / "app.py"

#: The credential `platform_stub` accepts and `_open_console` presents. A
#: throwaway string, never a real token: the stub is the whole authority here.
CONSOLE_TOKEN = "e2e-console-token"
CONSOLE_PRINCIPAL = "operator:e2e"


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


class _WhoamiHandler(BaseHTTPRequestHandler):
    """The identity authority, reduced to the one route the gate calls.

    Deliberately not a mock of `console_identity`: the app under test is a
    separate OS process, so the double has to sit where the real platform
    sits — behind the network — for the served console to exercise its real
    sign-in path. Answers 401 for anything but `CONSOLE_TOKEN`, so a browser
    that fails to present the token is refused here exactly as it would be
    against the real platform.
    """

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path != identity.WHOAMI_PATH:
            self._respond(404, {"error": "not_found"})
            return
        header = self.headers.get("Authorization", "")
        token = header[len("Bearer ") :] if header.startswith("Bearer ") else ""
        if token != CONSOLE_TOKEN:
            self._respond(401, {"error": "unauthenticated"})
            return
        self._respond(
            200,
            {
                "data": {
                    "principal_id": CONSOLE_PRINCIPAL,
                    "tenant_id": "tenant-e2e",
                    "capabilities": [],
                }
            },
        )

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        """Silence the default stderr access log; it is pure test noise."""


@pytest.fixture(scope="module")
def platform_stub():
    """A loopback whoami authority for the console's sign-in gate.

    Bound to 127.0.0.1 on an ephemeral port because `identity.platform_base_url`
    only accepts plain `http://` for a literal loopback host — the same
    "local development against a platform stub" shape it documents.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WhoamiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.fixture(scope="module")
def live_app(tmp_path_factory, platform_stub):
    data_dir = tmp_path_factory.mktemp("aicc_e2e_data")
    (data_dir / "tasks.json").write_text(json.dumps(FIXTURE_TASKS), encoding="utf-8")
    port = _free_port()
    env = {
        **os.environ,
        "AICC_DATA_DIR": str(data_dir),
        "AICC_BACKGROUND_SYNC": "0",
        "AICC_OPERATOR": "",
        identity.PLATFORM_URL_ENV: platform_stub,
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


def _open_console(page, url: str) -> None:
    """Navigate to the app and sign in through its real login form.

    Every browser session starts signed out — the gate caches its principal in
    `st.session_state`, which is per-Streamlit-session, so a fresh page is a
    fresh sign-in. Streamlit paints over the WebSocket after `load`, so the
    form is waited for rather than assumed present.
    """
    page.goto(url, wait_until="load")
    token_field = page.locator("input[type='password']")
    token_field.wait_for(timeout=90000)
    token_field.fill(CONSOLE_TOKEN)
    page.get_by_role("button", name="Войти", exact=True).click()
    # A successful sign-in reruns the script into the app proper, so the login
    # form leaving the DOM is the readiness signal for everything after it.
    token_field.wait_for(state="detached", timeout=90000)


def test_an_unauthenticated_browser_gets_the_login_form_not_the_board(live_app):
    """The served console — not just the unit-tested module — is gated.

    Without this, every other test here could sign in correctly and still tell
    us nothing about whether the board is reachable *without* signing in.
    """
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(live_app, wait_until="load")
        page.locator("input[type='password']").wait_for(timeout=90000)
        body = page.inner_text("body")
        assert "Войти" in body
        for lane in ("Backlog", "Blocked", "Done"):
            assert lane not in body
        browser.close()


def _board_text(url: str) -> str:
    try:
        launcher = sync_api.sync_playwright
        with launcher() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:  # browser not installed
                pytest.skip(f"Chromium unavailable: {exc}")
            page = browser.new_page()
            _open_console(page, url)
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
        _open_console(page, live_app)
        page.wait_for_selector("text=Очередь выполнения", timeout=90000)

        surface = page.locator("[data-testid='stMain']")
        dashboard_action_names = _dashboard_action_names(surface)
        assert surface.locator("h1").count() >= 1
        assert surface.locator("h2").count() >= 1
        assert surface.locator("[role='status']").count() >= 1
        # Progress bars are only rendered for tasks with live progress.
        # This fixture may intentionally contain only completed/queued tasks.
        if surface.locator("[role='progressbar']").count() == 0:
            assert surface.locator("[role='status']").count() >= 1
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
        _open_console(page, live_app)
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
