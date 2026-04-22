"""
browser_agent.py — Project Sane v1.5

Architecture:
  • Chrome is launched via subprocess (NOT via Playwright's launch_persistent_context).
    This is critical: Playwright's launcher forces --use-mock-keychain which breaks
    Google OAuth token decryption on macOS and causes Chrome to exit immediately.
  • Playwright then attaches to the running Chrome via connect_over_cdp().
  • The ChromeProjectSane profile is used so your Odoo Google session is preserved.
"""

import asyncio
import base64
import os
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.async_api import async_playwright

# ── Constants ─────────────────────────────────────────────────────────────────
# Agent uses a separate data dir (ChromeAgentWork) but with session cookies
# copied from Profile 3 (shsri@odoo.com). Chrome requires a non-default
# user-data-dir to allow --remote-debugging-port.
SOURCE_COOKIES = str(
    Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Profile 3" / "Cookies"
)
AGENT_PROFILE = str(
    Path.home() / "Library" / "Application Support" / "Google" / "ChromeAgentWork"
)
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT = 9225
SUPPORT_LOGIN_REASON = "testing"


class BrowserAgent:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self._playwright = None
        self._cdp_browser = None   # Playwright browser object (CDP-connected)
        self.context = None
        self.page = None
        self.screenshots = []

    # ─────────────────────────────────────────────────────────────────────────
    # _is_port_open — utility
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _is_port_open(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("localhost", port)) == 0

    # ─────────────────────────────────────────────────────────────────────────
    # start() — idempotent
    #   First call  : kills stale processes, launches Chrome subprocess,
    #                 waits for CDP port, connects Playwright.
    #   Subsequent  : opens a new tab in the existing live context.
    # ─────────────────────────────────────────────────────────────────────────
    async def start(self):
        # Already initialised in this server process — just open a fresh tab
        if self.context is not None:
            self.page = await self.context.new_page()
            await self.page.bring_to_front()
            return

        # ── Only launch Chrome if it's not already running on our port ────
        if self._is_port_open(CDP_PORT):
            print(f"[Browser] Chrome already on port {CDP_PORT} — reusing existing window.")
        else:
            # Sync the latest session cookies from Profile 3 before launching
            import shutil
            agent_default = os.path.join(AGENT_PROFILE, "Default")
            os.makedirs(agent_default, exist_ok=True)
            if os.path.exists(SOURCE_COOKIES):
                shutil.copy2(SOURCE_COOKIES, os.path.join(agent_default, "Cookies"))
                print(f"[Browser] Session cookies synced from Profile 3.")

            print(f"[Browser] Launching Chrome with shsri@odoo.com session...")
            cmd = [
                CHROME_PATH,
                f"--remote-debugging-port={CDP_PORT}",
                f"--user-data-dir={AGENT_PROFILE}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "http://localhost:8000",
            ]
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Wait for the CDP port to be ready
            for _ in range(40):
                if self._is_port_open(CDP_PORT):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(f"Chrome did not open CDP port {CDP_PORT} in time.")
            time.sleep(1)   # Allow pages to register

        # ── Connect Playwright to the running Chrome via CDP ──────────────
        self._playwright = await async_playwright().start()
        self._cdp_browser = await self._playwright.chromium.connect_over_cdp(
            f"http://localhost:{CDP_PORT}"
        )
        print("[Browser] Connected to Chrome via CDP.")

        self.context = self._cdp_browser.contexts[0]

        # Always open a fresh tab for this ticket (never reuse existing tabs)
        self.page = await self.context.new_page()
        await self.page.bring_to_front()
        print(f"[Browser] New tab opened. Ready.")



    # ─────────────────────────────────────────────────────────────────────────
    # stop() — intentional no-op
    # The browser window stays open for manual review after each ticket.
    # ─────────────────────────────────────────────────────────────────────────
    async def stop(self):
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # screenshot helper
    # ─────────────────────────────────────────────────────────────────────────
    async def screenshot(self, label: str, output_dir: str = "output") -> str:
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%H%M%S")
        filepath = os.path.join(output_dir, f"{label}_{timestamp}.png")
        if self.page:
            try:
                await self.page.screenshot(path=filepath, timeout=8000)
                self.screenshots.append(filepath)
            except Exception as e:
                print(f"[Screenshot] Skipping '{label}': {e}")
        return filepath


    # ─────────────────────────────────────────────────────────────────────────
    # investigate_duplicate_db — main flow
    # ─────────────────────────────────────────────────────────────────────────
    async def investigate_duplicate_db(
        self,
        url: str,
        ticket_info: dict,
        request_approval_callback=None,
        stream_callback=None,
    ) -> str:
        findings = []
        version_detected = "not detected from UI"

        try:
            # ── STEP 1: Build /_odoo/support URL ─────────────────────────
            if not url.startswith("http://") and not url.startswith("https://"):
                url = "https://" + url
            parsed_url = urlparse(url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
            support_url = f"{base_url}/_odoo/support"
            print(f"[STEP 1] → {support_url}")
            await self.page.goto(support_url)
            await self.page.wait_for_load_state("load")

            # ── STEP 2: Auto-handle Support Login screen ──────────────────
            # URL: <domain>/_odoo/support/login
            # Fill "Login Reason / Ticket ID" field → click submit
            if "/support/login" in self.page.url.lower():
                print(f"[STEP 2] Support login — auto-filling '{SUPPORT_LOGIN_REASON}'")
                try:
                    await self.page.wait_for_selector("input", timeout=8_000)

                    # Fill the first text input on the page
                    inputs = await self.page.query_selector_all(
                        "input[type='text'], input:not([type='hidden']):not([type='submit']):not([type='button'])"
                    )
                    if inputs:
                        await inputs[0].fill(SUPPORT_LOGIN_REASON)
                    else:
                        await self.page.locator("input").first.fill(SUPPORT_LOGIN_REASON)

                    await self.page.wait_for_timeout(400)

                    # Click the submit / login button
                    btn = await self.page.query_selector(
                        "button[type='submit'], input[type='submit'], "
                        "button:has-text('Login'), a:has-text('Login')"
                    )
                    if btn:
                        await btn.click()
                    else:
                        await self.page.locator("button, input[type='submit']").first.click()

                    print("[STEP 2] Clicked submit — waiting for redirect...")
                    await self.page.wait_for_url(
                        lambda u: "/support/login" not in u.lower(),
                        timeout=30_000
                    )
                    await self.page.wait_for_load_state("load")
                    print(f"[STEP 2] Login done → {self.page.url}")
                    findings.append(f"Auto-logged in to support gateway (reason: '{SUPPORT_LOGIN_REASON}')")
                except Exception as e:
                    print(f"[STEP 2] Warning: {e}")
                    findings.append(f"Auto-login warning: {e}")

            await self.screenshot("01_support_gateway")
            findings.append(f"Support gateway: {support_url}")

            # ── STEP 3: Create or navigate to duplicate database ──────────
            # Flow:
            #   a) Look for EXISTING duplicate DBs on the support page first
            #   b) If found → click the most recent one
            #   c) If not found → click "Duplicate" button and wait for creation
            #   d) After landing on the duplicate's domain, navigate into it
            print("[STEP 3] Checking support page for duplicate databases...")
            await self.page.wait_for_timeout(2000)

            async def _enter_duplicate():
                """Navigate into the duplicate DB from the support page.
                Returns True if we successfully entered a duplicate."""
                # Check for existing duplicates — links containing 'support-' in href or text
                dup_links = await self.page.query_selector_all(
                    "a[href*='support-'], a[href*='-support-']"
                )
                if dup_links:
                    print(f"[STEP 3] Found {len(dup_links)} existing duplicate(s). Using the first.")
                    link_text = await dup_links[0].inner_text()
                    link_href = await dup_links[0].get_attribute("href")
                    print(f"[STEP 3] → '{link_text}' ({link_href})")
                    await dup_links[0].click()
                    await self.page.wait_for_load_state("load")
                    await self.page.wait_for_timeout(3000)
                    findings.append(f"Navigated to existing duplicate: {link_text}")
                    return True

                # No existing duplicate — try to create one
                dup_btn = await self.page.query_selector(
                    "button:has-text('Duplicate'), input[value='Duplicate'], "
                    "a:has-text('Duplicate'), button:has-text('duplicate')"
                )
                if dup_btn:
                    print("[STEP 3] No duplicate found. Clicking Duplicate button...")
                    await dup_btn.click()
                    findings.append("Clicked Duplicate button — waiting for creation...")

                    # Poll for the new duplicate link to appear (up to 60 s)
                    for i in range(30):
                        await self.page.wait_for_timeout(2000)
                        new_dups = await self.page.query_selector_all(
                            "a[href*='support-'], a[href*='-support-']"
                        )
                        if new_dups:
                            link_text = await new_dups[0].inner_text()
                            link_href = await new_dups[0].get_attribute("href")
                            print(f"[STEP 3] Duplicate created: '{link_text}' ({link_href})")
                            await new_dups[0].click()
                            await self.page.wait_for_load_state("load")
                            await self.page.wait_for_timeout(3000)
                            findings.append(f"Created and navigated to new duplicate: {link_text}")
                            return True
                    print("[STEP 3] WARNING: Duplicate creation timed out.")
                    findings.append("WARNING: Duplicate creation timed out after 60s.")
                    return False
                else:
                    print("[STEP 3] WARNING: No Duplicate button found on support page.")
                    findings.append("WARNING: No Duplicate button found.")
                    return False

            entered_dup = await _enter_duplicate()

            # After clicking into the duplicate, we may land on that domain's support page
            # or directly on /web/login. Handle the support login for the duplicate too.
            if "/support/login" in self.page.url.lower():
                print("[STEP 3] Duplicate support login — auto-filling...")
                try:
                    await self.page.wait_for_selector("input", timeout=8_000)
                    inputs = await self.page.query_selector_all(
                        "input[type='text'], input:not([type='hidden']):not([type='submit'])"
                    )
                    if inputs:
                        await inputs[0].fill(SUPPORT_LOGIN_REASON)
                    await self.page.wait_for_timeout(300)
                    btn = await self.page.query_selector(
                        "button[type='submit'], input[type='submit'], button:has-text('Login')"
                    )
                    if btn:
                        await btn.click()
                    await self.page.wait_for_url(
                        lambda u: "/support/login" not in u.lower(), timeout=30_000
                    )
                    await self.page.wait_for_load_state("load")
                    print(f"[STEP 3] Duplicate login done → {self.page.url}")
                except Exception as e:
                    print(f"[STEP 3] Duplicate login warning: {e}")

            # If we're now on the DUPLICATE's /_odoo/support page, click into the DB
            if "/_odoo/support" in self.page.url and "/support/login" not in self.page.url:
                print("[STEP 3] On duplicate support page — clicking into database...")
                await self.page.wait_for_timeout(1500)
                db_link = None
                for sel in [
                    "p:has-text('Current database') a",
                    "div:has-text('Current database') a",
                    "h2 + p a", "h2 ~ p a",
                    "a[href*='/web'], a[href*='/odoo']",
                ]:
                    try:
                        el = await self.page.query_selector(sel)
                        if el:
                            db_link = el
                            break
                    except Exception:
                        continue

                if db_link:
                    link_href = await db_link.get_attribute("href")
                    print(f"[STEP 3] Entering duplicate DB → {link_href}")
                    await db_link.click()
                    await self.page.wait_for_load_state("load")
                    await self.page.wait_for_timeout(3000)
                else:
                    print("[STEP 3] No DB entry link found on duplicate support page.")

            # Re-bind to latest active tab if a new tab opened
            if self.page.is_closed():
                await asyncio.sleep(2)
                live = [p for p in self.context.pages if not p.is_closed()]
                if live:
                    self.page = live[-1]

            parsed_current = urlparse(self.page.url)
            base_url = f"{parsed_current.scheme}://{parsed_current.netloc}"
            print(f"[STEP 3] Inside DB at: {self.page.url}")
            findings.append(f"Inside database: {self.page.url}")

            await self.screenshot("02_db_dashboard")



            # ── STEP 4: Detect Odoo version ───────────────────────────────
            print("[STEP 4] Detecting version...")
            try:
                el = await self.page.query_selector("span.o_menu_brand, .o_version, footer span")
                if el:
                    version_detected = await el.inner_text()
            except Exception:
                pass
            findings.append(f"Odoo version: {version_detected}")

            # ── STEP 5: Check installed modules ───────────────────────────
            print("[STEP 5] Apps/Modules page...")
            try:
                await self.page.goto(f"{base_url}/odoo/apps/modules")
                await self.page.wait_for_timeout(2500)
                await self.screenshot("03_apps_modules")
                findings.append("Apps > Modules loaded. See screenshots.")
            except Exception as e:
                findings.append(f"Could not load Apps page: {e}")

            # ── STEP 6: AI Visual Investigation (HITL-gated) ──────────────
            if ticket_info.get("steps_to_reproduce") or ticket_info.get("summary"):
                from ai_agent import AIAgent
                import traceback

                print("[STEP 6] Capturing screenshot for Vision Model...")
                screenshot_bytes = await self.page.screenshot(full_page=True)
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

                ai = AIAgent(
                    groq_api_key=os.getenv("GROQ_API_KEY", ""),
                    gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
                )
                print("[STEP 6] Generating AI investigation script...")
                script_code = await ai.generate_playwright_execution_async(
                    ticket_info, self.page.url, stream_callback,
                    screenshot_b64=screenshot_b64
                )
                findings.append(f"Generated script (pending approval):\n{script_code}")

                approved = False
                if request_approval_callback:
                    approved = await request_approval_callback(script_code)
                else:
                    print("[STEP 6] No approval callback — dry-run mode.")

                if approved:
                    print("[STEP 6] Approved — executing...")
                    try:
                        wrapper = "async def __ai_exec(self, asyncio):\n"
                        for line in script_code.splitlines():
                            wrapper += f"    {line}\n"
                        local_ctx: dict = {}
                        exec(wrapper, globals(), local_ctx)
                        await local_ctx["__ai_exec"](self, asyncio)
                        await self.screenshot("04_ai_execution")
                        findings.append("Dynamic investigation completed.")
                    except Exception as e:
                        findings.append(f"Execution error: {e}\n{traceback.format_exc()}")
                else:
                    findings.append("AI script skipped by analyst.")

        except Exception as e:
            import traceback
            findings.append(f"Fatal error: {e}\n{traceback.format_exc()}")
            print(f"[INVESTIGATION] Fatal: {e}")

        # ── Build report ──────────────────────────────────────────────────
        report = (
            "=== DUPLICATE DB INVESTIGATION ===\n"
            f"URL: {url}\n"
            f"Version: {version_detected}\n\n"
            f"Screenshots: {', '.join(self.screenshots)}\n\n"
            "Observations:\n"
        )
        for f in findings:
            report += f"- {f}\n"
        return report

    # ─────────────────────────────────────────────────────────────────────────
    # test_on_runbot
    # ─────────────────────────────────────────────────────────────────────────
    async def test_on_runbot(self, odoo_version: str) -> str:
        if not odoo_version:
            return "No version provided for Runbot testing."
        try:
            await self.page.goto("https://runbot.odoo.com")
            await self.screenshot("runbot_01_home")
            found = False
            for selector in [
                f"a:has-text('{odoo_version}')",
                f"tr:has-text('{odoo_version}') a:has-text('Enterprise')",
                f"[href*='{odoo_version}']",
            ]:
                el = await self.page.query_selector(selector)
                if el:
                    await el.click()
                    await self.page.wait_for_load_state("load")
                    found = True
                    break
            if found:
                shot = await self.screenshot("runbot_02_version")
                return (
                    "=== RUNBOT ===\n"
                    f"Version: {odoo_version}\n"
                    f"URL: {self.page.url}\n"
                    f"Screenshot: {shot}\n"
                )
            return f"=== RUNBOT ===\nVersion {odoo_version} not found on Runbot."
        except Exception as e:
            return f"=== RUNBOT ===\nError: {e}"
