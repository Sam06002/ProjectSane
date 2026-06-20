"""
server.py — FastAPI orchestrator for Project Sane v3.
Exposes REST endpoints and SSE streams routing tasks to the background JobManager.
"""

import asyncio
import os
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

from run_state import RunState
from job_manager import JobManager, BackgroundJob
from monitor import get_run_history, get_run_detail

app = FastAPI(title="Project Sane v3")
templates = Jinja2Templates(directory="templates")
app.mount("/logs", StaticFiles(directory="logs"), name="logs")
app.mount("/output", StaticFiles(directory="output"), name="output")


@app.on_event("startup")
async def startup_event():
    os.makedirs("output", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    JobManager.start_watchdog()


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


# ── Runs dashboard API ────────────────────────────────────────────────────────

def serialize_active_job(job: BackgroundJob) -> dict:
    duration = round((datetime.now(timezone.utc) - job.started_at).total_seconds(), 1)
    return {
        "run_id": job.run_id,
        "started_at": job.started_at.isoformat(),
        "duration_seconds": duration,
        "ticket_preview": job.ticket_text[:120],
        "db_url": job.db_url,
        "total_errors": len(job.error) if job.error else 0,
        "total_warnings": 0,
        "run_valid": job.error is None,
        "state": job.state.value,
        "log_file": f"logs/run_{job.run_id}.json"
    }


@app.get("/api/runs")
async def list_runs():
    """Return the 20 most recent run summaries, merging active and historic runs."""
    active_jobs = [
        serialize_active_job(j) 
        for j in JobManager.get_all_jobs() 
        if j.state not in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT)
    ]
    index_runs = get_run_history(limit=20)
    
    # Avoid duplicate IDs
    active_ids = {j["run_id"] for j in active_jobs}
    filtered_index = [r for r in index_runs if r.get("run_id") not in active_ids]
    
    return JSONResponse(content=active_jobs + filtered_index)


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    """Return the full structured JSON log or current active job status."""
    job = JobManager.get_job(run_id)
    if job:
        if job.result:
            return JSONResponse(content=job.result)
        else:
            return JSONResponse(content={
                "run_id": job.run_id,
                "state": job.state.value,
                "started_at": job.started_at.isoformat(),
                "ticket_preview": job.ticket_text[:120],
                "db_url": job.db_url,
                "error": job.error
            })
            
    detail = get_run_detail(run_id)
    if detail is None:
        return JSONResponse(status_code=404, content={"error": "Run not found."})
    return JSONResponse(content=detail)


# ── Run Trigger Endpoint ──────────────────────────────────────────────────────

@app.post("/api/run")
async def run_pipeline(request: Request):
    """Starts a background pipeline job and returns the run_id immediately."""
    data = await request.json()
    ticket_text = data.get("ticket_text", "")
    raw_url = data.get("db_url", "https://shsri.odoo.com")
    odoo_version = data.get("odoo_version")

    # Normalise URL — auto-prepend https:// if omitted
    if raw_url and not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url
    db_url = raw_url

    # Validate URL
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

    # Reject non-customer Odoo domains
    _RESERVED_ODOO_SUBDOMAINS = {"www", "runbot", "preview", "staging", "demo", "mail", "download", "cdn"}
    _hostname_lower = _hostname.lower()
    _parts = _hostname_lower.split(".")
    if len(_parts) >= 3 and _parts[-2] == "odoo" and _parts[-1] == "com":
        _subdomain_part = _parts[0]
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

    # Create job and start background task
    job = JobManager.create_job(ticket_text, db_url, odoo_version)
    await JobManager.start_job(job)
    
    return JSONResponse(content={
        "run_id": job.run_id,
        "status": job.state.value
    })


# ── Job SSE Progress Stream ───────────────────────────────────────────────────

@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str):
    """Streams real-time execution progress logs and state transitions via SSE."""
    job = JobManager.get_job(run_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": f"Job {run_id} not found."})
        
    async def event_generator():
        # 1. Replay history logs first
        for msg in job.history:
            yield msg

        # 2. Stream new logs with a hard 5-minute timeout guard
        HARD_TIMEOUT_S = 300  # 5 minutes max stream duration
        start_t = asyncio.get_event_loop().time()
        while True:
            # Check hard timeout first
            if asyncio.get_event_loop().time() - start_t > HARD_TIMEOUT_S:
                yield f"event: error\ndata: {{\"message\": \"Stream timed out after {HARD_TIMEOUT_S}s\"}}\n\n"
                break
            # Terminal state: drain remaining queue then stop
            if job.state in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT) and job.queue.empty():
                break
            try:
                msg = await asyncio.wait_for(job.queue.get(), timeout=1.0)
                yield msg
                job.queue.task_done()
            except asyncio.TimeoutError:
                # Send SSE comment as keepalive so browser doesn't close the connection
                yield ": keepalive\n\n"
                continue

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
