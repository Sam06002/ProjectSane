"""
browser_agent.py — Playwright CDP session management for Project Sane v3.

Provides context-level isolation per run under a centrally managed Chrome process.
"""

import asyncio
import os
import random
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Tuple, List, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from exceptions import BrowserError

# ── Chrome session constants ───────────────────────────────────────────────────
SOURCE_COOKIES = str(
    Path.home() / "Library" / "Application Support"
    / "Google" / "Chrome" / "Profile 3" / "Cookies"
)
AGENT_PROFILE = str(
    Path.home() / "Library" / "Application Support" / "Google" / "ChromeAgentWork"
)
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT = 9225

_STALE_CDP_ERRORS = (
    "Browser context management is not supported",
    "Target closed",
    "Connection closed",
    "WebSocket error",
    "websocket",
    "code=1000",
)


# ── Human-Like Mouse Interaction ─────────────────────────────────────────────────────────

async def human_like_click(page: Page, selector: str, timeout: int = 10000) -> None:
    """
    Locates an Odoo UI element, smoothly moves the virtual cursor to its
    coordinate target, and performs a natural click.
    """
    element = await page.wait_for_selector(selector, state="visible", timeout=timeout)
    box = await element.bounding_box()
    if not box:
        raise ValueError(f"Target UI element '{selector}' has no bounding box (not clickable).")

    target_x = box["x"] + box["width"] / 2 + random.randint(-2, 2)
    target_y = box["y"] + box["height"] / 2 + random.randint(-2, 2)
    current_x, current_y = 100, 100

    steps = 15
    for i in range(steps):
        t = i / float(steps)
        move_x = current_x + (target_x - current_x) * t
        move_y = current_y + (target_y - current_y) * t
        await page.mouse.move(move_x, move_y)
        await asyncio.sleep(0.02)

    await page.mouse.click(target_x, target_y)
    await asyncio.sleep(0.7)


class BrowserRunContext:
    """Run-specific container for Playwright BrowserContext, Page, and screenshots."""
    def __init__(self, context: BrowserContext, page: Page):
        self.context: BrowserContext = context
        self.page: Page = page
        self.screenshots: List[str] = []

    async def close(self) -> None:
        """Closes the page and browser context cleanly."""
        try:
            if not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        try:
            await self.context.close()
        except Exception:
            pass


class BrowserManager:
    """Manages the single central Chrome process and yields isolated contexts per run."""
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None  # fallback default page for backward compatibility

    def _is_port_open(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def _kill_port(self, port: int) -> None:
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True
            )
            pids = result.stdout.strip().split()
            for pid in pids:
                if pid:
                    subprocess.run(["kill", "-9", pid], capture_output=True)
                    print(f"[Browser] Killed stale process PID {pid} on port {port}.")
        except Exception as e:
            print(f"[Browser] Warning: could not kill port {port}: {e}")

    def _launch_chrome(self) -> None:
        print(f"[Browser] Launching Chrome on CDP port {CDP_PORT}...")
        agent_default = os.path.join(AGENT_PROFILE, "Default")
        os.makedirs(agent_default, exist_ok=True)
        
        if os.path.exists(SOURCE_COOKIES):
            shutil.copy2(SOURCE_COOKIES, os.path.join(agent_default, "Cookies"))
            print("[Browser] Cookies synced from Profile 3 to Default.")
            
            agent_p1 = os.path.join(AGENT_PROFILE, "Profile 1")
            os.makedirs(agent_p1, exist_ok=True)
            shutil.copy2(SOURCE_COOKIES, os.path.join(agent_p1, "Cookies"))
            print("[Browser] Cookies synced from Profile 3 to Profile 1.")

        cmd = [
            CHROME_PATH,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={AGENT_PROFILE}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "about:blank",
        ]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def _wait_for_port(self, timeout_s: float = 8.0) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if self._is_port_open(CDP_PORT):
                return True
            await asyncio.sleep(0.15)
        return False

    async def _reset_handles(self) -> None:
        self.page = None
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    async def _connect(self) -> None:
        if not self.playwright:
            self.playwright = await async_playwright().start()
        print(f"[Browser] Connecting to CDP on port {CDP_PORT}...")
        self.browser = await self.playwright.chromium.connect_over_cdp(
            f"http://localhost:{CDP_PORT}"
        )

    async def ensure_connected(self) -> None:
        """Ensures Chrome process is running and connected over CDP."""
        if not self._is_port_open(CDP_PORT):
            self._launch_chrome()
            opened = await self._wait_for_port(timeout_s=8.0)
            if not opened:
                raise BrowserError(f"Chrome did not open CDP port {CDP_PORT} within 8 seconds.", "browser_start")
            await asyncio.sleep(2)

        if self.browser:
            try:
                if not self.browser.is_connected():
                    await self._reset_handles()
            except Exception:
                await self._reset_handles()

        if not self.browser:
            try:
                await self._connect()
            except Exception as e:
                err = str(e)
                if any(sig in err for sig in _STALE_CDP_ERRORS):
                    print(f"[Browser] Stale CDP session detected, resetting port...")
                    await self._reset_handles()
                    self._kill_port(CDP_PORT)
                    await asyncio.sleep(1)
                    self._launch_chrome()
                    await self._wait_for_port()
                    await asyncio.sleep(2)
                    await self._connect()
                else:
                    raise BrowserError(f"Failed to connect over CDP: {e}", "cdp_connect")

    async def create_run_context(self) -> BrowserRunContext:
        """Creates an isolated browser context and page, inheriting cookies from Default profile."""
        await self.ensure_connected()
        if not self.browser:
            raise BrowserError("Browser not initialized.", "create_run_context")
            
        # Get cookies from the default profile context (contexts[0])
        default_context = self.browser.contexts[0]
        cookies = await default_context.cookies()
        
        # Open isolated context
        context = await self.browser.new_context()
        await context.add_cookies(cookies)
        
        page = await context.new_page()
        return BrowserRunContext(context, page)

    # ── Legacy/Backward Compatibility Interface ──────────────────────────────

    async def start(self) -> Page:
        """Legacy start: returns a new page on default profile context."""
        await self.ensure_connected()
        if not self.browser:
            raise BrowserError("Browser connection failed.", "legacy_start")
        self.page = await self.browser.contexts[0].new_page()
        return self.page

    async def stop(self) -> None:
        """Teardown standard page."""
        if self.page and not self.page.is_closed():
            await self.page.close()

    async def hard_reset(self) -> None:
        """Completely kills Chrome and Playwright."""
        print("[Browser] Hard reset requested.")
        await self._reset_handles()
        self._kill_port(CDP_PORT)
        await asyncio.sleep(1)

    async def investigate_with_graph(
        self,
        base_url: str,
        ticket_text: str,
        ticket_info: dict,
        approved_plan: str,
        groq_api_key: str,
        gemini_api_key: str = "",
        job_id: str = "",
        run_context: Optional[BrowserRunContext] = None,
    ) -> dict:
        """Runs LangGraph on either the run context or the global instance."""
        from graph_agent import ProjectSaneGraph

        # Pass active run context as the browser_instance context
        active_instance = run_context if run_context is not None else self
        graph = ProjectSaneGraph(browser_instance=active_instance)
        
        report_context = await graph.arun(
            ticket_text=ticket_text,
            ticket_info=ticket_info,
            base_url=base_url,
            approved_plan=approved_plan,
            groq_api_key=groq_api_key,
            gemini_api_key=gemini_api_key,
            job_id=job_id,
        )
        return report_context
