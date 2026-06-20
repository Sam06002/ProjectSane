import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from run_state import RunState
from exceptions import (
    BaseProjectSaneException,
    AuthenticationError,
    PlanningError,
    ExecutionError,
    BrowserError,
    DuplicationError
)
import odoo_selectors as selectors
from browser_agent import ensure_demo_overlay, human_like_click_locator, human_like_fill
from demo_mode import demo_settings
from db_utils import assert_duplicate_database, is_duplicate_database

logger = logging.getLogger(__name__)

# Global CDP browser manager
from browser_agent import BrowserManager
browser_manager = BrowserManager()


class BackgroundJob:
    """Represents a single long-running pipeline execution run."""
    def __init__(self, run_id: str, ticket_text: str, db_url: str, odoo_version: Optional[str] = None):
        self.run_id: str = run_id
        self.ticket_text: str = ticket_text
        self.db_url: str = db_url
        self.odoo_version: Optional[str] = odoo_version
        self.state: RunState = RunState.CREATED
        self.queue: asyncio.Queue = asyncio.Queue()
        self.history: List[str] = []
        self.error: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.started_at: datetime = datetime.now(timezone.utc)
        self.finished_at: Optional[datetime] = None
        self.last_heartbeat_at: datetime = datetime.now(timezone.utc)

    def update_heartbeat(self) -> None:
        """Updates the timestamp of the last active progress update."""
        self.last_heartbeat_at = datetime.now(timezone.utc)

    async def transition_to(self, new_state: RunState) -> None:
        """Transitions the job to a new state, logging and emitting the event."""
        self.update_heartbeat()
        old_state = self.state
        self.state = new_state
        log_msg = f"Job {self.run_id} transitioned: {old_state.value} -> {new_state.value}"
        logger.info(log_msg)
        
        # Emit state change SSE
        await self.emit_event("state_change", {
            "run_id": self.run_id,
            "old_state": old_state.value,
            "new_state": new_state.value,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    async def emit_raw(self, msg: str) -> None:
        """Pushes a raw SSE string payload to the client queue and history."""
        self.update_heartbeat()
        self.history.append(msg)
        await self.queue.put(msg)

    async def emit_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Formats and pushes a structured SSE event to the queue."""
        msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        await self.emit_raw(msg)


class JobManager:
    """Orchestrates and tracks asynchronous execution of Project Sane jobs."""
    _jobs: Dict[str, BackgroundJob] = {}
    _job_tasks: Dict[str, asyncio.Task] = {}
    _watchdog_task: Optional[asyncio.Task] = None

    @classmethod
    def create_job(cls, ticket_text: str, db_url: str, odoo_version: Optional[str] = None) -> BackgroundJob:
        """Creates a new job with a unique run_id and registers it."""
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        job = BackgroundJob(run_id, ticket_text, db_url, odoo_version)
        cls._jobs[run_id] = job
        logger.info(f"Created job {run_id} for database: {db_url}")
        return job

    @classmethod
    def get_job(cls, run_id: str) -> Optional[BackgroundJob]:
        """Retrieves a job by its unique run_id."""
        return cls._jobs.get(run_id)

    @classmethod
    def get_all_jobs(cls) -> List[BackgroundJob]:
        """Returns all registered jobs, sorted by start time descending."""
        return sorted(cls._jobs.values(), key=lambda j: j.started_at, reverse=True)

    @classmethod
    async def start_job(cls, job: BackgroundJob) -> None:
        """Starts the background worker executing the job's pipeline."""
        await job.transition_to(RunState.QUEUED)
        task = asyncio.create_task(cls._execute_job_pipeline(job))
        cls._job_tasks[job.run_id] = task
        # Ensure watchdog is running
        cls.start_watchdog()

    @classmethod
    def start_watchdog(cls) -> None:
        """Starts the background watchdog loop if not already running."""
        if cls._watchdog_task is None or cls._watchdog_task.done():
            cls._watchdog_task = asyncio.create_task(cls._watchdog_loop())
            logger.info("Background watchdog sweeper started in JobManager.")

    @classmethod
    async def _finalize_job(
        cls,
        job: BackgroundJob,
        terminal_state: RunState,
        reason: str,
        *,
        obs=None,
        run_log=None,
    ) -> None:
        """
        Single terminal path for all job endings (COMPLETED, FAILED, CANCELLED, TIMED_OUT).
        Idempotent: if job is already in a terminal state, returns immediately.
        Responsibilities:
          - sets job.error
          - persists obs/run_log if available
          - emits a structured error SSE so the frontend always sees a terminal event
          - transitions the job state exactly once
          - stamps job.finished_at
        """
        _TERMINAL = {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT}
        if job.state in _TERMINAL:
            return  # Already finalized — do nothing
        job.error = reason
        job.finished_at = datetime.now(timezone.utc)
        # Persist observation / run log if provided
        if obs is not None:
            try:
                obs.finalise()
            except Exception:
                pass
        if run_log is not None:
            try:
                run_log.save()
            except Exception:
                pass
        # Always emit a structured error SSE so the frontend's reader loop
        # sees a terminal event and does not hang waiting for more data.
        try:
            await job.emit_event("error", {
                "message": reason,
                "run_id": job.run_id,
                "terminal_state": terminal_state.value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass
        try:
            await job.transition_to(terminal_state)
        except Exception:
            pass

    @classmethod
    async def _watchdog_loop(cls) -> None:
        """Periodically scans active runs and cancels any that have stalled (>150s)."""
        while True:
            try:
                await asyncio.sleep(10)
                now = datetime.now(timezone.utc)
                for job in list(cls._jobs.values()):
                    terminal_states = {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT}
                    if job.state not in terminal_states:
                        stalled_duration = (now - job.last_heartbeat_at).total_seconds()
                        # 150s threshold: allows 120s manual login window + 30s margin
                        if stalled_duration > 150.0:
                            logger.warning(
                                f"Job {job.run_id} is in non-terminal state '{job.state.value}' "
                                f"but has not updated heartbeat for {stalled_duration:.1f}s. "
                                "Watchdog canceling job..."
                            )
                            task = cls._job_tasks.get(job.run_id)
                            if task and not task.done():
                                task.cancel()
                            await cls._finalize_job(
                                job,
                                RunState.TIMED_OUT,
                                f"Job stalled (no progress for {stalled_duration:.1f}s). Watchdog timed it out.",
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in watchdog loop: {e}", exc_info=True)

    @classmethod
    async def _execute_job_pipeline(cls, job: BackgroundJob) -> None:
        """Outer wrapper: executes the pipeline with a hard 300s cap.
        All terminal transitions go through _finalize_job for consistency."""
        try:
            await asyncio.wait_for(cls._execute_job_pipeline_inner(job), timeout=300.0)
        except asyncio.TimeoutError:
            logger.error(f"Job {job.run_id} timed out after 300 seconds.")
            await cls._finalize_job(
                job, RunState.TIMED_OUT,
                "Job execution timed out after 300 seconds."
            )
        except asyncio.CancelledError:
            logger.warning(f"Job {job.run_id} was cancelled.")
            await cls._finalize_job(
                job, RunState.CANCELLED,
                "Job execution was cancelled."
            )
            raise
        except Exception as e:
            logger.error(f"Job {job.run_id} failed with unhandled exception in outer wrapper: {e}", exc_info=True)
            await cls._finalize_job(
                job, RunState.FAILED,
                f"Job execution failed: {e}"
            )
        finally:
            if job.run_id in cls._job_tasks:
                del cls._job_tasks[job.run_id]

    @classmethod
    async def _execute_job_pipeline_inner(cls, job: BackgroundJob) -> None:
        """The actual logic of the background workflow worker."""
        from ai_agent import AIAgent
        from logger import RunLogger
        from monitor import ObservationLayer
        from planner import Planner
        from stream_manager import StreamManager
        from urllib.parse import urlparse

        run_log = RunLogger()
        run_log._run_id = job.run_id
        run_log.run_id = job.run_id
        run_log.data["run_id"] = job.run_id

        # Helper callback to stream monitor events to our job queue
        async def _sse_emitter(msg: str):
            await job.emit_raw(msg)

        obs = ObservationLayer(
            run_logger=run_log,
            sse_emitter=_sse_emitter,
            ticket_text=job.ticket_text,
            db_url=job.db_url
        )

        run_context = None
        page = None
        is_production = False
        target_nav_url = None
        active_db_url = job.db_url
        try:
            # ── 1. Authenticating ─────────────────────────────────────────────
            # NOTE: All gateway/auth helpers MUST be defined before do_auth() is defined,
            # because do_auth() closes over them. Python marks any name assigned in the
            # enclosing scope as a cell variable; if do_auth() is called before those
            # names are bound, Python raises NameError: cannot access free variable.

            # Gateway manual login helper
            async def handle_manual_login_if_needed(page_obj, timeout_sec=120):
                def is_on_login_page(url):
                    u = url.lower()
                    from urllib.parse import urlparse as _urlparse
                    parsed = _urlparse(u)
                    path = parsed.path
                    netloc = parsed.netloc
                    if "accounts.odoo.com" in netloc:
                        return True
                    if "/web/login" in path or "/support/login" in path:
                        return True
                    path_parts = path.strip("/").split("/")
                    if "login" in path_parts:
                        return True
                    return False

                if is_on_login_page(page_obj.url):
                    # Signal frontend: browser is waiting for manual login
                    await job.emit_raw(StreamManager.emit_browser_state(
                        "awaiting_login",
                        "Manual login required — please log in via the Chrome window."
                    ))
                    await job.emit_raw(StreamManager.emit_thinking(0, "Authentication Required",
                        "Manual login required. Please enter your credentials in the Chrome window."))
                    await job.emit_raw(StreamManager.emit_demo_thought(
                        "Waiting for manual user login in Chrome window..."))
                    logger.info("Detected login page. Waiting for user to manually log in...")

                    start_time = time.time()
                    last_heartbeat = time.time()
                    while time.time() - start_time < timeout_sec:
                        await asyncio.sleep(1.0)
                        # Emit a heartbeat every 15s so the frontend transport watchdog
                        # and the backend stall watchdog both see activity.
                        if time.time() - last_heartbeat >= 15.0:
                            elapsed = int(time.time() - start_time)
                            remaining = int(timeout_sec - elapsed)
                            await job.emit_raw(StreamManager.emit_heartbeat(
                                f"awaiting_login:{elapsed}s/{timeout_sec}s (remaining: {remaining}s)"
                            ))
                            await job.emit_raw(StreamManager.emit_browser_state(
                                "awaiting_login",
                                f"Still waiting for manual login… ({remaining}s remaining)"
                            ))
                            job.update_heartbeat()  # Keep stall watchdog happy
                            last_heartbeat = time.time()
                        current_url = page_obj.url
                        if not is_on_login_page(current_url):
                            if "/web" in current_url or "/odoo" in current_url or "_odoo/support" in current_url:
                                logger.info(f"User successfully logged in! Current URL: {current_url}")
                                await job.emit_raw(StreamManager.emit_thinking(0, "Authenticated", "Manual login detected. Continuing..."))
                                await job.emit_raw(StreamManager.emit_browser_state(
                                    "active", "Login successful — browser now active."
                                ))
                                return True

                    raise AuthenticationError("Manual login timed out. Please run again and log in within 120 seconds.", "manual_login_timeout")

            # Scan for duplicate safely
            async def find_duplicate_link(target_url: Optional[str] = None):
                from urllib.parse import urljoin
                from db_utils import get_database_name
                links = page.locator("a")
                count = await links.count()
                target_db = get_database_name(target_url).lower() if target_url else None
                
                candidate_links = []
                for i in range(count):
                    link = links.nth(i)
                    href = await link.get_attribute("href") or ""
                    if not href:
                        continue
                    abs_href = urljoin(page.url, href)
                    
                    # Exclude self-referencing duplicate links to the current page/database we are already on
                    db_name_href = get_database_name(abs_href).lower()
                    db_name_page = get_database_name(page.url).lower()
                    if db_name_href and db_name_href == db_name_page:
                        continue
                    
                    if target_db:
                        # Exclude self-referencing support page links on the duplicate tools page itself
                        if "_odoo" in abs_href.lower() and "/_odoo/support" in page.url:
                            continue
                        if db_name_href == target_db:
                            return link
                    else:
                        if is_duplicate_database(abs_href, job.db_url):
                            candidate_links.append((link, abs_href))
                            
                if not target_db and candidate_links:
                    # Prefer "Support Page" links containing "_odoo/support"
                    for link, abs_href in candidate_links:
                        if "/_odoo/support" in abs_href.lower() or "_odoo" in abs_href.lower():
                            return link
                    return candidate_links[0][0]
                return None

            # Click helper to force navigation in the same tab instead of opening a new window/tab
            async def click_in_same_tab(locator, **kwargs):
                try:
                    await locator.evaluate("el => { el.removeAttribute('target'); if (el.form) el.form.removeAttribute('target'); }")
                except Exception as e:
                    logger.debug(f"Failed to strip target from element: {e}")
                await human_like_click_locator(page, locator, **kwargs)

            # Custom helper to poll and wait for URL to match a duplicate database pattern
            async def wait_for_duplicate_url(timeout_ms: int):
                start_t = time.time()
                timeout_s = timeout_ms / 1000.0
                while time.time() - start_t < timeout_s:
                    if is_duplicate_database(page.url, job.db_url):
                        return
                    await asyncio.sleep(0.5)
                # Final check after loop
                if not is_duplicate_database(page.url, job.db_url):
                    raise asyncio.TimeoutError(f"URL '{page.url}' is not a duplicate database of '{job.db_url}'")

            # Gateway page state helper to prevent race conditions
            async def wait_for_gateway_page(page_obj, timeout_ms=10000) -> str:
                start_time = time.time()
                while time.time() - start_time < (timeout_ms / 1000.0):
                    # 1. Check if reason input exists (needs support reason login)
                    # Strictly require URL to contain /support/login to avoid false matches on tools page
                    if "/support/login" in page_obj.url:
                        for sel in selectors.get_selector("reason_input"):
                            try:
                                if await page_obj.locator(sel).count() > 0:
                                    return "login"
                            except Exception:
                                pass
                    # 2. Check if database link exists (already logged in)
                    for sel in selectors.get_selector("db_link"):
                        try:
                            if await page_obj.locator(sel).count() > 0:
                                return "portal"
                        except Exception:
                            pass
                    # 3. Check if duplicate/neutralize button exists (specifically by text first)
                    for text_sel in ["button:has-text('Duplicate')", "a:has-text('Duplicate')", "button:has-text('Neutralize')", "a:has-text('Neutralize')", "button:has-text('Create a copy')", "a:has-text('Create a copy')"]:
                        try:
                            if await page_obj.locator(text_sel).count() > 0:
                                return "portal"
                        except Exception:
                            pass
                    # 4. Check if duplicate link exists
                    try:
                        dup_link = await find_duplicate_link()
                        if dup_link:
                            return "portal"
                    except Exception:
                        pass
                    await asyncio.sleep(0.2)
                return "unknown"

            # Gateway reason login helper
            async def handle_support_login(page_obj):
                if "/support/login" in page_obj.url:
                    await job.emit_raw(StreamManager.emit_thinking(0, "Authentication", "Submitting support reason..."))
                    reason_input = None
                    reason_selector = None
                    for sel in selectors.get_selector("reason_input"):
                        if await page_obj.locator(sel).count() > 0:
                            reason_input = page_obj.locator(sel).first
                            reason_selector = sel
                            break
                    if reason_input:
                        await job.emit_raw(StreamManager.emit_demo_thought("Entering support reason"))
                        await human_like_fill(page_obj, reason_selector, "testing")
                    else:
                        inputs = page_obj.locator("input, textarea")
                        for i in range(await inputs.count()):
                            inp = inputs.nth(i)
                            if await inp.is_visible():
                                await inp.fill("testing")
                                break
                    
                    submit_btn = None
                    for sel in selectors.get_selector("submit_button"):
                        if await page_obj.locator(sel).count() > 0:
                            submit_btn = page_obj.locator(sel).first
                            break
                    if submit_btn:
                        await job.emit_raw(StreamManager.emit_demo_thought("Submitting support gateway"))
                        await human_like_click_locator(page_obj, submit_btn)
                    else:
                        await page_obj.keyboard.press("Enter")
                    try:
                        await page_obj.wait_for_load_state("load", timeout=8000)
                    except Exception:
                        pass

            # Gateway cookie-sync helper
            async def authenticate_via_support_gateway(active_target_url):
                parsed = urlparse(active_target_url)
                target_base = f"{parsed.scheme}://{parsed.netloc}"
                gateways_to_try = [f"{target_base.rstrip('/')}/_odoo/support"]
                
                auth_success = False
                for gateway_url in gateways_to_try:
                    try:
                        await job.emit_raw(StreamManager.emit_thinking(0, "Authentication", f"Syncing via: {gateway_url}..."))
                        await job.emit_raw(StreamManager.emit_demo_thought("Opening support gateway"))
                        await page.goto(gateway_url, timeout=30000)
                        await ensure_demo_overlay(page)
                        await handle_manual_login_if_needed(page)
                        
                        # Wait for gateway page state
                        page_state = await wait_for_gateway_page(page)
                        if page_state == "login":
                            await handle_support_login(page)
                            page_state = await wait_for_gateway_page(page)
                        
                        db_link = None
                        if "-support-" in active_target_url.lower() or is_duplicate_database(active_target_url, job.db_url):
                            # We are trying to authenticate a duplicate database - find a duplicate link
                            db_link = await find_duplicate_link(active_target_url)
                        
                        if not db_link:
                            # Fallback to general database link for production/normal DB
                            db_name = urlparse(job.db_url).hostname.split(".")[0]
                            for sel in selectors.get_selector("db_link"):
                                if await page.locator(sel).count() > 0:
                                    db_link = page.locator(sel).first
                                    break
                                    
                        if db_link:
                            await job.emit_raw(StreamManager.emit_demo_thought("Opening customer database"))
                            await click_in_same_tab(db_link)
                            try:
                                await page.wait_for_load_state("load", timeout=10000)
                            except Exception:
                                pass
                            if "/web/login" not in page.url:
                                auth_success = True
                                break
                    except Exception as e:
                        logger.warning(f"Support gateway authentication failed: {e}")
                
                if not auth_success and "/web/login" in page.url:
                    await handle_manual_login_if_needed(page)
                    if "/web/login" in page.url:
                        raise AuthenticationError("Gateway redirection failed: browser stuck at login page.", "support_auth")

            # Now define and call do_auth() — all helpers it closes over are already bound above
            async def do_auth():
                nonlocal run_context, page, is_production, target_nav_url
                await job.transition_to(RunState.AUTHENTICATING)
                await job.emit_raw(StreamManager.emit_thinking(0, "Initialization", "Dual Cloud Engine ready — observation active"))
                await obs.record_event("Initialization", "Dual Cloud Engine started")

                # Signal frontend: browser is about to be created
                await job.emit_raw(StreamManager.emit_browser_state(
                    "launching", "Starting isolated browser context..."
                ))

                # Isolated context launch
                try:
                    run_context = await browser_manager.create_run_context()
                    run_context.sse_emitter = _sse_emitter
                    await obs.record_event("Browser ready", "Isolated run browser context created")
                    await obs.record_event("Demo mode", f"Observable automation settings: {demo_settings()}")
                except Exception as e:
                    await job.emit_raw(StreamManager.emit_browser_state(
                        "failed", f"Browser failed to start: {e}"
                    ))
                    raise BrowserError(f"Failed to create isolated browser: {e}", "browser_start")

                page = run_context.page

                # Check if production target
                is_production = not is_duplicate_database(job.db_url)
                parsed_job_url = urlparse(job.db_url)
                job_base_url = f"{parsed_job_url.scheme}://{parsed_job_url.netloc}"
                target_nav_url = f"{job_base_url}/_odoo/support" if is_production else (
                    job.db_url if "/odoo" in job.db_url or "/web" in job.db_url else f"{job_base_url}/odoo"
                )

                # Navigate to sandbox
                await job.emit_raw(StreamManager.emit_demo_thought("Opening customer database"))
                await page.goto(target_nav_url, timeout=30000)
                await ensure_demo_overlay(page)
                # handle_manual_login_if_needed emits browser_state: awaiting_login internally
                await handle_manual_login_if_needed(page)

                page_state = await wait_for_gateway_page(page)
                if page_state == "login":
                    await handle_support_login(page)
                    page_state = await wait_for_gateway_page(page)

                if "/web/login" in page.url:
                    await obs.record_warning("Cookie synchronization initiated...", context="initial_navigation")
                    await authenticate_via_support_gateway(job.db_url)

                # Signal frontend: browser is live and authenticated
                await job.emit_raw(StreamManager.emit_browser_state(
                    "active", "Browser authenticated and active."
                ))

            try:
                await asyncio.wait_for(do_auth(), timeout=120.0)
            except asyncio.TimeoutError:
                raise AuthenticationError("Authentication / Gateway login timed out after 120 seconds.", "gateway_login_timeout")

            # ── 2. Duplicating (if production) ────────────────────────────────
            async def do_duplication():
                nonlocal active_db_url
                if is_production:
                    await job.transition_to(RunState.DUPLICATING)
                    
                    # Check gateway state again to be sure
                    page_state = await wait_for_gateway_page(page)
                    if page_state == "login":
                        await handle_support_login(page)
                        page_state = await wait_for_gateway_page(page)
                    
                    if "/web/login" in page.url or "accounts.odoo.com" in page.url or "/support/login" in page.url:
                        await handle_manual_login_if_needed(page)
                        if "/web/login" in page.url or "accounts.odoo.com" in page.url or "/support/login" in page.url:
                            raise AuthenticationError(
                                "Gateway authentication failed: browser stuck at login page. "
                                "Please ensure you are actively logged into Odoo on Chrome Profile 3 to sync cookies.",
                                "support_auth"
                            )
                    
                    dup_link = await find_duplicate_link()
                    if dup_link:
                        duplicate_chosen = await dup_link.get_attribute("href")
                        await job.emit_raw(StreamManager.emit_thinking(0, "Gateway", f"Found existing duplicate: {duplicate_chosen}. Entering..."))
                        await job.emit_raw(StreamManager.emit_demo_thought("Opening existing duplicate database"))
                        await click_in_same_tab(dup_link)
                        try:
                            await wait_for_duplicate_url(15000)
                        except Exception as e:
                            logger.warning(f"Timeout waiting for duplicate URL after clicking dup_link: {e}")
                    else:
                        # Setup handler to automatically accept confirm/alert dialogs
                        async def handle_dialog(dialog):
                            logger.info(f"Gateway Dialog: {dialog.type} - '{dialog.message}'. Accepting...")
                            await dialog.accept()
                        page.on("dialog", lambda d: asyncio.create_task(handle_dialog(d)))

                        # Shorten duplicate name if too long or fill if empty
                        name_input = None
                        for sel in ["input[name='name']", "input[name='duplicate_name']", "input:near(:text('Duplicate Name'))"]:
                            try:
                                if await page.locator(sel).count() > 0:
                                    name_input = page.locator(sel).first
                                    break
                            except Exception:
                                pass
                        if name_input:
                            current_val = await name_input.input_value()
                            if not current_val:
                                prod_sub = urlparse(job.db_url).hostname.split(".")[0]
                                current_val = f"{prod_sub}-supp"
                            
                            if len(current_val) > 28:
                                new_val = current_val[:20].rstrip("-") + "-supp"
                                await job.emit_raw(StreamManager.emit_thinking(0, "Gateway", f"Shortening duplicate database name from '{current_val}' to '{new_val}' to prevent 'name too long' error..."))
                                await name_input.fill(new_val)
                            elif not await name_input.input_value():
                                await name_input.fill(current_val)

                        # Click Duplicate button
                        dup_btn = None
                        for sel in selectors.get_selector("duplicate_button"):
                            if await page.locator(sel).count() > 0:
                                dup_btn = page.locator(sel).first
                                break
                        if not dup_btn:
                            raise DuplicationError("Duplicate button not found on gateway page.", "db_duplication")

                        await job.emit_raw(StreamManager.emit_thinking(0, "Gateway", "Creating database copy..."))
                        await job.emit_raw(StreamManager.emit_demo_thought("Creating database copy"))
                        await click_in_same_tab(dup_btn, no_wait_after=True)
                        
                        # Check confirmation modal
                        try:
                            for sel in selectors.get_selector("modal_confirm"):
                                loc = page.locator(sel)
                                if await loc.count() > 0:
                                    await human_like_click_locator(page, loc.first)
                                    break
                        except Exception:
                            pass
                        
                        # Wait for duplication to complete and redirect to the duplicate database
                        await job.emit_raw(StreamManager.emit_thinking(0, "Gateway", "Duplication process started. Waiting for redirection to the duplicate database..."))
                        try:
                            await wait_for_duplicate_url(180000)
                        except Exception as e:
                            raise DuplicationError(f"Database copy failed or timed out: redirection to duplicate database did not occur. Error: {e}", "db_duplication")

                    # Sync support gateway on duplicate database base
                    try:
                        await page.wait_for_load_state("load", timeout=15000)
                    except Exception:
                        pass
                    # Keep the full URL (including query parameters like ?db=...) to preserve the database identifier
                    active_db_url = page.url
                    await authenticate_via_support_gateway(active_db_url)
                    obs._db_url = active_db_url

                # Transition Frontend to Backend dashboard
                transition_attempts = 0
                while transition_attempts < 5:
                    transition_attempts += 1
                    arrow_clicked = False
                    for sel in selectors.get_selector("arrow_toggle"):
                        loc = page.locator(sel)
                        if await loc.count() > 0 and await loc.first.is_visible():
                            await human_like_click_locator(page, loc.first)
                            try:
                                await page.wait_for_load_state("load", timeout=3000)
                            except Exception:
                                pass
                            arrow_clicked = True
                            break
                    grid_clicked = False
                    for sel in selectors.get_selector("grid_toggle"):
                        loc = page.locator(sel)
                        if await loc.count() > 0 and await loc.first.is_visible():
                            await human_like_click_locator(page, loc.first)
                            try:
                                await page.wait_for_load_state("load", timeout=5000)
                            except Exception:
                                pass
                            grid_clicked = True
                            break
                    if grid_clicked:
                        break
                    if not arrow_clicked and not grid_clicked:
                        if "/odoo" in page.url or "/web" in page.url:
                            break
                        await page.wait_for_timeout(1000)

                # Check neutralization
                is_neutralized = False
                for sel in selectors.get_selector("neutral_banner"):
                    if await page.locator(sel).count() > 0:
                        is_neutralized = True
                        break
                if is_neutralized:
                    await obs.record_event("Environment", "Neutralized Staging Environment")
                else:
                    await obs.record_warning("LIVE PRODUCTION DB WARNING: No neutralization banner found.", context="environment_check")

            try:
                await asyncio.wait_for(do_duplication(), timeout=180.0)
            except asyncio.TimeoutError:
                raise DuplicationError("Database duplication process timed out after 180 seconds.", "db_duplication_timeout")

            # ── 3. Analyzing ──────────────────────────────────────────────────
            ticket_info = {}
            module = "unknown"
            version = "unknown"
            confidence = 1.0
            groq_key = os.getenv("GROQ_API_KEY", "")
            gemini_key = os.getenv("GEMINI_API_KEY", "")

            async def do_triage():
                nonlocal ticket_info, module, version, confidence
                await job.transition_to(RunState.ANALYZING)
                await obs.on_engine1_call("ticket_analysis")
                
                ai_agent = AIAgent(groq_api_key=groq_key, gemini_api_key=gemini_key)

                # Triage streaming token emission
                triage_stream_text = ""
                def triage_token_collector():
                    return list(ai_agent.analyse_ticket_stream(job.ticket_text, run_logger=run_log))
                
                loop = asyncio.get_event_loop()
                # Thread-pool limitation: loop.run_in_executor runs the collector in a separate thread.
                # If wait_for times out, the task is cancelled, but the thread-bound function continues
                # running in the background until the Groq call completes.
                try:
                    tokens = await asyncio.wait_for(
                        loop.run_in_executor(None, triage_token_collector),
                        timeout=30.0
                    )
                except asyncio.TimeoutError:
                    raise PlanningError("Groq ticket analysis stream timed out after 30 seconds.", "groq_timeout")
                
                for token in tokens:
                    triage_stream_text += token
                    await job.emit_raw(f"data: {json.dumps({'type': 'thinking', 'step': 1, 'token': token})}\n\n")
                
                ticket_info = ai_agent._parse_ticket_json(triage_stream_text)
                if job.odoo_version:
                    ticket_info["odoo_version"] = job.odoo_version
                
                run_log.log_input(ticket_text=job.ticket_text, db_url=active_db_url, extracted=ticket_info)
                module = ticket_info.get("module") or "unknown"
                version = ticket_info.get("odoo_version") or "unknown"
                confidence = ticket_info.get("confidence") or 1.0

                await job.emit_raw(StreamManager.emit_thinking(0, "Ticket Triaged", f"Module: {module} | Version: {version}"))
                await obs.record_event("Ticket Triaged", f"Module={module} Version={version} Confidence={confidence}")

            try:
                await asyncio.wait_for(do_triage(), timeout=45.0)
            except asyncio.TimeoutError:
                raise PlanningError("Triage/Analysis stage timed out after 45 seconds.", "triage_timeout")

            # ── 4. Planning ───────────────────────────────────────────────────
            plan = None

            async def do_planning():
                nonlocal plan
                await job.transition_to(RunState.PLANNING)
                await obs.on_engine2_call("plan_generation")
                
                planner_engine = Planner(api_key=gemini_key)
                plan = await planner_engine.generate_plan(job.ticket_text, active_db_url)
                await obs.record_event("Plan validated", f"Module={plan.module} Confidence={plan.confidence:.2f}")
                plan_payload = plan.model_dump()
                await job.emit_raw(StreamManager.emit_plan(plan_payload))

                # Confidence check gate
                if plan.confidence < 0.6:
                    run_log.log_execution_gate(confidence=plan.confidence, threshold=0.6, executed=False, reason="Low confidence")
                    await obs.record_warning(f"Confidence {plan.confidence:.2f} too low — execution skipped.", context="confidence_gate")
                    
                    summary = obs.finalise()
                    report = {
                        "ticket_summary": plan.summary, "module": plan.module, "confidence": plan.confidence,
                        "steps_total": len(plan.steps), "steps_succeeded": 0, "steps_failed": 0,
                        "was_executed": False, "skip_reason": "Low confidence", "results": [],
                        "findings": ["Aborted execution due to low confidence."], "recommendation": "Manual review recommended.",
                        "monitor_summary": summary
                    }
                    job.result = report
                    await job.emit_event("final_summary", report)
                    await job.transition_to(RunState.COMPLETED)
                    return False

                run_log.log_execution_gate(confidence=plan.confidence, threshold=0.6, executed=True, reason="High confidence")
                return True

            try:
                should_continue = await asyncio.wait_for(do_planning(), timeout=45.0)
            except asyncio.TimeoutError:
                raise PlanningError("Planning stage timed out after 45 seconds.", "planning_timeout")

            if not should_continue:
                return

            # ── 5. Executing ──────────────────────────────────────────────────
            execution_results = []

            async def do_execution():
                nonlocal execution_results
                await job.transition_to(RunState.EXECUTING)
                await job.emit_raw(StreamManager.emit_thinking(0, "Execution Initialization", "Plan validated. Launching local deterministic execution engine..."))
                
                # Assert duplicate database safety checks before execution begins
                await assert_duplicate_database(active_db_url, job.db_url, page=page, run_logger=run_log)
                await assert_duplicate_database(page.url, job.db_url, page=page, run_logger=run_log)

                from executor import ExecutionEngine
                engine = ExecutionEngine(page=page, run_logger=run_log, sse_emitter=_sse_emitter, prod_url=job.db_url)

                # Run the 0-token local browser execution loop
                execution_results = await engine.execute_plan(plan)

                await job.emit_raw(StreamManager.emit_thinking(0, "Execution Complete", "Local browser navigation complete. Finalizing resolution guide..."))

            try:
                await asyncio.wait_for(do_execution(), timeout=180.0)
            except asyncio.TimeoutError:
                raise ExecutionError("Execution stage timed out after 180 seconds.", "execution_timeout")

            # ── 6. Reporting ──────────────────────────────────────────────────
            async def do_reporting():
                await job.transition_to(RunState.REPORTING)
                
                monitor_summary = obs.finalise()
                log_path = run_log.save()

                # Adapt execution results for the legacy reporting code
                error_is_reproduced = all(r.success for r in execution_results) if execution_results else False
                clean_resolution = "Local execution completed. See detailed findings."
                raw_execution_findings = "\\n".join([f"Step {r.step_id}: {'Success' if r.success else 'Failed'} - {r.message}" for r in execution_results])
                graph_findings = ""

                from doc_writer import generate_report
                generated_report_docx = generate_report(
                    ticket_text=job.ticket_text,
                    ticket_info=ticket_info,
                    db_findings=raw_execution_findings,
                    runbot_findings="",
                    resolution=clean_resolution,
                    screenshots=run_context.screenshots if run_context else []
                )

                report = {
                    "ticket_summary": plan.summary,
                    "module": plan.module,
                    "confidence": plan.confidence,
                    "steps_total": len(plan.steps),
                    "steps_succeeded": len(plan.steps) if error_is_reproduced else 0,
                    "steps_failed": 0,
                    "was_executed": True,
                    "skip_reason": None,
                    "results": [],
                    "findings": [clean_resolution],
                    "recommendation": clean_resolution,
                    "simple_draft": graph_findings,
                    "log_path": log_path,
                    "report_path": generated_report_docx,
                    "monitor_summary": monitor_summary
                }
                job.result = report
                await job.emit_event("final_summary", report)
                await job.transition_to(RunState.COMPLETED)

            try:
                await asyncio.wait_for(do_reporting(), timeout=20.0)
            except asyncio.TimeoutError:
                raise ExecutionError("Reporting stage timed out after 20 seconds.", "reporting_timeout")

        except asyncio.CancelledError:
            logger.warning(f"Job {job.run_id} was cancelled.")
            try:
                await obs.record_error("Job was cancelled.", context="job_worker")
            except Exception:
                pass
            await JobManager._finalize_job(
                job, RunState.CANCELLED,
                job.error or "Job execution was cancelled.",
                obs=obs, run_log=run_log,
            )
            raise
        except Exception as e:
            logger.error(f"Job {job.run_id} failed: {e}", exc_info=True)
            try:
                await obs.record_error(str(e), context="job_worker")
            except Exception:
                pass
            await JobManager._finalize_job(
                job, RunState.FAILED,
                f"Job execution failed: {e}",
                obs=obs, run_log=run_log,
            )
        finally:
            # Signal frontend: browser session is over (regardless of outcome)
            try:
                await job.emit_raw(StreamManager.emit_browser_state(
                    "completed", "Browser session closed."
                ))
            except Exception:
                pass
            # Close isolated browser context cleanly
            if run_context is not None:
                try:
                    await run_context.close()
                except Exception as _close_err:
                    logger.warning(f"Error closing run context for {job.run_id}: {_close_err}")
