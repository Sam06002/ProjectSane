import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ai_agent import AIAgent
from browser_agent import BrowserAgent
from doc_writer import generate_report

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# In-memory job store: {job_id: {inputs dict}}
jobs: dict = {}

# Global Browser Agent to keep the persistent context alive across multiple tickets
global_browser_agent = BrowserAgent(headless=False)

# ── HITL Approval Store ────────────────────────────────────────────────────────
# Maps job_id → asyncio.Event (set when user clicks Approve or Skip)
_approval_events: dict[str, asyncio.Event] = {}
# Maps job_id → bool (True = approved, False = skipped)
_approval_results: dict[str, bool] = {}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/run")
async def run_job(request: Request):
    form = await request.form()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "ticket_text": form.get("ticket_text", "").strip(),
        "odoo_version": form.get("odoo_version", "").strip(),
        "db_url": form.get("db_url", "").strip(),
        "documentation": form.get("documentation", "").strip(),
    }
    return {"job_id": job_id}


@app.post("/approve/{job_id}")
async def approve_script(job_id: str, request: Request):
    """
    HITL endpoint — called by the frontend when Sam clicks 'Approve & Run' or 'Skip'.
    Unblocks the waiting SSE coroutine and stores the decision.
    """
    body = await request.json()
    approved = bool(body.get("approved", False))
    _approval_results[job_id] = approved

    event = _approval_events.get(job_id)
    if event:
        event.set()

    return {"status": "ok", "approved": approved}


@app.get("/stream/{job_id}")
async def stream_job(job_id: str):
    if job_id not in jobs:
        return HTMLResponse("Job not found", status_code=404)

    job = jobs[job_id]

    async def event_generator():
        def sse(step: int, message: str, done: bool = False,
                report_path: str = "", event_type: str = "message",
                script_code: str = ""):
            data = {
                "step": step,
                "message": message,
                "done": done,
                "report_path": report_path,
                "event_type": event_type,
                "script_code": script_code,
            }
            return f"data: {json.dumps(data)}\n\n"

        try:
            groq_api_key = os.getenv("GROQ_API_KEY", "")
            gemini_api_key = os.getenv("GEMINI_API_KEY", "")

            if not groq_api_key:
                yield sse(0, "ERROR: GROQ_API_KEY not set in .env", done=True)
                return
            if not gemini_api_key:
                yield sse(0, "ERROR: GEMINI_API_KEY not set in .env", done=True)
                return

            os.makedirs("output", exist_ok=True)

            # Step 1 — AI analysis (Groq for fast JSON extraction)
            yield sse(1, "Analysing ticket with Groq (fast extraction)...")
            await asyncio.sleep(0)
            ai = AIAgent(groq_api_key=groq_api_key, gemini_api_key=gemini_api_key)

            ticket_text = job["ticket_text"]
            if job["documentation"]:
                ticket_text += f"\n\nAdditional documentation provided by Sam:\n{job['documentation']}"

            ticket_info = ai.analyse_ticket(ticket_text)

            # Override odoo_version with Sam's input if AI didn't detect it
            if job["odoo_version"] and not ticket_info.get("odoo_version"):
                ticket_info["odoo_version"] = job["odoo_version"]

            yield sse(1, f"Ticket analysed with Groq. Module: {ticket_info.get('module', 'unknown')} | Version: {ticket_info.get('odoo_version', 'unknown')}")
            await asyncio.sleep(0)

            # Step 2 — Browser: open duplicate DB
            yield sse(2, "Opening duplicate database in browser...")
            await asyncio.sleep(0)

            # Reset per-ticket state on the global agent
            global_browser_agent.screenshots = []

            # Use the global browser agent to prevent locking the persistent profile.
            # start() is idempotent — if context is already alive it just opens a new tab.
            await global_browser_agent.start()
            browser = global_browser_agent

            # ── Build the HITL approval callback ──────────────────────────────
            # This is a closure that:
            #   1. Generates the Playwright script via Gemini
            #   2. Streams it to the frontend as an 'approval_required' SSE event
            #   3. Waits (up to 5 min) for Sam to click Approve or Skip
            #   4. Returns (script_code, approved_bool) to browser_agent
            script_queue: asyncio.Queue = asyncio.Queue()

            async def request_approval(script_code: str) -> bool:
                # Register an event for this job
                event = asyncio.Event()
                _approval_events[job_id] = event

                # Push the approval request into the SSE queue so it streams out
                await script_queue.put(("approval_required", script_code))

                # Wait for Sam's decision (timeout 5 min)
                try:
                    await asyncio.wait_for(event.wait(), timeout=300)
                except asyncio.TimeoutError:
                    _approval_results[job_id] = False  # auto-skip on timeout

                return _approval_results.get(job_id, False)

            async def stream_cb(event_type: str, content: str):
                await script_queue.put((event_type, content))

            # Step 3 — Browser: investigate
            yield sse(3, "Investigating: checking config, modules, reproducing error...")
            await asyncio.sleep(0)

            # Run investigation in a background task so we can
            # interleave the approval SSE event while waiting
            investigation_task = asyncio.create_task(
                browser.investigate_duplicate_db(
                    job["db_url"], ticket_info,
                    request_approval_callback=request_approval,
                    stream_callback=stream_cb
                )
            )

            db_findings = ""
            while not investigation_task.done():
                # Check if the browser agent has pushed an approval request or stream chunks
                try:
                    event_type, content = script_queue.get_nowait()
                    if event_type == "approval_required":
                        # Stream the script to the frontend and pause
                        yield sse(3, "AI script ready — awaiting your approval.",
                                  event_type="approval_required",
                                  script_code=content)
                        await asyncio.sleep(0)
                    elif event_type == "thinking_stream":
                        # Stream the thinking chunks
                        yield sse(3, "AI is analyzing documentation...",
                                  event_type="thinking_stream",
                                  script_code=content)  # we'll reuse script_code for payload
                        await asyncio.sleep(0)
                except asyncio.QueueEmpty:
                    pass
                await asyncio.sleep(0.05)

            db_findings = await investigation_task

            # Step 4 — Runbot (conditional)
            runbot_findings = ""
            if ticket_info.get("check_runbot") and ticket_info.get("odoo_version"):
                yield sse(4, f"Testing standard behaviour on Runbot (version {ticket_info['odoo_version']})...")
                await asyncio.sleep(0)
                runbot_findings = await browser.test_on_runbot(ticket_info["odoo_version"])
            else:
                yield sse(4, "Runbot check not required for this ticket.")
                await asyncio.sleep(0)

            await browser.stop()

            # Step 4b — AI synthesis
            yield sse(4, "Synthesising resolution with Gemini 2.5 Pro...")
            await asyncio.sleep(0)
            all_findings = db_findings + ("\n\n" + runbot_findings if runbot_findings else "")
            resolution = ai.synthesise_resolution(ticket_text, all_findings)

            # Step 5 — Generate report
            yield sse(5, "Generating Word report...")
            await asyncio.sleep(0)
            report_path = generate_report(
                ticket_text,
                ticket_info,
                db_findings,
                runbot_findings,
                resolution,
                browser.screenshots,
            )

            yield sse(5, "Done! Report is ready.", done=True, report_path=report_path)

        except Exception as e:
            import traceback
            traceback.print_exc()
            yield sse(0, f"ERROR: {str(e)}", done=True)

        finally:
            # Clean up approval state
            _approval_events.pop(job_id, None)
            _approval_results.pop(job_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/download")
async def download_report(path: str):
    file_path = Path(path)
    if not file_path.exists():
        return HTMLResponse("Report not found", status_code=404)
    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
