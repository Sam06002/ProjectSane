"""
executor.py — Deterministic execution engine for Project Sane v2.

Translates abstract Action objects into safe, deterministic Playwright operations.
Absolutely no code generation or `exec()` is allowed.
"""

import asyncio
import logging
import time
import random
from typing import Any, Dict, Optional, List

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from schema import Action, ActionType, ExecutionResult, Step, Plan
from stream_manager import StreamManager
from db_utils import assert_duplicate_database

logger = logging.getLogger(__name__)

# Registry mapping human-readable labels to robust Playwright locators.
# This ensures the LLM never generates raw selectors.
# EXPANDED: covers all common targets the LLM generates to prevent text-fallback timeouts.
SELECTOR_REGISTRY = {
    # ── Top-level menu / navigation ──────────────────────────────────────
    "Companies": ".dropdown-item:has-text('Companies'), a:has-text('Manage Companies'), .dropdown-menu a[data-menu-xmlid*='company']",
    "companies": ".dropdown-item:has-text('Companies'), a:has-text('Manage Companies'), .dropdown-menu a[data-menu-xmlid*='company']",
    "Your Company": ".o_list_view .o_data_row:first-child, tr.o_data_row:first-child, .o_setting_box:has-text('Companies') a:has-text('Update Info'), a:has-text('Your Company')",
    "your company": ".o_list_view .o_data_row:first-child, tr.o_data_row:first-child, .o_setting_box:has-text('Companies') a:has-text('Update Info'), a:has-text('Your Company')",
    "My Company": ".o_list_view .o_data_row:first-child, tr.o_data_row:first-child, .o_setting_box:has-text('Companies') a:has-text('Update Info'), a:has-text('My Company')",
    "my company": ".o_list_view .o_data_row:first-child, tr.o_data_row:first-child, .o_setting_box:has-text('Companies') a:has-text('Update Info'), a:has-text('My Company')",
    "settings": "a:has-text('Settings'), a[data-menu-xmlid='base.menu_administration']",
    "apps": "a:has-text('Apps'), a[data-menu-xmlid='base.menu_management']",
    "modules": "a:has-text('Modules')",
    "Apps menu": "a.o_menu_toggle, .o_menu_toggle, a[title='Home Menu'], a.o_app_drawer_toggle, .o_app_drawer_toggle, a[title='Go to your Odoo Apps'], .o_navbar_apps_menu",
    "apps menu": "a.o_menu_toggle, .o_menu_toggle, a[title='Home Menu'], a.o_app_drawer_toggle, .o_app_drawer_toggle, a[title='Go to your Odoo Apps'], .o_navbar_apps_menu",

    # ── CRUD buttons ─────────────────────────────────────────────────────
    "save_button": "button:has-text('Save'), .o_form_button_save",
    "Save": "button:has-text('Save'), .o_form_button_save",
    "save": "button:has-text('Save'), .o_form_button_save",
    "discard_button": "button:has-text('Discard'), .o_form_button_cancel",
    "Discard": "button:has-text('Discard'), .o_form_button_cancel",
    "confirm_dialog": ".modal-dialog button:has-text('Ok'), .modal-dialog button:has-text('Confirm')",
    "Create": "button:has-text('Create'), button:has-text('New'), .o_list_button_add, .o_form_button_create",
    "create": "button:has-text('Create'), button:has-text('New'), .o_list_button_add, .o_form_button_create",
    "New": "button:has-text('New'), button:has-text('Create'), .o_list_button_add, .o_form_button_create",
    "new": "button:has-text('New'), button:has-text('Create'), .o_list_button_add, .o_form_button_create",
    "New Contact": "button:has-text('New'), button:has-text('Create'), .o_list_button_add",
    "New contact": "button:has-text('New'), button:has-text('Create'), .o_list_button_add",
    "Edit": "button:has-text('Edit'), .o_form_button_edit",
    "edit": "button:has-text('Edit'), .o_form_button_edit",

    # ── Form fields ──────────────────────────────────────────────────────
    "Email field": "input[name='email'], .o_field_widget[name='email'] input, div[name='email'] input",
    "email field": "input[name='email'], .o_field_widget[name='email'] input, div[name='email'] input",
    "Email field label": ".o_field_widget[name='email'], div[name='email'], label:has-text('Email')",
    "email": "input[name='email'], .o_field_widget[name='email'] input, div[name='email'] input",
    "Phone field": "input[name='phone'], .o_field_widget[name='phone'] input, div[name='phone'] input",
    "Name field": "input[name='name'], .o_field_widget[name='name'] input, div[name='name'] input",

    # ── Notifications / warnings / banners ───────────────────────────────
    "Warning banner text": ".o_notification_content, .o_notification_body, .o_notification, .alert, .o_field_invalid",
    "warning banner text": ".o_notification_content, .o_notification_body, .o_notification, .alert, .o_field_invalid",
    "Warning banner": ".o_notification_content, .o_notification_body, .o_notification, .alert",
    "Error message": ".o_notification_content, .o_notification_body, .o_error_dialog, .alert-danger",

    # ── Search / filter ──────────────────────────────────────────────
    # List-view search bar
    "search_bar": ".o_searchview_input",
    "Search bar": ".o_searchview_input",
    "search bar": ".o_searchview_input",
    # Settings page search bar — uses placeholder='Search...' not .o_searchview_input
    "Search...": ".o_searchview_input, input[placeholder='Search...'], .o_searchview .o_searchview_input, .o_setting_search input, input[type='search'], .o_cp_searchview input",
    "search...": ".o_searchview_input, input[placeholder='Search...'], .o_searchview .o_searchview_input, .o_setting_search input, input[type='search'], .o_cp_searchview input",
    "Settings search": "input[placeholder='Search...'], .o_setting_search input",
    "settings search": "input[placeholder='Search...'], .o_setting_search input",
    "Search settings": "input[placeholder='Search...'], .o_setting_search input",
    "search settings": "input[placeholder='Search...'], .o_setting_search input",
    "Search settings...": "input[placeholder='Search...'], .o_setting_search input",
    "search settings...": "input[placeholder='Search...'], .o_setting_search input",
    # Filter chips / clear search
    "Clear search filter": ".o_searchview_facet .o_delete, .o_cp_searchview .o_facet_remove",
    "clear search filter": ".o_searchview_facet .o_delete, .o_cp_searchview .o_facet_remove",

    # ── List view ────────────────────────────────────────────────────────
    "list_first_row": ".o_list_view .o_data_row:first-child",
    "First row": ".o_list_view .o_data_row:first-child",
    "first row": ".o_list_view .o_data_row:first-child",

    # ── Settings page fields ─────────────────────────────────────────
    # Common targets the LLM extracts from /odoo/settings
    "Fiscal Localization field value": ".o_field_widget[name='chart_template_id'], div[name='chart_template_id'], .o_setting_box:has-text('Fiscal Localization')",
    "fiscal localization field value": ".o_field_widget[name='chart_template_id'], div[name='chart_template_id']",
    "Fiscal Localization": ".o_field_widget[name='chart_template_id'], div[name='chart_template_id'], .o_setting_box:has-text('Fiscal Localization')",
    "fiscal localization": ".o_field_widget[name='chart_template_id'], div[name='chart_template_id']",
    "Localization": ".o_setting_box:has-text('Localization'), .o_setting_box:has-text('Fiscal')",
    "localization": ".o_setting_box:has-text('Localization'), .o_setting_box:has-text('Fiscal')",
    "Company currency": ".o_field_widget[name='currency_id'], div[name='currency_id']",
    "company currency": ".o_field_widget[name='currency_id'], div[name='currency_id']",
    "Default company": ".o_field_widget[name='company_id'], div[name='company_id']",
    "Company name": ".o_field_widget[name='name'] input, div[name='name'] input",

    # ── Website frontend elements ────────────────────────────────────────
    # Website navigation / header — covers both the builder view and raw frontend
    "Main navigation menu items": "header nav, header .navbar-nav, #top_menu, header .top_menu, nav.navbar, #wrapwrap header nav, header",
    "main navigation menu items": "header nav, header .navbar-nav, #top_menu, header .top_menu, nav.navbar, #wrapwrap header nav, header",
    "Navigation menu": "header nav, header .navbar-nav, #top_menu, nav.navbar, header",
    "navigation menu": "header nav, header .navbar-nav, #top_menu, nav.navbar, header",
    "Website menu": "header nav, header .navbar-nav, #top_menu, nav.navbar, header",
    "website menu": "header nav, header .navbar-nav, #top_menu, nav.navbar, header",
    "Menu items": "header nav, header .navbar-nav, #top_menu, nav.navbar, header",
    "menu items": "header nav, header .navbar-nav, #top_menu, nav.navbar, header",
    # Publication status (Website Pages list view)
    "Publication status": ".o_list_view .o_data_row .o_field_widget[name='website_published'], .badge:has-text('Published'), .badge:has-text('Unpublished'), .o_list_view .o_data_row td:last-child",
    "publication status": ".o_list_view .o_data_row .o_field_widget[name='website_published'], .badge:has-text('Published'), .badge:has-text('Unpublished'), .o_list_view .o_data_row td:last-child",
    "Publication status of 'Service' page": ".o_list_view .o_data_row:has-text('Service') .o_field_widget[name='website_published'], .o_list_view .o_data_row:has-text('Service'), .o_list_view .o_data_row:first-child",
    "Published": ".badge:has-text('Published'), .o_field_widget[name='website_published']",
    "Unpublished": ".badge:has-text('Unpublished'), .o_field_widget[name='website_published']",
    # Website page list
    "Website pages": ".o_list_view .o_data_row, .o_list_view",
    "website pages": ".o_list_view .o_data_row, .o_list_view",
    "Page list": ".o_list_view .o_data_row, .o_list_view",
    "page list": ".o_list_view .o_data_row, .o_list_view",

    # ── Chatter / messaging ──────────────────────────────────────────────
    "Log note": "button:has-text('Log note'), .o_chatter_button_log_note",
    "Send message": "button:has-text('Send message'), .o_chatter_button_new_message",

    # ── Debug / developer mode ───────────────────────────────────────────
    # The debug menu icon is a small bug/gear in the top navbar. Rather than
    # relying on hover-reveal selectors, clicks on these targets are intercepted
    # in _execute_action and handled via URL (?debug=1). Selectors below are
    # kept as fallback for extract-type steps that just want to read the element.
    "Debug menu": ".o_debug_manager .o_dropdown_toggler, .o_debug_manager button, a[href*='debug']",
    "debug menu": ".o_debug_manager .o_dropdown_toggler, .o_debug_manager button, a[href*='debug']",
    "Debug mode": ".o_debug_manager .o_dropdown_toggler, .o_debug_manager button, a[href*='debug']",
    "debug mode": ".o_debug_manager .o_dropdown_toggler, .o_debug_manager button, a[href*='debug']",
    "Debug": ".o_debug_manager .o_dropdown_toggler, .o_debug_manager button",
    "debug": ".o_debug_manager .o_dropdown_toggler, .o_debug_manager button",
    "Developer mode": ".o_debug_manager .o_dropdown_toggler, .o_debug_manager button",
    "developer mode": ".o_debug_manager .o_dropdown_toggler, .o_debug_manager button",
    "Activate developer mode": "a:has-text('Activate the developer mode'), a[href*='debug']",
    "activate developer mode": "a:has-text('Activate the developer mode'), a[href*='debug']",
}

# ── Targets that activate Odoo debug/developer mode ─────────────────────────
# When a click action targets any of these labels, the executor intercepts it
# and activates debug mode via URL (?debug=1) rather than clicking a DOM element.
# This is more reliable: the debug icon requires hover-reveal and may not be
# visible at all if the URL already contains debug=1.
DEBUG_TARGETS: set[str] = {
    "debug menu", "debug mode", "debug", "developer mode",
    "Debug menu", "Debug mode", "Debug", "Developer mode",
    "activate developer mode", "Activate developer mode",
    "activate the developer mode", "Activate the developer mode",
}

# Maps common module names (as generated by the LLM) to their Odoo URL paths.
# This prevents the "text='contacts'" fallback that finds 0 elements.
ODOO_MODULE_ROUTES = {
    "contacts": "/odoo/contacts",
    "Contacts": "/odoo/contacts",
    "contact": "/odoo/contacts",
    "Contact": "/odoo/contacts",
    "crm": "/odoo/crm",
    "CRM": "/odoo/crm",
    "sales": "/odoo/sales",
    "Sales": "/odoo/sales",
    "purchase": "/odoo/purchase",
    "Purchase": "/odoo/purchase",
    "inventory": "/odoo/inventory",
    "Inventory": "/odoo/inventory",
    "accounting": "/odoo/accounting",
    "Accounting": "/odoo/accounting",
    "invoicing": "/odoo/accounting",
    "Invoicing": "/odoo/accounting",
    "website": "/odoo/website",
    "Website": "/odoo/website",
    "helpdesk": "/odoo/helpdesk",
    "Helpdesk": "/odoo/helpdesk",
    "project": "/odoo/project",
    "Project": "/odoo/project",
    "employees": "/odoo/employees",
    "Employees": "/odoo/employees",
    "companies": "/odoo/companies",
    "Companies": "/odoo/companies",
    "users": "/odoo/users",
    "Users": "/odoo/users",
    "settings": "/odoo/settings",
    "Settings": "/odoo/settings",
    "apps": "/odoo/action-base.open_module_tree",
    "Apps": "/odoo/action-base.open_module_tree",
    "discuss": "/odoo/discuss",
    "Discuss": "/odoo/discuss",
    "calendar": "/odoo/calendar",
    "Calendar": "/odoo/calendar",
    # Website sub-pages — backend action routes
    "Website Pages": "/odoo/action-website.action_website_pages_list",
    "website pages": "/odoo/action-website.action_website_pages_list",
    "Website pages": "/odoo/action-website.action_website_pages_list",
    "Pages": "/odoo/action-website.action_website_pages_list",
    "pages": "/odoo/action-website.action_website_pages_list",
}

class ExecutionEngine:
    def __init__(self, page: Page, run_logger=None, sse_emitter=None, prod_url: str = ""):
        self.page = page
        self._run_logger = run_logger
        self.emit = sse_emitter # Allows sending real-time UI logs
        self.prod_url = prod_url

    async def human_like_glide_and_click(self, locator, selector_name: str = ""):
        """Smoothly slides the cursor across the screen before clicking."""
        box = await locator.bounding_box()
        if box:
            target_x = box["x"] + box["width"] / 2 + random.randint(-2, 2)
            target_y = box["y"] + box["height"] / 2 + random.randint(-2, 2)
            
            # Simulated smooth curve pathing frames
            steps = 12
            for i in range(steps):
                t = i / float(steps)
                await self.page.mouse.move(100 + (target_x - 100) * t, 100 + (target_y - 100) * t)
                await asyncio.sleep(0.01)
                
            await self.page.mouse.click(target_x, target_y)

    async def execute_plan(self, plan: Plan) -> list:
        """Iterates sequentially through the validated plan steps with 0 token cost."""
        # Safety assertion: verify current database is not production before any execution begins
        if self.prod_url:
            await assert_duplicate_database(self.page.url, self.prod_url, page=self.page, run_logger=self._run_logger)

        results = []
        for step in plan.steps:
            # 1. Broadcast action start immediately to the UI Server-Sent Events stream
            if self.emit:
                await self.emit(StreamManager.emit_action_start(step.id, step.action.type.value, step.action.target))
            
            # 2. Run the local interaction mapping sequence 
            t0 = time.time()
            result, elements, selector = await self._execute_action(step.action, step_id=step.id)
            duration_ms = (time.time() - t0) * 1000
            
            # 3. Update the visual cursor track and log actions
            screenshot_path = None
            if self._run_logger:
                screenshot_path = await self._run_logger.capture_screenshot(self.page, step.id)
                self._run_logger.log_action(
                    step_id=step.id, action_type=step.action.type.value, target=step.action.target,
                    selector_used=selector, success=result.success, message=result.message,
                    duration_ms=duration_ms, extracted_text=result.extracted_text, screenshot_path=screenshot_path
                )
            
            # 4. Stream action result details back to the analyst's web page panel
            if self.emit:
                await self.emit(StreamManager.emit_action_result(
                    step.id, result.success, result.message, result.extracted_text, screenshot_path=screenshot_path
                ))
                
            results.append(result)
            await asyncio.sleep(1.2) # Elegant pacing delay for video recordings
            
        return results

    async def _animate_cursor_to_element(self, locator) -> None:
        """Injects a custom cursor overlay and animates it smoothly to the target element, triggering a ripple click effect."""
        try:
            # 1. Inject the visual cursor DOM structure and keyframes style if not present
            await self.page.evaluate("""() => {
                if (!document.getElementById('project-sane-cursor')) {
                    const cursor = document.createElement('div');
                    cursor.id = 'project-sane-cursor';
                    cursor.style.position = 'fixed';
                    cursor.style.top = '0px';
                    cursor.style.left = '0px';
                    cursor.style.width = '24px';
                    cursor.style.height = '24px';
                    cursor.style.borderRadius = '50%';
                    cursor.style.backgroundColor = 'rgba(0, 196, 255, 0.7)'; // beautiful translucent neon cyan
                    cursor.style.border = '2.5px solid white';
                    cursor.style.boxShadow = '0 0 15px rgba(0, 196, 255, 0.9)';
                    cursor.style.pointerEvents = 'none';
                    cursor.style.zIndex = '9999999';
                    cursor.style.transition = 'top 0.45s cubic-bezier(0.25, 0.8, 0.25, 1), left 0.45s cubic-bezier(0.25, 0.8, 0.25, 1), transform 0.2s ease';
                    cursor.style.transform = 'translate(-50%, -50%) scale(1)';
                    
                    const style = document.createElement('style');
                    style.innerHTML = `
                        @keyframes sane-ripple {
                            0% { transform: translate(-50%, -50%) scale(1); opacity: 1; }
                            100% { transform: translate(-50%, -50%) scale(3.5); opacity: 0; }
                        }
                        .sane-ripple-effect {
                            animation: sane-ripple 0.5s cubic-bezier(0.1, 0.8, 0.3, 1) forwards;
                        }
                    `;
                    document.head.appendChild(style);
                    document.body.appendChild(cursor);
                }
            }""")
            
            box = await locator.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                
                # Animate the cursor to target coordinate
                await self.page.evaluate(f"""() => {{
                    const cursor = document.getElementById('project-sane-cursor');
                    if (cursor) {{
                        cursor.style.left = '{x}px';
                        cursor.style.top = '{y}px';
                    }}
                }}""")
                
                await asyncio.sleep(0.5)  # Let glide complete
                
                # Trigger neon pulse and click ripple animation
                await self.page.evaluate(f"""() => {{
                    const cursor = document.getElementById('project-sane-cursor');
                    if (cursor) {{
                        cursor.style.backgroundColor = 'rgba(255, 46, 99, 0.9)'; // vivid pink-red pulse
                        cursor.style.boxShadow = '0 0 20px rgba(255, 46, 99, 1)';
                        
                        const ripple = document.createElement('div');
                        ripple.style.position = 'fixed';
                        ripple.style.left = '{x}px';
                        ripple.style.top = '{y}px';
                        ripple.style.width = '24px';
                        ripple.style.height = '24px';
                        ripple.style.borderRadius = '50%';
                        ripple.style.border = '3.5px solid rgba(255, 46, 99, 0.8)';
                        ripple.style.pointerEvents = 'none';
                        ripple.style.zIndex = '9999998';
                        ripple.className = 'sane-ripple-effect';
                        
                        document.body.appendChild(ripple);
                        setTimeout(() => ripple.remove(), 500);
                        
                        setTimeout(() => {{
                            cursor.style.backgroundColor = 'rgba(0, 196, 255, 0.7)';
                            cursor.style.boxShadow = '0 0 15px rgba(0, 196, 255, 0.9)';
                        }}, 180);
                    }}
                }}""")
                await asyncio.sleep(0.2)
        except Exception:
            pass  # Cursor animation failures should never block execution

    async def _resolve_locator(self, target: str):
        """Maps a schema target to a Playwright locator, prioritizing the registry.
        
        Resolution order:
          1. Exact match in SELECTOR_REGISTRY → use that selector.
          2. Case-insensitive match in SELECTOR_REGISTRY → use that selector.
          3. Fallback → Playwright text= locator (last resort).
        """
        if target in SELECTOR_REGISTRY:
            return self.page.locator(SELECTOR_REGISTRY[target])
        
        # Case-insensitive fallback through registry
        target_lower = target.lower().strip()
        target_no_dots = target_lower.rstrip(".")
        for key, selector in SELECTOR_REGISTRY.items():
            key_lower = key.lower()
            if key_lower == target_lower or key_lower.rstrip(".") == target_no_dots:
                return self.page.locator(selector)
        
        # Last resort: Treat as plain text search with multiple strategies
        # Try get_by_text (more robust) then locator text= (legacy)
        loc = self.page.get_by_text(target, exact=False)
        try:
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass
        return self.page.locator(f"text='{target}'").first

    async def _execute_action(
        self, action: Action, step_id: int = 0
    ) -> tuple:
        """
        Execute a single action.

        Returns:
            (ExecutionResult, elements_found: Optional[int], selector_used: Optional[str])
        """
        selector_used: Optional[str] = SELECTOR_REGISTRY.get(action.target)
        elements_found: Optional[int] = None

        try:
            if action.type == ActionType.navigate:
                if action.target.startswith("/"):
                    # Explicit path — navigate directly
                    from urllib.parse import urlparse
                    parsed = urlparse(self.page.url)
                    base_url = f"{parsed.scheme}://{parsed.netloc}"
                    target_url = f"{base_url}{action.target}"
                    await self.page.goto(target_url)
                    # ── Redirect detection: if Odoo silently redirected us
                    # to a completely different path (e.g. /odoo/website → /),
                    # report the mismatch so the pipeline doesn't silently
                    # continue on the wrong page.
                    try:
                        await self.page.wait_for_load_state("load", timeout=5000)
                    except Exception:
                        pass
                    final_path = urlparse(self.page.url).path.rstrip("/") or "/"
                    intended_path = action.target.rstrip("/") or "/"
                    if final_path != intended_path and not final_path.startswith(intended_path):
                        # Check if we have a route in ODOO_MODULE_ROUTES for a
                        # path suffix that matches (e.g. /odoo/website/pages)
                        path_parts = action.target.strip("/").split("/")
                        route_key = "/".join(path_parts[-2:]) if len(path_parts) >= 2 else path_parts[-1]
                        for rk, rv in ODOO_MODULE_ROUTES.items():
                            if rk.lower() == route_key.lower() or rk.lower().replace(" ", "") == route_key.lower().replace("/", ""):
                                # Found a matching module route — redirect there
                                redirect_url = f"{base_url}{rv}"
                                selector_used = f"ODOO_MODULE_ROUTES['{rk}'] → {rv} (redirect recovery)"
                                await self.page.goto(redirect_url)
                                try:
                                    await self.page.wait_for_load_state("load", timeout=5000)
                                except Exception:
                                    pass
                                return (
                                    ExecutionResult(step_id=step_id, success=True, message=f"Navigated to {action.target} (recovered via route '{rk}')"),
                                    elements_found,
                                    selector_used,
                                )
                        # No route recovery found — report the redirect as a warning
                        return (
                            ExecutionResult(
                                step_id=step_id,
                                success=True,
                                message=f"Navigated to {action.target} but Odoo redirected to {self.page.url} (path mismatch: expected '{intended_path}', got '{final_path}')"
                            ),
                            elements_found,
                            selector_used,
                        )
                elif action.target in ODOO_MODULE_ROUTES or action.target.lower() in {k.lower() for k in ODOO_MODULE_ROUTES}:
                    # Module name → resolve to URL path (prevents text-click timeout)
                    route = ODOO_MODULE_ROUTES.get(action.target)
                    if not route:
                        # Case-insensitive lookup
                        for k, v in ODOO_MODULE_ROUTES.items():
                            if k.lower() == action.target.lower():
                                route = v
                                break
                    from urllib.parse import urlparse
                    parsed = urlparse(self.page.url)
                    base_url = f"{parsed.scheme}://{parsed.netloc}"
                    selector_used = f"ODOO_MODULE_ROUTES['{action.target}'] → {route}"
                    await self.page.goto(f"{base_url}{route}")
                else:
                    locator = await self._resolve_locator(action.target)
                    # Fix #6: count elements before acting
                    try:
                        elements_found = await locator.count()
                    except Exception:
                        pass
                    if elements_found == 0:
                        # Zero elements found — try Odoo app icon click as fallback
                        app_icon = self.page.locator(f".o_app[data-menu-xmlid*='{action.target}'], a.o_app:has-text('{action.target}')")
                        try:
                            if await app_icon.count() > 0:
                                locator = app_icon.first
                                elements_found = await app_icon.count()
                        except Exception:
                            pass
                    await locator.click()
                try:
                    await self.page.wait_for_load_state("load", timeout=5000)
                except Exception:
                    pass  # Ignore non-blocking long-polling timeouts
                if "/web/login" in self.page.url:
                    return (
                        ExecutionResult(
                            step_id=step_id,
                            success=False,
                            message=f"Navigated to {action.target} but was redirected to Odoo login page: {self.page.url}"
                        ),
                        elements_found,
                        selector_used,
                    )
                return (
                    ExecutionResult(step_id=step_id, success=True, message=f"Navigated to {action.target}"),
                    elements_found,
                    selector_used,
                )

            elif action.type == ActionType.click:
                # ── Debug mode intercept ──────────────────────────────────────
                # Clicking the debug menu/mode icon is fragile (hover-reveal, may
                # not exist). Instead, activate debug mode reliably via URL param.
                if action.target in DEBUG_TARGETS or action.target.lower().strip() in {t.lower() for t in DEBUG_TARGETS}:
                    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
                    parsed = urlparse(self.page.url)
                    # Only append ?debug=1 if not already active
                    if "debug=1" not in (parsed.query or "") and "debug=assets" not in (parsed.query or ""):
                        separator = "&" if parsed.query else "?"
                        new_url = self.page.url.split("?")[0] + separator.replace("&", "?") + "debug=1"
                        # Rebuild properly
                        qs = parsed.query
                        new_qs = (qs + "&debug=1") if qs else "debug=1"
                        new_url = urlunparse(parsed._replace(query=new_qs))
                        await self.page.goto(new_url, timeout=15000)
                        try:
                            await self.page.wait_for_load_state("load", timeout=5000)
                        except Exception:
                            pass
                        selector_used = "URL ?debug=1 (debug-mode intercept)"
                        return (
                            ExecutionResult(step_id=step_id, success=True, message="Activated Odoo debug mode via ?debug=1 URL parameter"),
                            None,
                            selector_used,
                        )
                    else:
                        # Debug mode already active — nothing to do
                        selector_used = "URL ?debug=1 (already active)"
                        return (
                            ExecutionResult(step_id=step_id, success=True, message="Debug mode already active (?debug=1 present in URL)"),
                            None,
                            selector_used,
                        )

                locator = await self._resolve_locator(action.target)
                # Fix #6: count before acting
                try:
                    elements_found = await locator.count()
                except Exception:
                    pass

                # Graceful early-exit: if 0 elements found, try text-based fallback or route recovery
                if elements_found == 0:
                    try:
                        body_text = await self.page.locator("body").first.inner_text()
                        html_content = await self.page.content()
                    except Exception:
                        body_text = ""
                        html_content = ""
                    
                    target_str = action.target.strip()
                    if target_str and (target_str.lower() in body_text.lower() or target_str.lower() in html_content.lower()):
                        for fallback_loc in [
                            self.page.get_by_text(target_str, exact=False),
                            self.page.locator(f"text='{target_str}'")
                        ]:
                            try:
                                if await fallback_loc.count() > 0:
                                    first_loc = fallback_loc.first
                                    await first_loc.wait_for(state="visible", timeout=3000)
                                    await self._animate_cursor_to_element(first_loc)
                                    await first_loc.click()
                                    return (
                                        ExecutionResult(step_id=step_id, success=True, message=f"Clicked '{action.target}' (recovered via text-based locator fallback)"),
                                        await fallback_loc.count(),
                                        f"text-fallback: {target_str}",
                                    )
                            except Exception:
                                pass

                    # Route fallback: if the LLM tried to click a module name (e.g. "Companies") 
                    # but skipped opening the menu, we can recover by navigating directly.
                    route = None
                    for k, v in ODOO_MODULE_ROUTES.items():
                        if k.lower() == action.target.lower().strip():
                            route = v
                            break
                    
                    if route:
                        from urllib.parse import urlparse
                        parsed = urlparse(self.page.url)
                        base_url = f"{parsed.scheme}://{parsed.netloc}"
                        await self.page.goto(f"{base_url}{route}")
                        try:
                            await self.page.wait_for_load_state("load", timeout=5000)
                        except Exception:
                            pass
                        return (
                            ExecutionResult(
                                step_id=step_id, 
                                success=True, 
                                message=f"Element '{action.target}' not found to click, but recovered by navigating to route '{route}'"
                            ),
                            0,
                            f"ODOO_MODULE_ROUTES['{action.target}'] → {route} (click route recovery)",
                        )

                    return (
                        ExecutionResult(
                            step_id=step_id,
                            success=False,
                            message=f"Cannot click '{action.target}': element not found on current page ({self.page.url})"
                        ),
                        0,
                        selector_used,
                    )

                # Use .first to avoid strict-mode violations when selector matches multiple elements
                first_loc = locator.first
                await first_loc.wait_for(state="visible", timeout=5000)
                await self._animate_cursor_to_element(first_loc)
                await first_loc.click()
                return (
                    ExecutionResult(step_id=step_id, success=True, message=f"Clicked {action.target}"),
                    elements_found,
                    selector_used,
                )

            elif action.type == ActionType.input:
                locator = await self._resolve_locator(action.target)
                # Fix #6: count before acting
                try:
                    elements_found = await locator.count()
                except Exception:
                    pass

                # Graceful early-exit: if 0 elements found, try text-based fallback
                if elements_found == 0:
                    try:
                        body_text = await self.page.locator("body").first.inner_text()
                        html_content = await self.page.content()
                    except Exception:
                        body_text = ""
                        html_content = ""
                    
                    target_str = action.target.strip()
                    if target_str and (target_str.lower() in body_text.lower() or target_str.lower() in html_content.lower()):
                        for fallback_loc in [
                            self.page.get_by_text(target_str, exact=False),
                            self.page.locator(f"text='{target_str}'")
                        ]:
                            try:
                                if await fallback_loc.count() > 0:
                                    first_loc = fallback_loc.first
                                    await first_loc.wait_for(state="visible", timeout=3000)
                                    await self._animate_cursor_to_element(first_loc)
                                    await first_loc.fill(action.value)
                                    
                                    # Press Enter after filling search inputs
                                    _search_targets = {
                                        "search", "search bar", "search_bar", "search...", "search settings",
                                        "settings search", "search settings",
                                    }
                                    if (
                                        action.target.lower().strip() in _search_targets
                                        or "search" in action.target.lower()
                                        or "Search..." in action.target
                                    ):
                                        await first_loc.press("Enter")
                                        try:
                                            await self.page.wait_for_load_state("load", timeout=3000)
                                        except Exception:
                                            pass
                                    return (
                                        ExecutionResult(step_id=step_id, success=True, message=f"Input '{action.value}' into {action.target} (recovered via text-based locator fallback)"),
                                        await fallback_loc.count(),
                                        f"text-fallback: {target_str}",
                                    )
                            except Exception:
                                pass

                    return (
                        ExecutionResult(
                            step_id=step_id,
                            success=False,
                            message=f"Cannot input into '{action.target}': field not found on current page ({self.page.url})"
                        ),
                        0,
                        selector_used,
                    )

                # Use .first to avoid strict-mode violations when selector matches multiple elements
                first_loc = locator.first
                await first_loc.wait_for(state="visible", timeout=5000)
                await self._animate_cursor_to_element(first_loc)
                await first_loc.fill(action.value)
                
                # Press Enter after filling search inputs — Odoo search bars need a
                # Return keystroke to actually filter results (fill() alone is not enough).
                _search_targets = {
                    "search", "search bar", "search_bar", "search...", "search settings",
                    "settings search", "search settings",
                }
                if (
                    action.target.lower().strip() in _search_targets
                    or "search" in action.target.lower()
                    or "Search..." in action.target
                    or (selector_used and "searchview" in (selector_used or "").lower())
                    or (selector_used and "placeholder='Search" in (selector_used or ""))
                ):
                    await first_loc.press("Enter")
                    try:
                        await self.page.wait_for_load_state("load", timeout=3000)
                    except Exception:
                        pass
                
                return (
                    ExecutionResult(step_id=step_id, success=True, message=f"Input '{action.value}' into {action.target}"),
                    elements_found,
                    selector_used,
                )

            elif action.type == ActionType.extract:
                locator = await self._resolve_locator(action.target)
                # Fix #6: count before acting
                try:
                    elements_found = await locator.count()
                except Exception:
                    pass

                # Graceful early-exit: if 0 elements found, try text-based fallback
                if elements_found == 0:
                    try:
                        body_text = await self.page.locator("body").first.inner_text()
                        html_content = await self.page.content()
                    except Exception:
                        body_text = ""
                        html_content = ""
                    
                    target_str = action.target.strip()
                    if target_str and (target_str.lower() in body_text.lower() or target_str.lower() in html_content.lower()):
                        for fallback_loc in [
                            self.page.get_by_text(target_str, exact=False),
                            self.page.locator(f"text='{target_str}'")
                        ]:
                            try:
                                if await fallback_loc.count() > 0:
                                    first_loc = fallback_loc.first
                                    await first_loc.wait_for(state="visible", timeout=3000)
                                    await self._animate_cursor_to_element(first_loc)
                                    text = await first_loc.inner_text()
                                    return (
                                        ExecutionResult(step_id=step_id, success=True, message=f"Extracted from {action.target} (recovered via text-based locator fallback)", extracted_text=text),
                                        await fallback_loc.count(),
                                        f"text-fallback: {target_str}",
                                    )
                            except Exception:
                                pass

                    page_title = await self.page.title()
                    # Grab a truncated snapshot of visible text on the page
                    try:
                        body_text = await self.page.locator("body").first.inner_text()
                        body_snippet = body_text[:800].strip()
                    except Exception:
                        body_snippet = "(could not extract body text)"
                    fallback_text = (
                        f"[Target '{action.target}' not found on page. "
                        f"Page title: '{page_title}'. "
                        f"Visible content snippet: {body_snippet}]"
                    )
                    return (
                        ExecutionResult(
                            step_id=step_id,
                            success=True,
                            message=f"Target '{action.target}' not found — extracted page snapshot instead",
                            extracted_text=fallback_text,
                        ),
                        0,
                        selector_used,
                    )

                # Use .first to avoid strict-mode violations when selector matches multiple elements
                first_loc = locator.first
                await first_loc.wait_for(state="visible", timeout=5000)
                await self._animate_cursor_to_element(first_loc)
                text = await first_loc.inner_text()
                return (
                    ExecutionResult(step_id=step_id, success=True, message=f"Extracted from {action.target}", extracted_text=text),
                    elements_found,
                    selector_used,
                )

            elif action.type == ActionType.wait:
                try:
                    ms = int(action.target)
                    await self.page.wait_for_timeout(ms)
                    return (
                        ExecutionResult(step_id=step_id, success=True, message=f"Waited {ms}ms"),
                        None,
                        None,
                    )
                except ValueError:
                    return (
                        ExecutionResult(step_id=step_id, success=False, message=f"Invalid wait time: {action.target}"),
                        None,
                        None,
                    )

            elif action.type == ActionType.screenshot:
                bytes_data = await self.page.screenshot()
                path = f"output/{int(time.time())}_{action.target.replace(' ', '_')}.png"
                with open(path, "wb") as f:
                    f.write(bytes_data)
                return (
                    ExecutionResult(step_id=step_id, success=True, message=f"Screenshot saved to {path}", screenshot_path=path),
                    None,
                    None,
                )

            else:
                return (
                    ExecutionResult(step_id=step_id, success=False, message=f"Unknown action type: {action.type}"),
                    None,
                    None,
                )

        except PlaywrightTimeoutError:
            return (
                ExecutionResult(step_id=step_id, success=False, message=f"Timeout executing {action.type} on {action.target}"),
                elements_found,
                selector_used,
            )
        except Exception as e:
            return (
                ExecutionResult(step_id=step_id, success=False, message=f"Error executing {action.type}: {str(e)}"),
                elements_found,
                selector_used,
            )

    async def execute_step(self, step: Step) -> ExecutionResult:
        """Executes a full step, logging reasoning, timing, result, and retry trace."""
        logger.info(f"Executing step {step.id}: {step.intent}")

        # ── 3. REASONING TRACE ─────────────────────────────────────────────
        if self._run_logger:
            self._run_logger.log_step(
                step_id=step.id,
                intent=step.intent,
                reasoning=step.reasoning,
                action_type=step.action.type.value,
                action_target=step.action.target,
                expected_outcome=step.expected_outcome,
                fallback=step.fallback,
            )

        # ── 4. EXECUTION with Fix #7 retry trace ──────────────────────────
        MAX_RETRIES = 1  # one automatic retry for transient failures
        retry_attempts: List[Dict[str, Any]] = []
        result: Optional[ExecutionResult] = None
        elements_found: Optional[int] = None
        selector_used: Optional[str] = None
        duration_ms: float = 0.0

        for attempt in range(1, MAX_RETRIES + 2):  # attempts: 1, 2
            t0 = time.time()
            try:
                result, elements_found, selector_used = await self._execute_action(
                    step.action, step_id=step.id
                )
            except Exception as exc:
                duration_ms = (time.time() - t0) * 1000
                # Fix #9: pass selector to log_error
                if self._run_logger:
                    self._run_logger.log_error(
                        message=str(exc),
                        step_id=step.id,
                        exc=exc,
                        context="execute_step",
                        selector=SELECTOR_REGISTRY.get(step.action.target),
                    )
                result = ExecutionResult(
                    step_id=step.id,
                    success=False,
                    message=f"Unhandled exception: {exc}",
                )
                retry_attempts.append({"attempt": attempt, "status": "fail", "error": str(exc)})
                if attempt <= MAX_RETRIES:
                    await asyncio.sleep(1)
                    continue
                break
            else:
                duration_ms = (time.time() - t0) * 1000
                status = "success" if result.success else "fail"
                retry_attempts.append({"attempt": attempt, "status": status})
                if result.success or attempt > MAX_RETRIES:
                    break
                # transient failure — retry once
                await asyncio.sleep(1)

        result.step_id = step.id
        retry_count = len(retry_attempts) - 1  # retries = attempts beyond the first

        # ── Screenshot after each step ─────────────────────────────────────
        screenshot_path: Optional[str] = None
        if self._run_logger:
            screenshot_path = await self._run_logger.capture_screenshot(
                self.page, step.id
            )

        # ── Log action result with all evidence ────────────────────────────
        if self._run_logger:
            self._run_logger.log_action(
                step_id=step.id,
                action_type=step.action.type.value,
                target=step.action.target,
                selector_used=selector_used,
                success=result.success,
                message=result.message,
                duration_ms=duration_ms,
                retry_count=retry_count,
                retry_attempts=retry_attempts if retry_count > 0 else None,
                extracted_text=result.extracted_text,
                screenshot_path=screenshot_path,
                elements_found=elements_found,
            )

        # ── 6. BROWSER STATE after step ────────────────────────────────────
        if self._run_logger:
            self._run_logger.log_browser_state(
                url=self.page.url,
                step_id=step.id,
                screenshot_path=screenshot_path,
                event="post_step",
            )

        if not result.success:
            logger.warning(
                f"Step {step.id} failed: {result.message}. "
                f"Applying fallback reasoning: {step.fallback}"
            )
            result.message += f" | Fallback suggested: {step.fallback}"

        return result
