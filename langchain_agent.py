"""
langchain_agent.py — Tool registry for Project Sane.
Integrates centralized selectors and custom exceptions.
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any

from langchain_core.tools import tool
from browser_agent import demo_pause, ensure_demo_overlay, human_like_click, human_like_fill
from demo_mode import emit_demo_thought, emit_plan_progress
from exceptions import ExecutionError
import odoo_selectors as selectors


def create_langchain_tools(page: Any, browser: Any, base_url: str) -> list:
    """
    Factory that returns a list of asynchronous LangChain @tool-decorated browser tools.
    Resolves targets via selectors.py and raises ExecutionError on failure.
    """

    @tool
    async def navigate_to_url(url: str) -> str:
        """Navigate the browser to the specified URL and wait for it to load. Returns the final page URL."""
        print(f"[TOOL] navigate_to_url → {url}")
        try:
            await emit_demo_thought(browser, "Opening requested Odoo page")
            await page.goto(url)
            await page.wait_for_timeout(1500)
            await ensure_demo_overlay(page)
            await emit_demo_thought(browser, "Verifying page loaded")
            return page.url
        except Exception as e:
            print(f"[TOOL ERROR] navigate_to_url: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    @tool
    async def take_screenshot(label: str = "screenshot") -> str:
        """Take a screenshot of the current viewport. Returns the base64-encoded PNG image data."""
        print(f"[TOOL] take_screenshot → {label}")
        try:
            import base64, os, time
            path = f"output/tool_{label}_{int(time.time())}.png"
            os.makedirs("output", exist_ok=True)
            await page.screenshot(path=path, full_page=True)
            if browser is not None:
                if getattr(browser, "screenshots", None) is None:
                    browser.screenshots = []
                if path not in browser.screenshots:
                    browser.screenshots.append(path)
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            print(f"[TOOL ERROR] take_screenshot: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    @tool
    async def get_page_content() -> str:
        """Retrieve the full HTML/text content of the current page."""
        print(f"[TOOL] get_page_content → reading current page")
        try:
            return await page.content()
        except Exception as e:
            print(f"[TOOL ERROR] get_page_content: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    @tool
    async def click_element(selector: str) -> str:
        """Click on the element matching the specified selector registry key (e.g. 'save_button') or text."""
        print(f"[TOOL] click_element → {selector}")
        try:
            await emit_demo_thought(browser, f"Selecting {selector}")
            resolved_patterns = selectors.get_selector(selector)
            clicked = False
            error_msgs = []
            for pat in resolved_patterns:
                try:
                    await emit_demo_thought(browser, f"Highlighting {selector}")
                    await human_like_click(page, pat)
                    clicked = True
                    break
                except Exception as e:
                    error_msgs.append(f"Pattern '{pat}' failed: {e}")
            
            if not clicked:
                raise ExecutionError(f"Cannot click element '{selector}': No pattern matched on current page ({page.url}). Failures: {error_msgs}", "click_element")
            await emit_plan_progress(browser, 0, "active", f"Clicked {selector}")
            return f"Clicked: {selector}"
        except Exception as e:
            print(f"[TOOL ERROR] click_element: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    @tool
    async def type_into_field(input_str: str, selector: str = "input:visible") -> str:
        """Type input_str into the input field matching the registry key (e.g. 'reason_input') or selector."""
        print(f"[TOOL] type_into_field → inputting '{input_str}' into {selector}")
        try:
            await emit_demo_thought(browser, f"Entering value in {selector}")
            resolved_patterns = selectors.get_selector(selector)
            typed = False
            error_msgs = []
            for pat in resolved_patterns:
                try:
                    await human_like_fill(page, pat, input_str)
                    # Support return press for search bars
                    if "search" in selector.lower() or "search" in pat.lower():
                        await emit_demo_thought(browser, "Submitting search")
                        await page.locator(pat).press("Enter")
                        await demo_pause(page)
                    typed = True
                    break
                except Exception as e:
                    error_msgs.append(f"Pattern '{pat}' failed: {e}")

            if not typed:
                raise ExecutionError(f"Cannot type into field '{selector}': No pattern matched. Failures: {error_msgs}", "type_into_field")
            await emit_plan_progress(browser, 0, "active", f"Updated {selector}")
            return f"Typed '{input_str}' into {selector}"
        except Exception as e:
            print(f"[TOOL ERROR] type_into_field: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    @tool
    async def check_odoo_version() -> str:
        """Check the current Odoo database version by navigating to General Settings. Returns page title and URL."""
        print(f"[TOOL] check_odoo_version → checking")
        try:
            await emit_demo_thought(browser, "Opening General Settings")
            await page.goto(f"{base_url}/web#action=base_setup.action_general_configuration")
            await page.wait_for_timeout(1500)
            await ensure_demo_overlay(page)
            await emit_demo_thought(browser, "Verifying Odoo version context")
            title = await page.title()
            return f"Page: {title} | URL: {page.url}"
        except Exception as e:
            print(f"[TOOL ERROR] check_odoo_version: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    @tool
    async def get_installed_modules() -> str:
        """Get the list of installed Odoo modules by navigating to settings/apps."""
        print(f"[TOOL] get_installed_modules → checking")
        try:
            await emit_demo_thought(browser, "Opening Apps settings")
            await page.goto(f"{base_url}/odoo/settings/apps")
            await page.wait_for_timeout(2000)
            await ensure_demo_overlay(page)
            await emit_demo_thought(browser, "Checking installed modules")
            return await page.title()
        except Exception as e:
            print(f"[TOOL ERROR] get_installed_modules: {e}\n{traceback.format_exc()}")
            return f"ERROR: {e}"

    @tool
    async def lookup_navigation(feature: str) -> str:
        """Look up the typical Odoo navigation URL for a given feature name (e.g. sales, invoicing, settings)."""
        print(f"[TOOL] lookup_navigation → {feature}")
        import knowledge.navigation
        route = knowledge.navigation.get_navigation_path(feature)
        return f"{base_url}{route}"

    @tool
    async def search_past_tickets(query: str) -> str:
        """Search the local SQLite memory database for similar resolved tickets using a query string."""
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

    return [
        navigate_to_url,
        take_screenshot,
        get_page_content,
        click_element,
        type_into_field,
        check_odoo_version,
        get_installed_modules,
        lookup_navigation,
        search_past_tickets,
    ]
