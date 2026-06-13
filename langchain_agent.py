"""
langchain_agent.py — Compatibility shim for ANTIGRAVITY_PROMPT_013 tooling.

Exposes create_odoo_tools() with:
  - nest_asyncio-powered _sync() that works inside a running event loop
  - print() on every tool invocation for terminal visibility
  - Full traceback on any tool failure

NOTE: The primary LangGraph state machine lives in graph_agent.py.
      This file provides a synchronous tool registry for legacy callers.
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any

import nest_asyncio
nest_asyncio.apply()  # allows run_until_complete inside an already-running event loop


def _sync(coro):
    """Run an async coroutine synchronously from a sync tool."""
    try:
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(coro)
    except RuntimeError:
        # Fallback: create a new event loop if needed
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def create_odoo_tools(page: Any, browser: Any, base_url: str) -> list:
    """
    Factory that returns a list of synchronous Odoo browser tool callables.
    Each tool prints its invocation to the terminal for live visibility.

    Args:
        page:     Playwright Page object (or mock).
        browser:  Playwright Browser object (or None).
        base_url: The authenticated Odoo database URL.

    Returns:
        List of tool objects exposing .name and __call__.
    """

    class Tool:
        def __init__(self, name: str, fn):
            self.name = name
            self._fn = fn

        def __call__(self, *args, **kwargs):
            return self._fn(*args, **kwargs)

    # ── Tool Implementations ───────────────────────────────────────────────────

    def navigate_to_url(url: str) -> str:
        print(f"[TOOL] navigate_to_url → {url}")
        try:
            async def _go():
                await page.goto(url)
                await page.wait_for_timeout(1500)
                return page.url
            return _sync(_go())
        except Exception as e:
            print(f"[TOOL ERROR] navigate_to_url: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    def take_screenshot(label: str = "screenshot") -> str:
        print(f"[TOOL] take_screenshot → {label}")
        try:
            import base64, os, time
            path = f"output/tool_{label}_{int(time.time())}.png"
            os.makedirs("output", exist_ok=True)

            async def _snap():
                await page.screenshot(path=path, full_page=True)
                with open(path, "rb") as f:
                    return base64.b64encode(f.read()).decode("utf-8")
            return _sync(_snap())
        except Exception as e:
            print(f"[TOOL ERROR] take_screenshot: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    def get_page_content() -> str:
        print(f"[TOOL] get_page_content → reading current page")
        try:
            async def _content():
                return await page.content()
            return _sync(_content())
        except Exception as e:
            print(f"[TOOL ERROR] get_page_content: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    def click_element(selector: str) -> str:
        print(f"[TOOL] click_element → {selector}")
        try:
            async def _click():
                await page.click(selector)
                await page.wait_for_timeout(800)
                return f"Clicked: {selector}"
            return _sync(_click())
        except Exception as e:
            print(f"[TOOL ERROR] click_element: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    def type_into_field(input_str: str, selector: str = "input:visible") -> str:
        print(f"[TOOL] type_into_field → {input_str}")
        try:
            async def _type():
                await page.fill(selector, input_str)
                return f"Typed '{input_str}' into {selector}"
            return _sync(_type())
        except Exception as e:
            print(f"[TOOL ERROR] type_into_field: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    def check_odoo_version() -> str:
        print(f"[TOOL] check_odoo_version → checking")
        try:
            async def _version():
                await page.goto(f"{base_url}/web#action=base_setup.action_general_configuration")
                await page.wait_for_timeout(1500)
                title = await page.title()
                return f"Page: {title} | URL: {page.url}"
            return _sync(_version())
        except Exception as e:
            print(f"[TOOL ERROR] check_odoo_version: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    def get_installed_modules() -> str:
        print(f"[TOOL] get_installed_modules → checking")
        try:
            async def _modules():
                await page.goto(f"{base_url}/odoo/settings/apps")
                await page.wait_for_timeout(2000)
                return await page.title()
            return _sync(_modules())
        except Exception as e:
            print(f"[TOOL ERROR] get_installed_modules: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    def lookup_navigation(feature: str) -> str:
        print(f"[TOOL] lookup_navigation → {feature}")
        # Returns a known navigation hint for common Odoo features
        nav_map = {
            "sales": "/odoo/sales",
            "invoicing": "/odoo/accounting",
            "inventory": "/odoo/inventory",
            "purchase": "/odoo/purchase",
            "settings": "/odoo/settings",
        }
        for key, path in nav_map.items():
            if key in feature.lower():
                return f"{base_url}{path}"
        return f"{base_url}/odoo"

    def search_past_tickets(query: str) -> str:
        print(f"[TOOL] search_past_tickets → {query}")
        try:
            import memory_store
            parts = query.split(" ", 1)
            module = parts[0] if parts else ""
            error = parts[1] if len(parts) > 1 else query
            results = memory_store.search_similar_resolutions(module, error)
            if not results:
                return "No past tickets found matching that query."
            lines = []
            for r in results:
                lines.append(
                    f"Ticket: {r['ticket_summary']}\n"
                    f"  Root Cause: {r['root_cause']}\n"
                    f"  Fix Steps: {r['resolution_steps']}"
                )
            return "\n\n".join(lines)
        except Exception as e:
            print(f"[TOOL ERROR] search_past_tickets: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    # ── Register and return tools ──────────────────────────────────────────────
    return [
        Tool("navigate_to_url",      navigate_to_url),
        Tool("take_screenshot",      take_screenshot),
        Tool("get_page_content",     get_page_content),
        Tool("click_element",        click_element),
        Tool("type_into_field",      type_into_field),
        Tool("check_odoo_version",   check_odoo_version),
        Tool("get_installed_modules", get_installed_modules),
        Tool("lookup_navigation",    lookup_navigation),
        Tool("search_past_tickets",  search_past_tickets),
    ]
