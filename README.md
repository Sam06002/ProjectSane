# Project Sane — Odoo Support Automation Agent

Version 1.5 | Status: Active Development

---

## Overview

![Project Sane v1.5 — High-Level Operational Workflow](workflow_diagram.png)

Project Sane is an internal automation system built to eliminate repetitive manual work from the Odoo customer support workflow. A support analyst pastes a raw customer ticket into the web interface. The system then autonomously reads the ticket, opens the customer's duplicate database in a controlled browser session, navigates the Odoo UI, captures visual evidence, queries official documentation in real time, and produces a structured resolution report in Microsoft Word format — all without the analyst touching the browser.

The system is designed to run locally on macOS and integrates with the analyst's existing Chrome profile so that existing Odoo sessions are preserved and reused across ticket runs.


---

## Architecture

The system is composed of five modules that form a sequential pipeline.

```
Analyst submits ticket (Web UI)
        |
        v
[server.py] FastAPI server receives the job and opens an SSE stream
        |
        v
[ai_agent.py] Groq (llama-3.3-70b) performs fast structured JSON extraction
        |
        v
[browser_agent.py] Playwright opens the Odoo support gateway,
                   creates or reuses a duplicate database,
                   navigates the UI, and captures screenshots
        |
        v
[ai_agent.py] Gemini 2.5 Pro + Google Search Grounding queries official
              Odoo documentation and generates a Functional Blueprint
              plus an executable Playwright investigation script
        |
        v
[HITL Gate] Analyst reviews the AI-generated script in the UI
            and clicks Approve or Skip
        |
        v
[doc_writer.py] python-docx assembles the final Word report
                with findings, screenshots, and resolution guide
        |
        v
Analyst downloads the report from the UI
```

### Module Responsibilities

| File | Role |
|---|---|
| `server.py` | FastAPI application. Manages jobs, SSE streaming, HITL approval endpoints, and the global browser singleton. |
| `ai_agent.py` | All AI logic. Groq for ticket extraction, Gemini 2.5 Pro for grounded documentation search and script generation, Gemini for synthesis. |
| `browser_agent.py` | All browser automation. Chrome lifecycle management via CDP, duplicate database creation, screenshot capture, dynamic AI script execution. |
| `doc_writer.py` | Assembles the Word report from all collected findings, ticket info, and screenshots. |
| `templates/index.html` | The analyst-facing web UI. Renders the multi-step progress tracker, the HITL approval code view, and the download button. |

---

## Prerequisites

- macOS (arm64 or x86\_64)
- Python 3.11 or higher
- Google Chrome installed at the standard macOS path (`/Applications/Google Chrome.app`)
- A Chrome profile that is already authenticated to the target Odoo instance
- API keys for Groq and Google Gemini (see Configuration section)

---

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/Sam06002/ProjectSane.git
cd ProjectSane
```

**2. Create a virtual environment**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**3. Install Python dependencies**

```bash
pip install -r requirements.txt
```

**4. Install Playwright browser drivers**

Playwright requires its own browser binaries for headless operation. The system also uses native Chrome, but this step ensures Playwright's internal tooling is complete.

```bash
playwright install chromium
```

**5. Configure environment variables**

```bash
cp .env.example .env
```

Open `.env` and fill in your API keys:

```
GROQ_API_KEY=gsk_...
GEMINI_API_KEY=AI...
ACTIVE_PROVIDER=gemini
HEADLESS=false
```

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes | API key from console.groq.com. Used for fast ticket extraction via llama-3.3-70b. Free tier is sufficient. |
| `GEMINI_API_KEY` | Yes | API key from aistudio.google.com. Used for documentation search grounding and script generation via Gemini 2.5 Pro. |
| `ACTIVE_PROVIDER` | No | Set to `gemini` (default) for the full production pipeline. Set to `local` to route AI calls to a local LM Studio server instead of Gemini. |
| `LOCAL_API_BASE` | No | Only used when `ACTIVE_PROVIDER=local`. Default: `http://127.0.0.1:1234/v1`. |
| `HEADLESS` | No | Set to `true` to run Chrome without a visible window. Default: `false`. |

### Chrome Profile Configuration

The browser agent is configured to use a specific Chrome profile that already holds an authenticated Odoo session. This avoids having to log in through Google OAuth on every run.

The profile is identified by inspecting `~/Library/Application Support/Google/Chrome/` for the profile directory associated with the Odoo account. By default the system uses `Profile 3`. To change this, update the constants at the top of `browser_agent.py`:

```python
CHROME_USER_DATA_DIR = str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome")
CHROME_PROFILE_DIR = "Profile 3"
```

Before every run, the agent copies the session cookies from the source profile into a separate working directory (`ChromeAgentWork`). This is necessary because Chrome's security policy prohibits enabling remote debugging on the default profile directory. The cookie copy ensures the agent has access to the active Odoo session without disturbing the analyst's live Chrome window.

---

## Running the Server

```bash
source .venv/bin/activate
./.venv/bin/uvicorn server:app --port 8000 --reload
```

Open `http://localhost:8000` in a browser.

The `--reload` flag is recommended during development. It causes the server to restart automatically when any Python file is saved. Note that restarting the server does not close the Chrome window managed by the agent, since `BrowserAgent.stop()` is a deliberate no-op.

---

## Using the System

1. Open `http://localhost:8000`.
2. Paste the raw customer ticket text into the ticket field.
3. Enter the customer's Odoo database URL (e.g. `shsri.odoo.com`).
4. Optionally specify the Odoo version and any additional documentation context.
5. Click Run.
6. The progress tracker advances through five steps. A Chrome window will open automatically.
7. When the AI generates an investigation script, it pauses and displays the code in the UI for review. Click Approve to execute it or Skip to proceed with static findings only.
8. When the pipeline completes, click the download button to retrieve the Word report.

---

## Pipeline Steps

### Step 1 — Ticket Analysis

The raw ticket text is sent to Groq (llama-3.3-70b) with a structured extraction prompt. The model returns a JSON object containing:

- `summary`: one-sentence description of the issue
- `odoo_version`: detected version string or null
- `module`: the relevant Odoo module
- `error_message`: verbatim error text if present
- `steps_to_reproduce`: ordered list of reproduction steps
- `check_runbot`: boolean indicating whether Runbot verification is needed
- `config_keys_to_check`: list of configuration settings relevant to the issue

Groq is used here rather than Gemini because it has a significantly higher free-tier rate limit and consistently lower latency for structured JSON extraction tasks.

### Step 2 — Browser: Duplicate Database

The browser agent performs the following sequence:

1. Launches Chrome via subprocess using the analyst's session cookies. Chrome is started with `--remote-debugging-port=9225` so Playwright can connect via CDP without Playwright's own launcher interfering with the macOS keychain.
2. Navigates to `<database_url>/_odoo/support`.
3. If the support login screen appears (URL contains `/support/login`), the agent automatically fills the Login Reason field with `testing` and submits the form.
4. On the support gateway page, the agent first checks for existing duplicate databases (identified by `support-` in their URL). If found, it navigates to the most recent one. If none exist, it clicks the Duplicate button and polls until the duplicate appears.
5. If the duplicate's support page requires a second login, the agent repeats the login step.
6. The agent clicks through to enter the duplicate database and records the final URL as the active investigation context.

### Step 3 — Investigation

Inside the duplicate database:

1. The Odoo version is detected from the DOM.
2. The agent navigates to the Apps and Modules page and takes a screenshot.
3. A full-page base64 screenshot is captured and sent alongside the ticket context to the AI.

### Step 4 — AI Script Generation and HITL Approval

Gemini 2.5 Pro receives the screenshot, the ticket context, and the system prompt. It first produces a Functional Blueprint (a Markdown document containing a Request Summary, Diagnosis, and Solution Path) using real-time Google Search grounding against official Odoo documentation. It then translates the Solution Path into an async Playwright Python snippet.

The script is streamed to the frontend. The pipeline pauses and waits up to five minutes for the analyst to click Approve or Skip. If approved, the script is executed inside the duplicate database via Python's `exec()`. If skipped or timed out, the pipeline continues with static findings only.

This approval gate is mandatory and non-negotiable per the engineering decision recorded in `lessons.md` (Lesson 001). AI-generated code that modifies a live Odoo database must have explicit human authorisation before execution.

### Step 5 — Report Generation

`doc_writer.py` assembles a Microsoft Word document containing:

- Ticket summary, module, version, and error message
- The original ticket text
- All investigation findings as a numbered list
- Inline screenshots captured at each stage
- The full Resolution Guide synthesised by Gemini 2.5 Pro

The report is saved to the `output/` directory and made available for download through the `/download` endpoint.

---

## Directory Structure

```
ProjectSane/
|
|-- server.py              FastAPI application, job store, SSE streaming, HITL endpoints
|-- ai_agent.py            Groq and Gemini API clients, ticket extraction, script generation
|-- browser_agent.py       Playwright CDP browser management, Odoo navigation, screenshots
|-- doc_writer.py          Word report assembly using python-docx
|-- chrome_launcher.py     Legacy Chrome subprocess launcher (superseded by browser_agent.py)
|-- main.py                Standalone CLI entry point (pre-server architecture)
|-- setup_session.py       One-time utility for establishing a fresh Odoo browser session
|-- playwright_script.py   Utility for standalone Playwright testing
|-- requirements.txt       Python package dependencies
|-- lessons.md             Engineering decisions, safety rules, and architectural notes
|
|-- .env                   Local environment variables (not committed)
|-- .env.example           Environment variable template
|
|-- templates/
|   |-- index.html         Analyst web UI (step tracker, HITL approval, download button)
|
|-- output/                Generated Word reports and screenshots (not committed)
|
|-- .venv/                 Python virtual environment (not committed)
|-- .agent/                GSD workflow planning files (internal tooling)
|-- .planning/             GSD roadmap and phase tracking (internal tooling)
```

---

## API Reference

### POST /run

Accepts a form submission and returns a job ID.

**Form fields:**

| Field | Description |
|---|---|
| `ticket_text` | Raw customer ticket text |
| `db_url` | Customer's Odoo database URL |
| `odoo_version` | Optional. Overrides AI-detected version. |
| `documentation` | Optional. Additional context appended to the ticket. |

**Response:**

```json
{ "job_id": "uuid-string" }
```

### GET /stream/{job_id}

Server-Sent Events stream. Each event is a JSON object:

```json
{
  "step": 3,
  "message": "Investigating: checking config, modules, reproducing error...",
  "done": false,
  "report_path": "",
  "event_type": "message",
  "script_code": ""
}
```

When `event_type` is `approval_required`, the `script_code` field contains the AI-generated Playwright script for the analyst to review.

When `done` is `true`, the `report_path` field contains the server-side path to the generated Word document.

### POST /approve/{job_id}

Unblocks the HITL gate.

**Request body:**

```json
{ "approved": true }
```

### GET /download?path={report_path}

Returns the Word report file as an attachment.

---

## AI Model Routing

The system uses a hybrid routing strategy to balance speed, cost, and output quality.

| Task | Model | Provider | Rationale |
|---|---|---|---|
| Ticket extraction | llama-3.3-70b-versatile | Groq | High free-tier quota, consistent JSON output, low latency |
| Documentation search and blueprint | gemini-2.5-pro | Google Gemini | Native Google Search grounding, multimodal screenshot input |
| Playwright script generation | gemini-2.5-pro | Google Gemini | Code generation quality, access to blueprint context |
| Resolution synthesis | gemini-2.5-pro | Google Gemini | Long-context reasoning over all findings |

To route all AI calls to a local LM Studio server (e.g. for offline use or cost reduction), set `ACTIVE_PROVIDER=local` and `LOCAL_API_BASE=http://127.0.0.1:1234/v1` in `.env`. The system will use the OpenAI-compatible endpoint and send base64 screenshots as multimodal input if the local model supports it.

---

## Browser Session Management

The browser agent uses the following strategy to avoid the common failure modes encountered during development:

**Why subprocess launch instead of Playwright's launcher:**
Playwright's `launch_persistent_context` injects `--use-mock-keychain` into every Chrome launch command. On macOS, this flag prevents Chrome from accessing the system keychain, causing token decryption to fail and Chrome to exit immediately. Launching Chrome directly via `subprocess.Popen` avoids this flag entirely.

**Why a separate working profile:**
Chrome's security policy refuses `--remote-debugging-port` when `--user-data-dir` is set to the default Chrome data directory. The agent therefore maintains a separate directory (`ChromeAgentWork`) and copies only the session cookies from the analyst's active profile into it before each launch. The cookie file contains the `session_id` required to authenticate with Odoo without going through OAuth.

**Why a global browser singleton:**
Each ticket run reuses the same Chrome window. On the first run, Chrome is launched and a Playwright CDP connection is established. On subsequent runs, a new tab is opened inside the existing window. This prevents the profile lock that occurs when Chrome is killed and relaunched repeatedly.

---

## Known Limitations

- The system is designed for macOS only. Linux and Windows paths and keychain behaviour differ.
- If the analyst's Odoo `session_id` cookie expires between runs, the agent will land on a login page. A new cookie copy will be needed.
- Gemini 2.5 Pro has stricter rate limits on the free tier than Groq. Consecutive ticket runs within a short window may encounter 429 errors. The system retries with exponential backoff up to three times.
- Duplicate database creation can take several minutes on large Odoo databases. The agent polls for up to 60 seconds before reporting a timeout.
- The `exec()` path for AI script execution has no sandboxing. The HITL approval gate is the sole safety mechanism. Never approve scripts without reading them.

---

## Security Notes

AI-generated Playwright code executed via `exec()` runs with the full privileges of the Python process. It can navigate to any URL, click any button, and modify data in the Odoo database. The analyst is responsible for reviewing every generated script before approving it. The Skip option exists for this reason.

The `.env` file contains API keys and must never be committed to version control. It is listed in `.gitignore`.

---

## Development Notes

- All engineering decisions, safety rules, and architectural rationale are recorded in `lessons.md`.
- The server uses hot-reload in development mode. Saving any Python file causes the server to restart, but the Chrome window and CDP connection are maintained because the browser agent's `stop()` method is a no-op.
- Screenshots are saved to `output/` with timestamps. This directory grows over time and should be cleaned periodically.
- To kill a stale Chrome process on the agent's CDP port: `lsof -ti :9225 | xargs kill -9`

---

## License

Internal proprietary system. Not for public distribution.
