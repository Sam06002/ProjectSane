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
from demo_mode import (
    DEMO_ACTION_DELAY_MS,
    DEMO_CURSOR_STEP_DELAY_MS,
    DEMO_HIGHLIGHT_MS,
    DEMO_MODE,
    demo_settings,
)

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

DEMO_OVERLAY_SCRIPT = """
(() => {
  if (window.__projectSaneDemoOverlay) return;
  window.__projectSaneDemoOverlay = true;
  const style = document.createElement('style');
  style.id = 'project-sane-demo-style';
  style.textContent = `
    #project-sane-cursor {
      position: fixed; left: 24px; top: 24px; width: 22px; height: 22px;
      z-index: 2147483647; pointer-events: none; transform: translate(-2px, -2px);
      filter: drop-shadow(0 7px 14px rgba(0,0,0,.28));
    }
    #project-sane-cursor::before {
      content: ''; position: absolute; left: 0; top: 0; width: 0; height: 0;
      border-left: 14px solid #0ea5e9; border-top: 9px solid transparent;
      border-bottom: 9px solid transparent; transform: rotate(-18deg);
    }
    #project-sane-cursor::after {
      content: ''; position: absolute; left: 9px; top: 12px; width: 8px; height: 8px;
      border-radius: 50%; background: #f8fafc; border: 2px solid #0f172a;
    }
    .project-sane-click-ring {
      position: fixed; width: 16px; height: 16px; margin-left: -8px; margin-top: -8px;
      border: 2px solid #22d3ee; border-radius: 999px; z-index: 2147483646;
      pointer-events: none; animation: projectSaneClick .55s ease-out forwards;
    }
    @keyframes projectSaneClick {
      from { opacity: .95; transform: scale(.35); }
      to { opacity: 0; transform: scale(3.6); }
    }
    .project-sane-highlight {
      outline: 3px solid #22d3ee !important; outline-offset: 3px !important;
      box-shadow: 0 0 0 6px rgba(34,211,238,.18), 0 0 24px rgba(34,211,238,.5) !important;
      transition: outline-color .12s ease, box-shadow .12s ease;
    }
  `;
  document.documentElement.appendChild(style);
  const cursor = document.createElement('div');
  cursor.id = 'project-sane-cursor';
  document.documentElement.appendChild(cursor);
  window.__projectSaneCursor = { x: 35, y: 35 };
  window.__projectSaneMoveCursor = async (x, y, steps = 24, stepDelay = 18) => {
    const start = window.__projectSaneCursor || { x: 35, y: 35 };
    for (let i = 1; i <= steps; i += 1) {
      const t = i / steps;
      const ease = t < .5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
      const nx = start.x + (x - start.x) * ease;
      const ny = start.y + (y - start.y) * ease;
      cursor.style.left = `${nx}px`;
      cursor.style.top = `${ny}px`;
      window.__projectSaneCursor = { x: nx, y: ny };
      await new Promise(r => setTimeout(r, stepDelay));
    }
  };
  window.__projectSaneClickEffect = (x, y) => {
    const ring = document.createElement('div');
    ring.className = 'project-sane-click-ring';
    ring.style.left = `${x}px`;
    ring.style.top = `${y}px`;
    document.documentElement.appendChild(ring);
    setTimeout(() => ring.remove(), 700);
  };
  window.__projectSaneHighlight = async (el, duration = 550) => {
    if (!el) return;
    el.classList.add('project-sane-highlight');
    await new Promise(r => setTimeout(r, duration));
    el.classList.remove('project-sane-highlight');
  };
})();
"""


async def ensure_demo_overlay(page: Page) -> None:
    if not DEMO_MODE:
        return
    try:
        await page.evaluate(DEMO_OVERLAY_SCRIPT)
    except Exception:
        pass


async def demo_pause(page: Page, ms: int = DEMO_ACTION_DELAY_MS) -> None:
    if DEMO_MODE and ms > 0:
        await page.wait_for_timeout(ms)


async def _visible_center_for_selector(page: Page, selector: str, timeout: int) -> Tuple[object, dict]:
    element = await page.wait_for_selector(selector, state="visible", timeout=timeout)
    box = await element.bounding_box()
    if not box:
        raise ValueError(f"Target UI element '{selector}' has no bounding box (not clickable).")
    return element, box


async def human_like_click(page: Page, selector: str, timeout: int = 10000) -> None:
    """
    Locates an Odoo UI element, smoothly moves the virtual cursor to its
    coordinate target, and performs a natural click.
    """
    await ensure_demo_overlay(page)
    element, box = await _visible_center_for_selector(page, selector, timeout)

    target_x = box["x"] + box["width"] / 2 + random.randint(-2, 2)
    target_y = box["y"] + box["height"] / 2 + random.randint(-2, 2)
    if DEMO_MODE:
        await page.evaluate(
            """async ({ x, y, stepDelay }) => {
                await window.__projectSaneMoveCursor?.(x, y, 26, stepDelay);
            }""",
            {"x": target_x, "y": target_y, "stepDelay": DEMO_CURSOR_STEP_DELAY_MS},
        )
        await element.evaluate(
            """async (el, duration) => window.__projectSaneHighlight?.(el, duration)""",
            DEMO_HIGHLIGHT_MS,
        )
    else:
        await page.mouse.move(target_x, target_y)

    if DEMO_MODE:
        await page.evaluate("""({ x, y }) => window.__projectSaneClickEffect?.(x, y)""", {"x": target_x, "y": target_y})
    await element.click(timeout=timeout)
    await demo_pause(page)


async def human_like_fill(page: Page, selector: str, value: str, timeout: int = 10000) -> None:
    await ensure_demo_overlay(page)
    element, box = await _visible_center_for_selector(page, selector, timeout)
    target_x = box["x"] + min(max(box["width"] * 0.35, 10), max(box["width"] - 8, 10))
    target_y = box["y"] + box["height"] / 2
    if DEMO_MODE:
        await page.evaluate(
            """async ({ x, y, stepDelay }) => {
                await window.__projectSaneMoveCursor?.(x, y, 24, stepDelay);
            }""",
            {"x": target_x, "y": target_y, "stepDelay": DEMO_CURSOR_STEP_DELAY_MS},
        )
        await element.evaluate(
            """async (el, duration) => window.__projectSaneHighlight?.(el, duration)""",
            DEMO_HIGHLIGHT_MS,
        )
    await page.fill(selector, value)
    await demo_pause(page, max(350, DEMO_ACTION_DELAY_MS // 2))


async def human_like_click_locator(page: Page, locator, timeout: int = 10000) -> None:
    await locator.wait_for(state="visible", timeout=timeout)
    handle = await locator.element_handle()
    if handle is None:
        raise ValueError("Target locator has no element handle.")
    box = await handle.bounding_box()
    if not box:
        raise ValueError("Target locator has no bounding box.")
    await ensure_demo_overlay(page)
    target_x = box["x"] + box["width"] / 2 + random.randint(-2, 2)
    target_y = box["y"] + box["height"] / 2 + random.randint(-2, 2)
    if DEMO_MODE:
        await page.evaluate(
            """async ({ x, y, stepDelay }) => {
                await window.__projectSaneMoveCursor?.(x, y, 24, stepDelay);
            }""",
            {"x": target_x, "y": target_y, "stepDelay": DEMO_CURSOR_STEP_DELAY_MS},
        )
        await handle.evaluate(
            """async (el, duration) => window.__projectSaneHighlight?.(el, duration)""",
            DEMO_HIGHLIGHT_MS,
        )
        await page.evaluate("""({ x, y }) => window.__projectSaneClickEffect?.(x, y)""", {"x": target_x, "y": target_y})
    await locator.click(timeout=timeout)
    await demo_pause(page)


class BrowserRunContext:
    """Run-specific container for Playwright BrowserContext, Page, and screenshots."""
    def __init__(self, context: BrowserContext, page: Page, owns_context: bool = True):
        self.context: BrowserContext = context
        self.page: Page = page
        self.owns_context = owns_context
        self.screenshots: List[str] = []
        self.sse_emitter = None
        self.demo_mode = demo_settings()

    async def close(self) -> None:
        """Closes the page and browser context cleanly."""
        try:
            if not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        try:
            if self.owns_context:
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
        """Creates a run page, preferring isolated contexts when CDP supports them."""
        await self.ensure_connected()
        if not self.browser:
            raise BrowserError("Browser not initialized.", "create_run_context")

        default_context = self.browser.contexts[0]
        # Clean up any leftover duplicate/support pages from previous runs to prevent tab clutter
        try:
            from db_utils import is_duplicate_database
            for p in list(default_context.pages):
                url = p.url
                if len(default_context.pages) <= 1:
                    break
                if "/_odoo/support" in url or is_duplicate_database(url) or "sane1-support" in url or "-support-" in url:
                    print(f"[Browser] Closing leftover page from previous run: {url}")
                    await p.close()
        except Exception as e:
            print(f"[Browser] Error cleaning up leftover pages: {e}")

        cookies = []
        try:
            cookies = await default_context.cookies()
        except Exception as e:
            print(f"[Browser] Cookie snapshot unavailable over CDP; continuing without cookie copy: {e}")

        try:
            context = await self.browser.new_context()
            if cookies:
                await context.add_cookies(cookies)
            if DEMO_MODE:
                await context.add_init_script(DEMO_OVERLAY_SCRIPT)
            page = await context.new_page()
            await ensure_demo_overlay(page)
            return BrowserRunContext(context, page, owns_context=True)
        except Exception as e:
            err = str(e)
            if "Browser context management is not supported" not in err:
                raise
            print("[Browser] Isolated context unavailable over CDP; using fresh page in default profile.")
            if DEMO_MODE:
                try:
                    await default_context.add_init_script(DEMO_OVERLAY_SCRIPT)
                except Exception:
                    pass
            page = await default_context.new_page()
            await ensure_demo_overlay(page)
            return BrowserRunContext(default_context, page, owns_context=False)

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
        # Safety verification before graph execution starts
        from db_utils import assert_duplicate_database
        active_page = run_context.page if run_context else self.page
        if active_page:
            await assert_duplicate_database(active_page.url, base_url, page=active_page)
        else:
            await assert_duplicate_database(base_url, base_url)

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
