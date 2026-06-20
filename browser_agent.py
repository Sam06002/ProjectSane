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
      position: fixed;
      left: 35px;
      top: 35px;
      width: 20px;
      height: 20px;
      background-color: #ff3f3f;
      border: 2px solid #ffffff;
      border-radius: 50%;
      z-index: 2147483647;
      pointer-events: none;
      box-shadow: 0 0 10px rgba(0,0,0,0.5);
      transform: translate(-10px, -10px);
      display: block;
    }
    .project-sane-click-ring {
      position: fixed;
      width: 30px;
      height: 30px;
      border: 3px solid #ff3f3f;
      border-radius: 50%;
      z-index: 2147483646;
      pointer-events: none;
      transform: translate(-15px, -15px);
      animation: projectSaneClickRing 0.5s ease-out forwards;
    }
    @keyframes projectSaneClickRing {
      from {
        opacity: 1;
        transform: translate(-15px, -15px) scale(0.2);
      }
      to {
        opacity: 0;
        transform: translate(-15px, -15px) scale(2.0);
      }
    }
    .project-sane-highlight {
      outline: 3px solid #ff3f3f !important;
      outline-offset: 3px !important;
      box-shadow: 0 0 0 6px rgba(255, 63, 63, 0.2), 0 0 24px rgba(255, 63, 63, 0.5) !important;
      transition: outline-color 0.15s ease, box-shadow 0.15s ease;
    }
  `;
  document.documentElement.appendChild(style);
  const cursor = document.createElement('div');
  cursor.id = 'project-sane-cursor';
  document.documentElement.appendChild(cursor);
  window.__projectSaneCursor = { x: 35, y: 35 };
  window.__projectSaneShowCursor = () => {
    const cursorEl = document.getElementById('project-sane-cursor');
    if (cursorEl) cursorEl.style.display = 'block';
  };
  window.__projectSaneMoveCursor = (x, y) => {
    const cursorEl = document.getElementById('project-sane-cursor');
    if (cursorEl) {
      cursorEl.style.left = x + 'px';
      cursorEl.style.top = y + 'px';
    }
    window.__projectSaneCursor = { x, y };
  };
  window.__projectSaneClickEffect = (x, y) => {
    const ring = document.createElement('div');
    ring.className = 'project-sane-click-ring';
    ring.style.left = x + 'px';
    ring.style.top = y + 'px';
    document.documentElement.appendChild(ring);
    setTimeout(() => ring.remove(), 500);
  };
  window.__projectSaneRemoveCursor = () => {
    const cursorEl = document.getElementById('project-sane-cursor');
    if (cursorEl) cursorEl.style.display = 'none';
  };
})();
"""


async def show_cursor(page: Page) -> None:
    """Make the virtual cursor visible on the page."""
    if not DEMO_MODE:
        return
    try:
        await page.evaluate("window.__projectSaneShowCursor?.()")
    except Exception:
        pass


async def move_cursor(page: Page, x: float, y: float) -> None:
    """Smoothly move both the Playwright mouse and the virtual cursor to (x, y) coordinates."""
    if not DEMO_MODE:
        try:
            await page.mouse.move(x, y)
        except Exception:
            pass
        return

    try:
        current_pos = await page.evaluate("window.__projectSaneCursor || { x: 35, y: 35 }")
    except Exception:
        current_pos = {"x": 35, "y": 35}

    start_x = current_pos.get("x", 35)
    start_y = current_pos.get("y", 35)

    steps = 20
    for i in range(1, steps + 1):
        t = i / steps
        ease = 2 * t * t if t < 0.5 else -1 + (4 - 2 * t) * t
        curr_x = start_x + (x - start_x) * ease
        curr_y = start_y + (y - start_y) * ease

        try:
            await page.mouse.move(curr_x, curr_y)
        except Exception:
            pass

        try:
            await page.evaluate("""({ x, y }) => {
                const cursor = document.getElementById('project-sane-cursor');
                if (cursor) {
                    cursor.style.left = x + 'px';
                    cursor.style.top = y + 'px';
                }
                window.__projectSaneCursor = { x, y };
            }""", {"x": curr_x, "y": curr_y})
        except Exception:
            pass

        await asyncio.sleep(0.01)


async def click_animation(page: Page, x: float, y: float) -> None:
    """Trigger the click ripple animation at the specified coordinates."""
    if not DEMO_MODE:
        return
    try:
        await page.evaluate(
            "({ x, y }) => window.__projectSaneClickEffect?.(x, y)",
            {"x": x, "y": y}
        )
    except Exception:
        pass


async def remove_cursor(page: Page) -> None:
    """Hide the virtual cursor from the page."""
    if not DEMO_MODE:
        return
    try:
        await page.evaluate("window.__projectSaneRemoveCursor?.()")
    except Exception:
        pass


async def ensure_demo_overlay(page: Page) -> None:
    if not DEMO_MODE:
        return
    try:
        await page.evaluate(DEMO_OVERLAY_SCRIPT)
        await show_cursor(page)
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
    Locates an Odoo UI element, highlights it, smoothly moves the virtual cursor
    to its coordinate target, triggers a click animation, pauses for 500ms, and
    performs the click.
    """
    await ensure_demo_overlay(page)
    element, box = await _visible_center_for_selector(page, selector, timeout)

    target_x = box["x"] + box["width"] / 2 + random.randint(-2, 2)
    target_y = box["y"] + box["height"] / 2 + random.randint(-2, 2)

    if DEMO_MODE:
        # Highlight target element
        try:
            await element.evaluate("""el => el.classList.add('project-sane-highlight')""")
        except Exception:
            pass

        # Move visible cursor along the same path as Playwright mouse
        await move_cursor(page, target_x, target_y)

        # Trigger click ripple animation
        await click_animation(page, target_x, target_y)

        # Pause 500ms
        await page.wait_for_timeout(500)

        # Execute click
        await element.click(timeout=timeout)

        # Remove highlight
        try:
            await element.evaluate("""el => el.classList.remove('project-sane-highlight')""")
        except Exception:
            pass

        # Delay after click = 1000ms
        await page.wait_for_timeout(1000)
    else:
        # Existing behavior preserved
        await page.mouse.move(target_x, target_y)
        await element.click(timeout=timeout)


async def human_like_fill(page: Page, selector: str, value: str, timeout: int = 10000) -> None:
    await ensure_demo_overlay(page)
    element, box = await _visible_center_for_selector(page, selector, timeout)
    target_x = box["x"] + min(max(box["width"] * 0.35, 10), max(box["width"] - 8, 10))
    target_y = box["y"] + box["height"] / 2

    if DEMO_MODE:
        # Highlight target element
        try:
            await element.evaluate("""el => el.classList.add('project-sane-highlight')""")
        except Exception:
            pass

        # Move cursor to field
        await move_cursor(page, target_x, target_y)

        # Focus/click animation
        await click_animation(page, target_x, target_y)
        await page.wait_for_timeout(350)

        # Fill element
        await page.fill(selector, value)

        # Remove highlight
        try:
            await element.evaluate("""el => el.classList.remove('project-sane-highlight')""")
        except Exception:
            pass

        await demo_pause(page, max(350, DEMO_ACTION_DELAY_MS // 2))
    else:
        await page.mouse.move(target_x, target_y)
        await page.fill(selector, value)


async def human_like_click_locator(page: Page, locator, timeout: int = 10000, **kwargs) -> None:
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
        # Highlight target element
        try:
            await handle.evaluate("""el => el.classList.add('project-sane-highlight')""")
        except Exception:
            pass

        # Move visible cursor along the same path as Playwright mouse
        await move_cursor(page, target_x, target_y)

        # Trigger click ripple animation
        await click_animation(page, target_x, target_y)

        # Pause 500ms
        await page.wait_for_timeout(500)

        # Execute click
        await locator.click(timeout=timeout, **kwargs)

        # Remove highlight
        try:
            await handle.evaluate("""el => el.classList.remove('project-sane-highlight')""")
        except Exception:
            pass

        # Delay after click = 1000ms
        await page.wait_for_timeout(1000)
    else:
        # Existing behavior preserved
        await page.mouse.move(target_x, target_y)
        await locator.click(timeout=timeout, **kwargs)


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


async def _safe_page_url(page) -> Optional[str]:
    """Safely retrieves page url, returning None if page is closed/stale."""
    try:
        return page.url
    except Exception:
        return None

async def _safe_page_title(page) -> Optional[str]:
    """Safely retrieves page title, returning None if page is closed/stale."""
    try:
        return await page.title()
    except Exception:
        return None

async def _safe_locator_count(locator) -> int:
    """Safely retrieves locator count, returning 0 if locator is stale/invalid."""
    try:
        return await locator.count()
    except Exception:
        return 0

async def _safe_close_page(page) -> bool:
    """Safely closes page, returning True if successful, False otherwise."""
    try:
        await page.close()
        return True
    except Exception:
        return False


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

        # Sync Local State key seed to allow encrypted cookie decryption on macOS
        local_state_src = os.path.join(os.path.dirname(os.path.dirname(SOURCE_COOKIES)), "Local State")
        local_state_dest = os.path.join(AGENT_PROFILE, "Local State")
        if os.path.exists(local_state_src):
            try:
                shutil.copy2(local_state_src, local_state_dest)
                print("[Browser] Chrome Local State synced.")
            except Exception as e:
                print(f"[Browser] Local State sync warning: {e}")
        
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
            try:
                self.playwright = await asyncio.wait_for(
                    async_playwright().start(), timeout=10.0
                )
            except asyncio.TimeoutError:
                raise BrowserError("Playwright startup timed out after 10 seconds.", "playwright_start")
        print(f"[Browser] Connecting to CDP on port {CDP_PORT}...")
        self.browser = await self.playwright.chromium.connect_over_cdp(
            f"http://localhost:{CDP_PORT}",
            timeout=15000
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

        # Lightweight health check: verify we can access contexts
        if self.browser:
            try:
                _ = self.browser.contexts
            except Exception:
                print("[Browser] Health check failed — stale browser handle. Reconnecting...")
                await self._reset_handles()
                self._kill_port(CDP_PORT)
                await asyncio.sleep(1)
                self._launch_chrome()
                opened = await self._wait_for_port(timeout_s=8.0)
                if not opened:
                    raise BrowserError(f"Chrome did not reopen CDP port {CDP_PORT} after health-check reset.", "browser_start")
                await asyncio.sleep(2)
                await self._connect()

    async def create_run_context(self) -> BrowserRunContext:
        """Creates a run page, preferring isolated contexts when CDP supports them."""
        await self.ensure_connected()
        if not self.browser:
            raise BrowserError("Browser not initialized.", "create_run_context")

        try:
            default_context = self.browser.contexts[0]
        except (IndexError, Exception) as e:
            raise BrowserError(f"No browser contexts available (Chrome may have closed): {e}", "create_run_context")

        # Clean up any leftover duplicate/support pages from previous runs to prevent tab clutter.
        # Each page is handled independently — a stale handle on one page must NOT crash the pipeline.
        try:
            from db_utils import is_duplicate_database
            pages_snapshot = []
            try:
                pages_snapshot = list(default_context.pages)
            except Exception as pe:
                print(f"[Browser] Could not snapshot pages (stale context): {pe}")
            for p in pages_snapshot:
                url = await _safe_page_url(p)
                if url is None:
                    continue
                try:
                    page_count = len(default_context.pages)
                except Exception:
                    page_count = 1
                if page_count <= 1:
                    break
                if "/_odoo/support" in url or is_duplicate_database(url) or "sane1-support" in url or "-support-" in url:
                    print(f"[Browser] Closing leftover page from previous run: {url}")
                    await _safe_close_page(p)
        except Exception as e:
            # Never let cleanup kill the pipeline
            print(f"[Browser] Non-fatal error cleaning up leftover pages: {e}")

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
