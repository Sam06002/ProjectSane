"""
server.py — FastAPI orchestrator for Project Sane v3.

Dual Cloud Engine routing:
  Engine 1 — Groq (llama-3.1-8b-instant)   : ticket triage + plan generation
  Engine 2 — Gemini (gemini-2.5-flash)      : multimodal blueprint + code generation

Observation Layer:
  ObservationLayer wraps RunLogger and emits live monitor_* SSE events
  for every error, warning, and milestone in real time.
"""

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

# Load .env before anything reads os.getenv()
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from ai_agent import AIAgent
from logger import RunLogger
from monitor import ObservationLayer, get_run_history, get_run_detail
from planner import Planner
from schema import ExecutionResult
from executor import ExecutionEngine
from browser_agent import BrowserManager
from stream_manager import StreamManager


# ── Async adapter for synchronous Groq streaming generators ──────────────────
async def _stream_to_sse(sync_generator):
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)

    def _collect_tokens():
        return list(sync_generator)

    tokens = await loop.run_in_executor(executor, _collect_tokens)
    for token in tokens:
        yield token


app = FastAPI(title="Project Sane v3")
templates = Jinja2Templates(directory="templates")

# Global CDP singleton — reused across requests
browser_manager = BrowserManager()


@app.on_event("startup")
async def startup_event():
    os.makedirs("output", exist_ok=True)
    os.makedirs("logs", exist_ok=True)


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


# ── Runs dashboard API ────────────────────────────────────────────────────────

@app.get("/api/runs")
async def list_runs():
    """Return the 20 most recent run summaries for the dashboard."""
    return JSONResponse(content=get_run_history(limit=20))


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    """Return the full structured JSON log for a specific run."""
    detail = get_run_detail(run_id)
    if detail is None:
        return JSONResponse(status_code=404, content={"error": "Run not found."})
    return JSONResponse(content=detail)


# ── Main pipeline ─────────────────────────────────────────────────────────────

@app.post("/api/run")
async def run_pipeline(request: Request):
    data = await request.json()
    ticket_text = data.get("ticket_text", "")
    raw_url = data.get("db_url", "https://shsri.odoo.com")

    # Normalise URL — auto-prepend https:// if omitted
    if raw_url and not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url
    db_url = raw_url

    # Validate URL — reject obviously invalid domains (e.g. "test", "localhost-only")
    # A valid Odoo SaaS URL must contain at least one dot (e.g. "shsri.odoo.com")
    from urllib.parse import urlparse as _urlparse
    _parsed_url = _urlparse(db_url)
    _hostname = _parsed_url.hostname or ""
    if not _hostname or ("." not in _hostname and _hostname != "localhost"):
        return JSONResponse(
            status_code=422,
            content={
                "error": f"Invalid database URL: '{db_url}'. Please provide a valid Odoo domain (e.g. 'yourcompany.odoo.com')."
            }
        )

    # Reject non-customer Odoo domains — www.odoo.com, runbot.odoo.com, preview.odoo.com etc.
    # are marketing/infrastructure sites, not SaaS customer databases.
    _RESERVED_ODOO_SUBDOMAINS = {"www", "runbot", "preview", "staging", "demo", "mail", "download", "cdn"}
    _hostname_lower = _hostname.lower()
    _parts = _hostname_lower.split(".")
    if len(_parts) >= 3 and _parts[-2] == "odoo" and _parts[-1] == "com":
        _subdomain_part = _parts[0]  # e.g. "www" from "www.odoo.com"
        if _subdomain_part in _RESERVED_ODOO_SUBDOMAINS:
            return JSONResponse(
                status_code=422,
                content={
                    "error": (
                        f"'{db_url}' is not a customer Odoo database. "
                        f"Please provide your company's Odoo URL (e.g. 'yourcompany.odoo.com')."
                    )
                }
            )

    # Extract production base URL (for support gateway redirection)
    if "-support-" in db_url.lower():
        subdomain = db_url.split("//")[-1].split(".")[0]
        prod_subdomain = subdomain.split("-support-")[0]
        domain_suffix = db_url.split("//")[-1].split(".", 1)[-1]
        prod_base_url = f"https://{prod_subdomain}.{domain_suffix}"
    else:
        prod_base_url = db_url

    async def event_generator():
        nonlocal db_url, prod_base_url
        # ── Thinking token SSE helper ─────────────────────────────────────────
        def thinking_sse(step: int, token: str) -> str:
            return f"data: {json.dumps({'type': 'thinking', 'step': step, 'token': token})}\n\n"

        # ── Initialise structured logger ──────────────────────────────────────
        run_log = RunLogger()

        # ── Initialise observation layer ──────────────────────────────────────
        # The emitter queues raw SSE strings into a local buffer that we yield below.
        _monitor_queue: list = []

        async def _sse_emitter(msg: str):
            _monitor_queue.append(msg)

        obs = ObservationLayer(
            run_logger=run_log,
            sse_emitter=_sse_emitter,
            ticket_text=ticket_text,
            db_url=db_url,
        )

        async def flush_monitor():
            """Yield all queued monitor SSE events to the client."""
            while _monitor_queue:
                yield _monitor_queue.pop(0)

        # ── API keys ──────────────────────────────────────────────────────────
        groq_key   = os.getenv("GROQ_API_KEY", "")
        gemini_key = os.getenv("GEMINI_API_KEY", "")

        # ── Dual Cloud Engine initialisation ──────────────────────────────────
        ai = AIAgent(groq_api_key=groq_key, gemini_api_key=gemini_key)
        planner = Planner(api_key=gemini_key)

        yield StreamManager.emit_thinking(
            0, "Initialization",
            "Dual Cloud Engine ready — Engine 1 (Groq) + Engine 2 (Gemini) | Observation Layer active"
        )
        await obs.record_event("Initialization", "Dual Cloud Engine + Observation Layer started")
        async for msg in flush_monitor():
            yield msg

        # ── Browser start ─────────────────────────────────────────────────────
        try:
            page = await browser_manager.start()
            await obs.record_event("Browser ready", f"CDP connected on port 9225")
        except Exception as e:
            await obs.on_browser_error(str(e), context="browser_start")
            async for msg in flush_monitor():
                yield msg
            run_log.save()
            obs.finalise()
            yield StreamManager.emit_error(f"Failed to connect to Browser: {e}")
            return
        async for msg in flush_monitor():
            yield msg

        # ── Initial navigation & Duplication Gate ─────────────────────────────
        is_production = "-support-" not in db_url.lower() and not ("/odoo" in db_url or "/web" in db_url)
        
        target_nav_url = f"{db_url.rstrip('/')}/_odoo/support" if is_production else (
            db_url if "/odoo" in db_url or "/web" in db_url else f"{db_url.rstrip('/')}/odoo"
        )
        
        yield StreamManager.emit_thinking(
            0, 
            "Navigation", 
            f"Production database detected. Routing to support gateway at {target_nav_url}..." if is_production 
            else f"Connecting to database at {target_nav_url}..."
        )
        
        try:
            # Helper for Odoo support gateway login reason field
            async def handle_support_login(page_obj):
                if "/support/login" in page_obj.url:
                    yield StreamManager.emit_thinking(0, "Authentication", "Handling support gateway login reason field...")
                    reason_selectors = [
                        "input[name='reason']",
                        "textarea[name='reason']",
                        "input[id='reason']",
                        "textarea[id='reason']",
                        "input[placeholder*='reason']",
                        "textarea[placeholder*='reason']",
                        "[name='reason']"
                    ]
                    reason_input = None
                    for sel in reason_selectors:
                        if await page_obj.locator(sel).count() > 0:
                            reason_input = page_obj.locator(sel).first
                            break
                    if reason_input:
                        await reason_input.fill("testing")
                    else:
                        inputs = page_obj.locator("input, textarea")
                        for i in range(await inputs.count()):
                            inp = inputs.nth(i)
                            if await inp.is_visible():
                                await inp.fill("testing")
                                break
                    
                    submit_button = None
                    submit_selectors = [
                        "button[type='submit']",
                        "input[type='submit']",
                        "button:has-text('Submit')",
                        "button:has-text('Login')",
                        "button:has-text('Confirm')",
                        "button:has-text('Ok')"
                    ]
                    for sel in submit_selectors:
                        if await page_obj.locator(sel).count() > 0:
                            submit_button = page_obj.locator(sel).first
                            break
                    if submit_button:
                        await submit_button.click()
                    else:
                        await page_obj.keyboard.press("Enter")
                    
                    try:
                        await page_obj.wait_for_load_state("load", timeout=10000)
                    except Exception:
                        pass

            # Helper to authenticate via support gateway
            async def authenticate_via_support_gateway(active_target_url):
                from urllib.parse import urlparse
                parsed = urlparse(active_target_url)
                target_base = f"{parsed.scheme}://{parsed.netloc}"
                
                # Try target's own support gateway first (self-healing, direct auth)
                # If target is already production, target_base is same as prod_base_url
                gateways_to_try = [f"{target_base.rstrip('/')}/_odoo/support"]
                if target_base.lower() != prod_base_url.lower():
                    gateways_to_try.append(f"{prod_base_url.rstrip('/')}/_odoo/support")
                
                auth_success = False
                for gateway_url in gateways_to_try:
                    try:
                        yield StreamManager.emit_thinking(0, "Authentication", f"Navigating to support gateway: {gateway_url}...")
                        await page.goto(gateway_url, timeout=30000)
                        
                        async for msg in handle_support_login(page):
                            yield msg
                        
                        # Find the database link
                        db_link = None
                        
                        # If active_target_url has duplicate subdomain, search for it first
                        if "-support-" in active_target_url.lower():
                            dup_subdomain = active_target_url.split("//")[-1].split(".")[0]
                            for pattern in [f"a[href*='{dup_subdomain}']", f"a:has-text('{dup_subdomain}')"]:
                                loc = page.locator(pattern)
                                if await loc.count() > 0:
                                    db_link = loc.first
                                    yield StreamManager.emit_thinking(0, "Gateway", f"Found duplicate database link: '{dup_subdomain}'")
                                    break
                        
                        # Fallback database name link detection
                        if not db_link:
                            db_name = prod_base_url.split("//")[-1].split(".")[0]
                            db_link_selectors = [
                                f"a:has-text('{db_name}')",
                                "a:has-text('database')",
                                "a:has-text('enter')",
                                "a:has-text('connect')",
                                ".o_database_link",
                                "a[href*='/web']"
                            ]
                            for sel in db_link_selectors:
                                if await page.locator(sel).count() > 0:
                                    db_link = page.locator(sel).first
                                    break
                        
                        if db_link:
                            link_href = await db_link.get_attribute("href")
                            yield StreamManager.emit_thinking(0, "Gateway", f"Clicking database link on gateway: {link_href}...")
                            await db_link.click()
                            try:
                                await page.wait_for_load_state("load", timeout=15000)
                            except Exception:
                                pass
                            
                            # Check if we successfully logged in and escaped web/login
                            if "/web/login" not in page.url:
                                auth_success = True
                                break
                            else:
                                yield StreamManager.emit_thinking(0, "Gateway", "Database link clicked but page still redirected to login. Trying fallback...")
                        else:
                            # Direct navigate fallback
                            admin_url_direct = f"{active_target_url.rstrip('/')}/odoo"
                            yield StreamManager.emit_thinking(0, "Gateway", f"No database link found on gateway. Trying direct navigation to: {admin_url_direct}...")
                            await page.goto(admin_url_direct, timeout=30000)
                            if "/web/login" not in page.url:
                                auth_success = True
                                break
                    except Exception as e:
                        yield StreamManager.emit_thinking(0, "Gateway", f"Error trying support gateway {gateway_url}: {e}")
                
                if not auth_success and "/web/login" in page.url:
                    yield StreamManager.emit_thinking(0, "Gateway", "All support gateway authentication attempts exhausted. Page remains on login screen.")

            await page.goto(target_nav_url, timeout=30000)
            
            # Detect Odoo login page redirect
            if "/web/login" in page.url:
                await obs.record_warning(
                    "Odoo login page detected. Session cookies may be expired or missing. Initiating automatic cookie sync...",
                    context="initial_navigation"
                )
                yield StreamManager.emit_thinking(
                    0, "Cookie Sync",
                    "Odoo login page detected. Performing self-healing session cookie sync..."
                )
                
                # Perform clean browser reset & fresh cookie sync launch
                await browser_manager.hard_reset()
                page = await browser_manager.start()
                
                yield StreamManager.emit_thinking(0, "Navigation Retry", f"Re-connecting to {target_nav_url}...")
                await page.goto(target_nav_url, timeout=30000)
                
                if "/web/login" in page.url:
                    # If cookie sync fails on duplicate subdomain or target, try using the support gateway for self-healing!
                    yield StreamManager.emit_thinking(0, "Gateway Authentication", "Attempting self-healing support gateway authentication...")
                    async for msg in authenticate_via_support_gateway(db_url):
                        yield msg
                    
                    if "/web/login" in page.url:
                        await obs.record_error(
                            "Cookie sync and gateway login completed, but Odoo still redirected to login. Please ensure you are logged in to Odoo on your main Chrome 'Work' profile (Profile 3) and try again.",
                            context="initial_navigation"
                        )
                        async for msg in flush_monitor():
                            yield msg
                        run_log.save()
                        obs.finalise()
                        yield StreamManager.emit_error("Authentication failed. Please login to your Odoo account on your Work profile first.")
                        await browser_manager.stop()
                        return
            
            # Execute duplication flow if production target
            if is_production:
                # Check support login on gateway
                async for msg in handle_support_login(page):
                    yield msg
                
                # ── Diagnostic screenshot: what does the gateway page look like? ──
                try:
                    diag_path = f"output/gateway_diagnostic_{int(time.time())}.png"
                    await page.screenshot(path=diag_path)
                    yield StreamManager.emit_thinking(0, "Gateway", f"Diagnostic screenshot saved: {diag_path}")
                except Exception:
                    pass
                
                # ── Helper: Scan page for any duplicate/neutralized database links ──
                async def find_duplicate_link():
                    """Scans the current page for duplicate database links using multiple strategies."""
                    # Strategy 1: Links with 'support-' in href (standard Odoo pattern)
                    for pattern in ["a[href*='support-']", "a[href*='neutralized']", "a[href*='copy']"]:
                        loc = page.locator(pattern)
                        if await loc.count() > 0:
                            return loc.first, f"pattern: {pattern}"
                    
                    # Strategy 2: Links containing the prod subdomain + extra segments
                    prod_sub = prod_base_url.split("//")[-1].split(".")[0]
                    link_loc = page.locator(f"a[href*='{prod_sub}-']")
                    if await link_loc.count() > 0:
                        return link_loc.first, f"prod-subdomain-dash: {prod_sub}-*"
                    
                    # Strategy 3: Scan all links for any .odoo.com domain that is a duplicate of the production one
                    # A duplicate MUST start with the production subdomain followed by a dash.
                    all_links = page.locator("a[href*='.odoo.com']")
                    link_count = await all_links.count()
                    for i in range(link_count):
                        href = await all_links.nth(i).get_attribute("href") or ""
                        href_sub = href.split("//")[-1].split(".")[0] if "//" in href else ""
                        # It's a valid duplicate only if it starts with prod_sub + '-'
                        # (e.g. "shsri-support-20260524" for prod_sub "shsri")
                        if href_sub and href_sub.startswith(f"{prod_sub}-") and "odoo.com" in href:
                            return all_links.nth(i), f"prod-subdomain-prefix: {href_sub}"
                    
                    # Strategy 4: Text-based detection
                    for text_pattern in ["Duplicate", "Neutralized copy", "Test database", "Copy of"]:
                        text_loc = page.locator(f"a:has-text('{text_pattern}')")
                        if await text_loc.count() > 0:
                            return text_loc.first, f"text: {text_pattern}"
                    
                    return None, None
                
                # ── Check for existing duplicates ──
                dup_link, match_strategy = await find_duplicate_link()
                
                duplicate_chosen = None
                if dup_link:
                    duplicate_chosen = await dup_link.get_attribute("href")
                    yield StreamManager.emit_thinking(0, "Gateway", f"Found existing duplicate via {match_strategy}: {duplicate_chosen}. Clicking to enter...")
                    await dup_link.click()
                    try:
                        await page.wait_for_load_state("load", timeout=15000)
                    except Exception:
                        pass
                else:
                    # ── Click Duplicate/Neutralize button ──
                    dup_btn_selectors = [
                        "button:has-text('Duplicate')",
                        "a:has-text('Duplicate')",
                        "button:has-text('Neutralize')",
                        "a:has-text('Neutralize')",
                        "button:has-text('Create a copy')",
                        "a:has-text('Create a copy')",
                        "button:has-text('Copy')",
                        ".o_btn_duplicate",
                        "button[name='duplicate']",
                        "input[type='submit'][value*='Duplicate']",
                        "input[type='submit'][value*='Neutralize']",
                        # Broader: any submit/action button in the form
                        "form button[type='submit']",
                    ]
                    dup_btn = None
                    for sel in dup_btn_selectors:
                        try:
                            if await page.locator(sel).count() > 0:
                                btn = page.locator(sel).first
                                if await btn.is_visible():
                                    dup_btn = btn
                                    yield StreamManager.emit_thinking(0, "Gateway", f"Found duplicate button via: {sel}")
                                    break
                        except Exception:
                            pass
                    
                    if dup_btn:
                        yield StreamManager.emit_thinking(0, "Gateway", "No existing duplicate found. Clicking to create one...")
                        await dup_btn.click()
                        
                        # Wait briefly for any confirmation dialog
                        try:
                            confirm = page.locator(".modal-dialog button:has-text('Ok'), .modal-dialog button:has-text('Confirm'), .modal-dialog button:has-text('Yes')")
                            await confirm.first.wait_for(state="visible", timeout=3000)
                            await confirm.first.click()
                        except Exception:
                            pass  # No confirmation dialog — that's fine
                        
                        # ── Fast-path: Odoo may navigate directly to the duplicate right after the click ──
                        # Give it a few seconds to navigate, then check if we've already landed on the duplicate.
                        found_dup = False
                        import time as _time
                        
                        try:
                            await page.wait_for_load_state("load", timeout=8000)
                        except Exception:
                            pass
                        
                        prod_sub = prod_base_url.split("//")[-1].split(".")[0]
                        current_sub = page.url.split("//")[-1].split(".")[0] if "//" in page.url else ""
                        if current_sub.startswith(f"{prod_sub}-") and current_sub != prod_sub:
                            # Browser already landed on the duplicate — no polling needed
                            duplicate_chosen = page.url
                            yield StreamManager.emit_thinking(
                                0, "Gateway",
                                f"Duplicate database ready immediately — browser already at: {page.url}. Skipping poll."
                            )
                            found_dup = True
                        
                        if not found_dup:
                            # ── Polling: navigate back to the support gateway each time ──
                            # We must go back to target_nav_url before scanning; reloading the
                            # current page is wrong when Odoo has already navigated us to the
                            # duplicate's /web/login page.
                            yield StreamManager.emit_thinking(0, "Gateway", "Waiting for database duplication to complete (polling up to 180s)...")
                            poll_start = _time.time()
                            
                            for poll_attempt in range(36):  # 36 × 5s = 180s (3 minutes)
                                elapsed = int(_time.time() - poll_start)
                                await asyncio.sleep(5)
                                
                                yield StreamManager.emit_thinking(0, "Gateway", f"Polling attempt {poll_attempt + 1}/36 ({elapsed}s elapsed)...")
                                
                                # Always navigate back to the support gateway before scanning.
                                # Do NOT reload the current page — it may be the duplicate's login page.
                                try:
                                    await page.goto(target_nav_url, timeout=15000)
                                    await page.wait_for_load_state("load", timeout=8000)
                                except Exception:
                                    pass
                                
                                # Re-handle support login if the gateway requires it again
                                async for msg in handle_support_login(page):
                                    yield msg
                                
                                # Scan for the new duplicate
                                dup_link, match_strategy = await find_duplicate_link()
                                if dup_link:
                                    duplicate_chosen = await dup_link.get_attribute("href")
                                    yield StreamManager.emit_thinking(0, "Gateway", f"Duplicate database ready via {match_strategy}: {duplicate_chosen} (after {elapsed}s). Entering...")
                                    await dup_link.click()
                                    try:
                                        await page.wait_for_load_state("load", timeout=15000)
                                    except Exception:
                                        pass
                                    found_dup = True
                                    break
                                
                                # Check for progress/status indicators
                                try:
                                    progress_texts = await page.inner_text("body")
                                    if any(kw in progress_texts.lower() for kw in ["duplicating", "copying", "in progress", "creating", "please wait"]):
                                        yield StreamManager.emit_thinking(0, "Gateway", f"Duplication still in progress ({elapsed}s)... continuing to poll.")
                                except Exception:
                                    pass
                        
                        if not found_dup:
                            # Final diagnostic screenshot before failing
                            try:
                                fail_path = f"output/duplication_failed_{int(time.time())}.png"
                                await page.screenshot(path=fail_path)
                                yield StreamManager.emit_thinking(0, "Gateway", f"Duplication failure screenshot: {fail_path}")
                            except Exception:
                                pass
                            raise RuntimeError(
                                "Duplication timed out after 180 seconds. "
                                "The database may be too large or the support gateway did not create a visible duplicate link. "
                                "Check output/duplication_failed_*.png for diagnostic screenshots."
                            )
                    else:
                        # No button found — take diagnostic screenshot and give clear error
                        try:
                            no_btn_path = f"output/no_dup_button_{int(time.time())}.png"
                            await page.screenshot(path=no_btn_path)
                            yield StreamManager.emit_thinking(0, "Gateway", f"No duplicate button found. Screenshot: {no_btn_path}")
                        except Exception:
                            pass
                        raise RuntimeError(
                            "No existing duplicate link or 'Duplicate'/'Neutralize' button found on support gateway. "
                            "Check output/no_dup_button_*.png for a screenshot of what the gateway page looks like."
                        )
                
                # Navigate to the duplicate support gateway to login and click on database link (rather than direct navigation)
                from urllib.parse import urlparse
                parsed = urlparse(page.url)
                duplicate_base = f"{parsed.scheme}://{parsed.netloc}"
                yield StreamManager.emit_thinking(0, "Gateway", f"Navigating to duplicate's support gateway for safe entry...")
                async for msg in authenticate_via_support_gateway(duplicate_base):
                    yield msg
                
                # Update context URLs to Neutralized Duplicate DB
                db_url = duplicate_base
                obs.db_url = db_url
            else:
                admin_url = target_nav_url
            
            run_log.log_browser_state(url=page.url, step_id=None, event="initial_navigation")
            await obs.record_event("Navigation complete", f"Loaded neutralized Odoo admin dashboard")
            
            # ── Transition Frontend Portal to Backend Dashboard ───────────────
            # Check if we landed on the frontend website/portal instead of backend dashboard (Image 1 & 2).
            # If cookies/cache are saved and the user is logged in, they might land on the portal.
            # We run a robust state machine to click the top-left arrow first (to go to edit/backend mode)
            # and then click the all apps grid icon to reach the backend apps dashboard.
            arrow_selectors = [
                "a.o_frontend_to_backend",
                ".o_frontend_to_backend",
                "a[title*='Backend']",
                "a[title*='Edit']",
                "a[href*='/web']"
            ]
            grid_selectors = [
                "a[title='Go to your Odoo Apps']",
                "[title='Go to your Odoo Apps']",
                "a.o_app_drawer_toggle",
                ".o_app_drawer_toggle",
                ".o_menu_toggle",
                "a:has-text('Go to your Odoo Apps')"
            ]

            transition_attempts = 0
            while transition_attempts < 5:
                transition_attempts += 1
                arrow_clicked = False
                for sel in arrow_selectors:
                    loc = page.locator(sel)
                    try:
                        if await loc.count() > 0 and await loc.first.is_visible():
                            yield StreamManager.emit_thinking(0, "Portal Transition", f"Website frontend portal detected. Clicking top-left arrow ({sel}) to switch to editor mode...")
                            await loc.first.click()
                            try:
                                await page.wait_for_load_state("load", timeout=3000)
                            except Exception:
                                pass
                            arrow_clicked = True
                            break
                    except Exception:
                        pass

                grid_clicked = False
                for sel in grid_selectors:
                    loc = page.locator(sel)
                    try:
                        if await loc.count() > 0 and await loc.first.is_visible():
                            yield StreamManager.emit_thinking(0, "Portal Transition", f"Clicking Odoo Apps grid icon ({sel}) to open backend apps dashboard...")
                            await loc.first.click()
                            try:
                                await page.wait_for_load_state("load", timeout=5000)
                            except Exception:
                                pass
                            grid_clicked = True
                            break
                    except Exception:
                        pass

                if grid_clicked:
                    yield StreamManager.emit_thinking(0, "Portal Transition", "Successfully reached backend apps dashboard.")
                    break
                if not arrow_clicked and not grid_clicked:
                    if "/odoo" in page.url or "/web" in page.url:
                        break
                    await page.wait_for_timeout(1000)
            
            # ── Environment & Neutralization Detection ────────────────────────
            is_neutralized = False
            try:
                neutral_selectors = [
                    ".o_neutralize_banner",
                    ".database_neutralized",
                    ".o_test_mode_banner",
                    ".o_ribbon:has-text('Neutralized')",
                    ".o_ribbon:has-text('Test')"
                ]
                for sel in neutral_selectors:
                    if await page.locator(sel).count() > 0:
                        is_neutralized = True
                        break
                
                if not is_neutralized:
                    body_content = await page.inner_text("body")
                    neutral_triggers = ["neutralized", "neutralize", "this is a test database"]
                    is_neutralized = any(trigger in body_content.lower() for trigger in neutral_triggers)
            except Exception:
                pass

            if is_neutralized:
                await obs.record_event(
                    "Environment: Neutralized Staging",
                    "Database is in Neutralized Test Mode (safe for automation)."
                )
            else:
                await obs.record_warning(
                    "LIVE PRODUCTION DATABASE DETECTED! No Odoo neutralization banner found. "
                    "Any write actions could affect real operational data. Handle with extreme caution!",
                    context="environment_check"
                )
        except Exception as e:
            await obs.on_browser_error(str(e), context="initial_navigation")
            async for msg in flush_monitor():
                yield msg
            run_log.save()
            obs.finalise()
            yield StreamManager.emit_error(f"Failed to load database URL: {e}")
            await browser_manager.stop()
            return
        async for msg in flush_monitor():
            yield msg

        # ── Engine 1: Streaming ticket analysis ───────────────────────────────
        yield StreamManager.emit_thinking(
            0, "Analysing ticket",
            "Engine 1 (Groq / llama-3.1-8b-instant) extracting ticket metadata..."
        )
        await obs.on_engine1_call("ticket_analysis")
        async for msg in flush_monitor():
            yield msg

        full_response = ""
        async for token in _stream_to_sse(
            ai.analyse_ticket_stream(ticket_text, run_logger=run_log)
        ):
            yield thinking_sse(1, token)
            full_response += token
            await asyncio.sleep(0)

        ticket_info = ai._parse_ticket_json(full_response)

        if data.get("odoo_version") and not ticket_info.get("odoo_version"):
            ticket_info["odoo_version"] = data["odoo_version"]

        run_log.log_input(ticket_text=ticket_text, db_url=db_url, extracted=ticket_info)

        module  = ticket_info.get("module") or "unknown"
        version = ticket_info.get("odoo_version") or "unknown"

        if module == "unknown":
            await obs.record_warning(
                "Module could not be extracted from ticket — ticket may lack explicit module mention.",
                context="ticket_analysis"
            )

        yield StreamManager.emit_thinking(
            0, "Ticket analysed",
            f"Module: {module} | Version: {version}"
        )
        await obs.record_event("Ticket analysed", f"Module={module} Version={version}")
        async for msg in flush_monitor():
            yield msg

        # ── Engine 1: Streaming plan generation ───────────────────────────────
        yield StreamManager.emit_thinking(
            0, "Generating investigation plan",
            "Engine 1 (Groq) generating step-by-step browser investigation plan..."
        )
        await obs.on_engine1_call("plan_generation")
        async for msg in flush_monitor():
            yield msg

        plan_full = ""
        async for token in _stream_to_sse(
            ai.generate_plan_stream(ticket_text, ticket_info, run_logger=run_log)
        ):
            yield thinking_sse(2, token)
            plan_full += token
            await asyncio.sleep(0)

        # ── Planner: Deterministic JSON execution plan ────────────────────────
        yield StreamManager.emit_thinking(0, "Planning", "Building deterministic JSON execution plan...")
        try:
            plan = await planner.generate_plan(ticket_text, db_url)
            await obs.record_event("Plan validated", f"Module={plan.module} Confidence={plan.confidence:.2f}")
        except Exception as e:
            await obs.on_planner_error(str(e))
            async for msg in flush_monitor():
                yield msg
            run_log.save()
            obs.finalise()
            yield StreamManager.emit_error(f"Planning failed: {e}")
            await browser_manager.stop()
            return
        async for msg in flush_monitor():
            yield msg

        yield StreamManager.emit_thinking(
            0, "Plan Validated",
            f"Detected module {plan.module}. Confidence: {plan.confidence:.2f}"
        )

        try:
            run_log.log_plan(plan.dict())
        except Exception:
            run_log.log_plan({"error": "plan.dict() failed"})

        # ── Confidence gate ───────────────────────────────────────────────────
        if plan.confidence < 0.6:
            run_log.log_execution_gate(
                confidence=plan.confidence, threshold=0.6,
                executed=False, reason="Confidence below threshold"
            )
            await obs.record_warning(
                f"Confidence {plan.confidence:.2f} is below 0.6 — execution skipped.",
                context="confidence_gate"
            )
            async for msg in flush_monitor():
                yield msg
            summary = obs.finalise()
            yield StreamManager.emit_error("Confidence too low (< 0.6). Analysis only.")
            yield StreamManager.emit_summary({
                "ticket_summary": plan.summary, "module": plan.module,
                "confidence": plan.confidence, "steps_total": len(plan.steps),
                "steps_succeeded": 0, "steps_failed": 0, "was_executed": False,
                "skip_reason": "Low confidence", "results": [],
                "findings": ["Aborted before execution due to low confidence."],
                "recommendation": "Manual investigation required.",
                "monitor_summary": summary,
            })
            run_log.save()
            await browser_manager.stop()
            return

        run_log.log_execution_gate(
            confidence=plan.confidence, threshold=0.6,
            executed=True, reason="Confidence above threshold"
        )

        # ── Execution loop ────────────────────────────────────────────────────
        engine = ExecutionEngine(page, run_logger=run_log)
        results = []
        findings = []

        for step in plan.steps:
            # ── Session Recovery ──
            # Detect session expiration / redirect to login page before executing the step
            if "/web/login" in page.url:
                yield StreamManager.emit_thinking(step.id, "Session Recovery", "Odoo login page detected. Session may have expired. Recovering session via support gateway...")
                async for msg in authenticate_via_support_gateway(db_url):
                    yield msg
                
                # Navigate back to the backend apps page if possible
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(db_url)
                    base_url = f"{parsed.scheme}://{parsed.netloc}"
                    yield StreamManager.emit_thinking(step.id, "Session Recovery", f"Returning to database dashboard at {base_url}...")
                    await page.goto(f"{base_url}/odoo", timeout=20000)
                except Exception:
                    pass

            yield StreamManager.emit_thinking(step.id, step.intent, step.reasoning)
            await asyncio.sleep(0.5)

            yield StreamManager.emit_action_start(step.id, step.action.type.value, step.action.target)

            res: ExecutionResult = await engine.execute_step(step)

            # If the step failed, check if we were redirected to Odoo login page during the action
            if not res.success and "/web/login" in page.url:
                yield StreamManager.emit_thinking(step.id, "Session Recovery", "Action failed due to Odoo login redirect. Recovering session and retrying step...")
                async for msg in authenticate_via_support_gateway(db_url):
                    yield msg
                
                # Re-attempt the step after login recovery
                res = await engine.execute_step(step)

            results.append(res)

            yield StreamManager.emit_action_result(step.id, res.success, res.message, res.extracted_text)

            if res.extracted_text:
                findings.append(f"Step {step.id} extracted: {res.extracted_text}")

            if not res.success:
                await obs.on_step_failure(step.id, res.message)
                async for msg in flush_monitor():
                    yield msg
                yield StreamManager.emit_error(f"Step {step.id} failed: {res.message}")
                break

            await asyncio.sleep(1)

        # ── Cleanup & report ──────────────────────────────────────────────────
        await browser_manager.stop()

        success_count = sum(1 for r in results if r.success)
        monitor_summary = obs.finalise()
        log_path = run_log.save()

        report = {
            "ticket_summary": plan.summary,
            "module": plan.module,
            "confidence": plan.confidence,
            "steps_total": len(plan.steps),
            "steps_succeeded": success_count,
            "steps_failed": len(results) - success_count,
            "was_executed": True,
            "skip_reason": None,
            "results": [r.dict() for r in results],
            "findings": findings,
            "recommendation": (
                "Execution completed successfully."
                if success_count == len(plan.steps)
                else "Execution failed midway — see monitor_summary for errors."
            ),
            "log_path": log_path,
            "monitor_summary": monitor_summary,
        }

        # Flush any remaining monitor events before the final summary
        async for msg in flush_monitor():
            yield msg

        yield StreamManager.emit_summary(report)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
