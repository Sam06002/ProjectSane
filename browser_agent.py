"""
browser_agent.py — Playwright CDP session management for Project Sane v3.

Resilience improvements over v2:
  - Auto-detects stale/incompatible CDP connections and self-heals
  - Kills the old Chrome process, relaunches, and retries once
  - Hard reset method for external callers (server.py)
"""

import asyncio
import os
import shutil
import socket
import subprocess
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

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

# Error fragments that signal a stale / incompatible CDP session
_STALE_CDP_ERRORS = (
    "Browser context management is not supported",
    "Target closed",
    "Connection closed",
    "WebSocket error",
    "websocket",
    "code=1000",
)


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page: Page = None
        # Live screenshot history captured by the graph executor node
        self.screenshots: list = []

    # ── Port utilities ─────────────────────────────────────────────────────────

    def _is_port_open(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def _kill_port(self, port: int) -> None:
        """Kill any process currently holding the CDP port."""
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

    # ── Chrome launcher ────────────────────────────────────────────────────────

    def _launch_chrome(self) -> None:
        """Sync cookies and launch a fresh Chrome subprocess."""
        print(f"[Browser] Launching Chrome on CDP port {CDP_PORT}...")

        agent_default = os.path.join(AGENT_PROFILE, "Default")
        os.makedirs(agent_default, exist_ok=True)
        
        if os.path.exists(SOURCE_COOKIES):
            # Sync to Default profile
            shutil.copy2(SOURCE_COOKIES, os.path.join(agent_default, "Cookies"))
            print("[Browser] Cookies synced from Profile 3 to Default.")
            
            # Sync to Profile 1 profile as well (for extra redundancy)
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
        """Poll until CDP port is open. Returns True on success."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if self._is_port_open(CDP_PORT):
                return True
            await asyncio.sleep(0.15)
        return False

    # ── Internal reset (wipes stale Playwright handles) ───────────────────────

    async def _reset_handles(self) -> None:
        """Disconnect and clear all Playwright objects without touching Chrome."""
        self.page = None
        self.context = None
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

    # ── Core connect logic ────────────────────────────────────────────────────

    async def _connect(self) -> None:
        """Start Playwright and connect to CDP. Raises on failure."""
        if not self.playwright:
            self.playwright = await async_playwright().start()

        print(f"[Browser] Connecting to CDP on port {CDP_PORT}...")
        self.browser = await self.playwright.chromium.connect_over_cdp(
            f"http://localhost:{CDP_PORT}"
        )
        self.context = self.browser.contexts[0]

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> Page:
        """
        Ensures Chrome is running via CDP and returns an active Page.

        Self-healing strategy:
          1. If port is closed → launch Chrome fresh.
          2. If browser handle exists but is disconnected → reset handles first.
          3. Try to connect. If a stale-session error occurs →
             kill the old process, relaunch Chrome, reset Playwright handles,
             reconnect (one retry only).
          4. Open a fresh tab. If new_page() fails (target closed) →
             full reset+relaunch+reconnect once.
          5. Return the Page.
        """
        # ── Step 1: Launch Chrome if port is not open ──────────────────────
        if not self._is_port_open(CDP_PORT):
            self._launch_chrome()
            opened = await self._wait_for_port(timeout_s=8.0)
            if not opened:
                raise RuntimeError(
                    f"Chrome did not open CDP port {CDP_PORT} within 8 seconds."
                )
            await asyncio.sleep(2)  # let DevTools protocol initialise internally

        # ── Step 1.5: Proactive stale-handle check ─────────────────────────
        # The browser object may exist but be disconnected (Chrome killed externally).
        if self.browser:
            try:
                if not self.browser.is_connected():
                    print("[Browser] Existing browser handle is disconnected. Resetting...")
                    await self._reset_handles()
            except Exception:
                # is_connected() itself can throw if the handle is deeply broken
                print("[Browser] Browser handle in broken state. Resetting...")
                await self._reset_handles()

        # ── Step 2: Connect (with one self-healing retry) ──────────────────
        for attempt in range(1, 3):  # attempt 1 = normal, attempt 2 = after reset
            try:
                if not self.browser:
                    await self._connect()
                break  # connected successfully

            except Exception as e:
                err = str(e)
                is_stale = any(sig in err for sig in _STALE_CDP_ERRORS)

                if is_stale and attempt == 1:
                    print(
                        f"[Browser] Stale CDP session detected: {err[:120]}\n"
                        f"[Browser] Killing old Chrome and relaunching..."
                    )
                    await self._reset_handles()
                    self._kill_port(CDP_PORT)
                    await asyncio.sleep(1)

                    self._launch_chrome()
                    opened = await self._wait_for_port(timeout_s=8.0)
                    if not opened:
                        raise RuntimeError(
                            f"Chrome did not reopen CDP port {CDP_PORT} after reset."
                        )
                    await asyncio.sleep(2)
                    # Loop will try _connect() again on attempt 2
                else:
                    raise  # non-stale error or second attempt failed → propagate

        # ── Step 3: Open a fresh tab (with self-healing on stale context) ──
        try:
            self.page = await self.context.new_page()
        except Exception as e:
            err_msg = str(e)
            print(f"[Browser] new_page() failed: {err_msg[:120]}. Full reset and retry...")
            # Full recovery: kill chrome, reset handles, relaunch, reconnect
            await self._reset_handles()
            self._kill_port(CDP_PORT)
            await asyncio.sleep(1)
            self._launch_chrome()
            opened = await self._wait_for_port(timeout_s=8.0)
            if not opened:
                raise RuntimeError(
                    f"Chrome did not reopen CDP port {CDP_PORT} after new_page() failure."
                )
            await asyncio.sleep(2)
            await self._connect()
            self.page = await self.context.new_page()

        print("[Browser] Session ready.")
        return self.page

    async def stop(self) -> None:
        """
        Close the active tab only. Chrome process and CDP connection stay alive
        so the next job reuses the same window without relaunching.
        """
        if self.page and not self.page.is_closed():
            await self.page.close()

    async def hard_reset(self) -> None:
        """
        Full teardown: kill Chrome, reset all Playwright handles.
        Call this if you want a completely clean slate between runs.
        """
        print("[Browser] Hard reset requested.")
        await self._reset_handles()
        self._kill_port(CDP_PORT)
        await asyncio.sleep(1)
        print("[Browser] Hard reset complete.")

    async def investigate_with_graph(
        self,
        base_url: str,
        ticket_text: str,
        ticket_info: dict,
        approved_plan: str,
        gemini_api_key: str,
        job_id: str = "",
    ) -> dict:
        """
        Run the LangGraph multi-agent state machine (planner → executor → reviewer).

        Passes this live BrowserManager (its active Playwright page + screenshot
        history) into the graph so the executor node can capture real-time
        screenshots and ground its evaluation in the true sandbox state. Returns
        the full final graph state dict.

        Args:
            base_url:       Active Odoo sandbox URL (already authenticated).
            ticket_text:    Raw customer support ticket text.
            ticket_info:    Dict produced by Engine 1 (Groq triage).
            approved_plan:  High-level investigation plan from Engine 1.
            gemini_api_key: Gemini API key for LLM calls within graph nodes.
            job_id:         Unique run identifier for tracing correlation.

        Returns:
            Final GraphState dict produced by the reviewer node.
        """
        from graph_agent import ProjectSaneGraph

        # Pass 'self' context straight into the state machine constructor
        graph = ProjectSaneGraph(browser_instance=self)
        report_context = await graph.arun(
            ticket_text=ticket_text,
            ticket_info=ticket_info,
            base_url=base_url,
            approved_plan=approved_plan,
            gemini_api_key=gemini_api_key,
            job_id=job_id,
        )
        return report_context

