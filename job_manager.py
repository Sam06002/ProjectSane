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

    async def transition_to(self, new_state: RunState) -> None:
        """Transitions the job to a new state, logging and emitting the event."""
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
        self.history.append(msg)
        await self.queue.put(msg)

    async def emit_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Formats and pushes a structured SSE event to the queue."""
        msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        await self.emit_raw(msg)


class JobManager:
    """Orchestrates and tracks asynchronous execution of Project Sane jobs."""
    _jobs: Dict[str, BackgroundJob] = {}

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
        asyncio.create_task(cls._execute_job_pipeline(job))

    @classmethod
    async def _execute_job_pipeline(cls, job: BackgroundJob) -> None:
        """The actual background workflow worker executing Odoo Support Automation."""
        from ai_agent import AIAgent
        from logger import RunLogger
        from monitor import ObservationLayer
        from planner import Planner
        from stream_manager import StreamManager
        from urllib.parse import urlparse

        run_log = RunLogger()
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
        try:
            # ── 1. Authenticating ─────────────────────────────────────────────
            await job.transition_to(RunState.AUTHENTICATING)
            await job.emit_raw(StreamManager.emit_thinking(0, "Initialization", "Dual Cloud Engine ready — observation active"))
            await obs.record_event("Initialization", "Dual Cloud Engine started")

            # Isolated context launch
            try:
                run_context = await browser_manager.create_run_context()
                run_context.sse_emitter = _sse_emitter
                await obs.record_event("Browser ready", "Isolated run browser context created")
                await obs.record_event("Demo mode", f"Observable automation settings: {demo_settings()}")
            except Exception as e:
                raise BrowserError(f"Failed to create isolated browser: {e}", "browser_start")

            page = run_context.page

            # Check if production target
            is_production = "-support-" not in job.db_url.lower() and not ("/odoo" in job.db_url or "/web" in job.db_url)
            target_nav_url = f"{job.db_url.rstrip('/')}/_odoo/support" if is_production else (
                job.db_url if "/odoo" in job.db_url or "/web" in job.db_url else f"{job.db_url.rstrip('/')}/odoo"
            )

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
                        await handle_support_login(page)
                        try:
                            await page.wait_for_url(lambda u: "/support/login" not in u, timeout=10000)
                        except Exception:
                            pass
                        
                        db_link = None
                        if "-support-" in active_target_url.lower():
                            dup_subdomain = active_target_url.split("//")[-1].split(".")[0]
                            for pattern in [f"a[href*='{dup_subdomain}']", f"a:has-text('{dup_subdomain}')"]:
                                loc = page.locator(pattern)
                                if await loc.count() > 0:
                                    db_link = loc.first
                                    break
                        if not db_link:
                            db_name = urlparse(job.db_url).hostname.split(".")[0]
                            for sel in selectors.get_selector("db_link"):
                                if await page.locator(sel).count() > 0:
                                    db_link = page.locator(sel).first
                                    break
                        if db_link:
                            await job.emit_raw(StreamManager.emit_demo_thought("Opening customer database"))
                            await human_like_click_locator(page, db_link)
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
                    raise AuthenticationError("Gateway redirection failed: browser stuck at login page.", "support_auth")

            # Navigate to sandbox
            await job.emit_raw(StreamManager.emit_demo_thought("Opening customer database"))
            await page.goto(target_nav_url, timeout=30000)
            await ensure_demo_overlay(page)
            if "/web/login" in page.url:
                await obs.record_warning("Cookie synchronization initiated...", context="initial_navigation")
                await authenticate_via_support_gateway(job.db_url)

            # ── 2. Duplicating (if production) ────────────────────────────────
            active_db_url = job.db_url
            if is_production:
                await job.transition_to(RunState.DUPLICATING)
                await handle_support_login(page)
                try:
                    await page.wait_for_url(lambda u: "/support/login" not in u, timeout=10000)
                except Exception:
                    pass
                
                if "/web/login" in page.url or "accounts.odoo.com" in page.url or "/support/login" in page.url:
                    raise AuthenticationError(
                        "Gateway authentication failed: browser stuck at login page. "
                        "Please ensure you are actively logged into Odoo on Chrome Profile 3 to sync cookies.",
                        "support_auth"
                    )
                
                # Scan for duplicate
                async def find_duplicate_link():
                    prod_sub = urlparse(job.db_url).hostname.split(".")[0]
                    for pattern in ["a[href*='support-']", "a[href*='neutralized']", "a[href*='copy']", f"a[href*='{prod_sub}-']"]:
                        loc = page.locator(pattern)
                        if await loc.count() > 0:
                            return loc.first
                    return None

                dup_link = await find_duplicate_link()
                if dup_link:
                    duplicate_chosen = await dup_link.get_attribute("href")
                    await job.emit_raw(StreamManager.emit_thinking(0, "Gateway", f"Found existing duplicate: {duplicate_chosen}. Entering..."))
                    await job.emit_raw(StreamManager.emit_demo_thought("Opening existing duplicate database"))
                    await human_like_click_locator(page, dup_link)
                else:
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
                    await human_like_click_locator(page, dup_btn)
                    
                    # Check confirmation modal
                    try:
                        for sel in selectors.get_selector("modal_confirm"):
                            loc = page.locator(sel)
                            if await loc.count() > 0:
                                await human_like_click_locator(page, loc.first)
                                break
                    except Exception:
                        pass
                    
                    # Poll for duplication completed
                    found_dup = False
                    poll_start = time.time()
                    for poll_attempt in range(36):
                        await asyncio.sleep(5)
                        await job.emit_raw(StreamManager.emit_thinking(0, "Gateway", f"Polling duplication progress ({int(time.time() - poll_start)}s elapsed)..."))
                        
                        try:
                            await page.goto(target_nav_url, timeout=15000)
                            await ensure_demo_overlay(page)
                            await page.wait_for_load_state("load", timeout=8000)
                        except Exception:
                            pass
                        await handle_support_login(page)
                        try:
                            await page.wait_for_url(lambda u: "/support/login" not in u, timeout=10000)
                        except Exception:
                            pass
                        
                        dup_link = await find_duplicate_link()
                        if dup_link:
                            duplicate_chosen = await dup_link.get_attribute("href")
                            await job.emit_raw(StreamManager.emit_demo_thought("Opening completed duplicate"))
                            await human_like_click_locator(page, dup_link)
                            found_dup = True
                            break
                    
                    if not found_dup:
                        raise DuplicationError("Database copy timed out on support gateway.", "db_duplication")

                # Sync support gateway on duplicate database base
                try:
                    await page.wait_for_load_state("load", timeout=15000)
                except Exception:
                    pass
                duplicate_base = f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}"
                await authenticate_via_support_gateway(duplicate_base)
                active_db_url = duplicate_base
                obs.db_url = active_db_url

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

            # ── 3. Analyzing ──────────────────────────────────────────────────
            await job.transition_to(RunState.ANALYZING)
            await obs.on_engine1_call("ticket_analysis")
            
            groq_key = os.getenv("GROQ_API_KEY", "")
            gemini_key = os.getenv("GEMINI_API_KEY", "")
            ai_agent = AIAgent(groq_api_key=groq_key, gemini_api_key=gemini_key)

            # Triage streaming token emission
            triage_stream_text = ""
            def triage_token_collector():
                return list(ai_agent.analyse_ticket_stream(job.ticket_text, run_logger=run_log))
            
            loop = asyncio.get_event_loop()
            tokens = await loop.run_in_executor(None, triage_token_collector)
            
            for token in tokens:
                triage_stream_text += token
                await job.emit_raw(f"data: {json.dumps({'type': 'thinking', 'step': 1, 'token': token})}\n\n")
            
            ticket_info = ai_agent._parse_ticket_json(triage_stream_text)
            if job.odoo_version:
                ticket_info["odoo_version"] = job.odoo_version
            
            run_log.log_input(ticket_text=job.ticket_text, db_url=active_db_url, extracted=ticket_info)
            module = ticket_info.get("module") or "unknown"
            version = ticket_info.get("odoo_version") or "unknown"
            confidence = ticket_info.get("confidence") or 1.0  # default to 1.0 if not generated yet

            await job.emit_raw(StreamManager.emit_thinking(0, "Ticket Triaged", f"Module: {module} | Version: {version}"))
            await obs.record_event("Ticket Triaged", f"Module={module} Version={version} Confidence={confidence}")

            # ── 4. Planning ───────────────────────────────────────────────────
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
                return

            run_log.log_execution_gate(confidence=plan.confidence, threshold=0.6, executed=True, reason="High confidence")

            # ── 5. Executing ──────────────────────────────────────────────────
            await job.transition_to(RunState.EXECUTING)
            await job.emit_raw(StreamManager.emit_thinking(0, "Execution Initialization", "Plan validated. Launching local deterministic execution engine..."))
            
            from executor import ExecutionEngine
            engine = ExecutionEngine(page=page, run_logger=run_log, sse_emitter=_sse_emitter)

            # Run the 0-token local browser execution loop
            execution_results = await engine.execute_plan(plan)

            await job.emit_raw(StreamManager.emit_thinking(0, "Execution Complete", "Local browser navigation complete. Finalizing resolution guide..."))

            # ── 6. Reporting ──────────────────────────────────────────────────
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

        except Exception as e:
            logger.error(f"Job {job.run_id} failed: {e}", exc_info=True)
            job.error = str(e)
            await obs.record_error(str(e), context="job_worker")
            obs.finalise()
            run_log.save()
            
            # Emit error SSE
            await job.emit_event("error", {"message": f"Job execution failed: {e}"})
            await job.transition_to(RunState.FAILED)
        finally:
            job.finished_at = datetime.now(timezone.utc)
            # Close isolated context cleanly
            if run_context is not None:
                await run_context.close()
