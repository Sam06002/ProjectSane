# Project Sane — Autonomous Odoo Support Agent
> **Version 1.6** | High-Fidelity Multi-Agent State Machine & Stateful Memory Engine

---

## 📌 Executive Overview

Project Sane is an enterprise-grade autonomous optimization and troubleshooting platform designed to eliminate manual replication overhead from Odoo functional support workflows. 

By ingesting raw customer support tickets, the system securely authenticates into isolated staging databases using active, persistent employee sessions. It then orchestrates a multi-agent network that captures live visual context, evaluates user interface states against official documentation, executes precise resolution sequences, and auto-generates polished, client-ready investigation reports.

---

## ⚙️ Core Architecture (The v1.6 Engine)

Project Sane splits cognitive operations across a synchronized **Dual-Cloud Engine** managed by an asynchronous **LangGraph State Machine** to guarantee non-linear reflection, self-healing retries, and complete data safety.

```
   [ Start: Ticket Submission ]
                │
                ▼
      +───────────────────+
      |   Triage Node     | <── Groq (llama-3.1-8b-instant) Fast JSON Parsing
      +─────────┬─────────+
                │
                ▼
      +───────────────────+
      |   Planner Node    | <── Gemini 2.5-Flash + SQLite Memory Initialization
      +─────────┬─────────+
                │
                ▼
      +───────────────────+
┌───> |   Executor Node   | <── Playwright Dynamic CDP Browser Navigation
│     +─────────┬─────────+
│               │ Takes Live base64 Screen Capture ("Eyes")
│               ▼
│     +───────────────────+
└──── |   Reviewer Node   | ───► [ Success: Error Reproduced ]
      +───────────────────+                  │
        (Max 3 Iterations)                   ▼
                                   - Compile Document (.docx)
                                   - Commit Resolution to SQLite DB
```

### Hybrid Model Routing Strategy

| Layer | Responsibility | Model Vector | Operational Rationale |
| :--- | :--- | :--- | :--- |
| **Engine 1** | Ticket Triage & Metadata Parsing | `llama-3.1-8b-instant` (Groq) | Ultra-low latency structural JSON extraction. |
| **Engine 2** | Multi-Modal Visual Diagnosis | `gemini-2.5-flash` (Google GenAI) | Native Google Search Grounding & base64 viewport processing. |
| **State Layer** | Iterative Loop Orchestration | `LangGraph` State Machine | Enforces a hard 3-retry ceiling with programmatic feedback loops. |
| **Memory Layer** | Long-Term Context Retention | `SQLite3` Cache Engine | Sub-millisecond persistence mapping past fixes locally. |

---

## 📂 Logical Directory Layout

```
ProjectSane/
│
├── server.py              # FastAPI application web backend, SSE streaming token manager
├── graph_agent.py         # LangGraph state machine (Planner, Executor, Reviewer nodes)
├── ai_agent.py            # Dual-Cloud SDK endpoint initializations (Groq + Gemini)
├── browser_agent.py       # Playwright CDP session manager & self-healing browser core
├── memory_store.py        # SQLite persistence layer mapping historical resolutions
├── doc_writer.py          # Microsoft Word report generation engine (python-docx)
├── chrome_launcher.py     # Detached background browser process launcher
├── setup_session.py       # One-time interactive proxy script for employee cookie caching
├── schema.py              # Strict Pydantic input/output safety validation schemas
│
├── templates/
│   └── index.html         # Premium asynchronous dark-mode front-end dashboard
├── logs/
│   ├── sane_memory.db     # SQLite persistence database binary file
│   └── run_index.json     # Machine-readable sequential execution run ledger
└── output/                # Generated client .docx reports and timeline screenshots
```

---

## 🚀 Quick Start Pipeline

### 1. Environmental Setup
Initialize your clean virtual workspace and install our pinned execution dependencies:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Credentials

Generate a local environment parameters configuration file:

```bash
cp .env.example .env
```

Open your newly created `.env` file and insert your dedicated API keys:

```env
GROQ_API_KEY=gsk_your_groq_key_here
GEMINI_API_KEY=AIzaSy_your_gemini_key_here
```

### 3. Establish Persistent Authentication Session

Execute our one-time secure authentication proxy script. This opens a dedicated, detached instance of Chrome to cache your active Odoo employee session cookies securely (completely separate from your primary browsing instances):

```bash
python setup_session.py
```

*Log into your Odoo employee gateway within the 120-second window. Once completed, your profile is permanently cached.*

### 4. Launch the Platform

Boot the asynchronous platform orchestration server:

```bash
uvicorn server:app --port 8000 --reload
```

Navigate your browser to `http://localhost:8000` to interact with the executive operations dashboard.

---

## 🔒 Security Invariants & Structural Safety

Project Sane is engineered around an uncompromising defense-in-depth layout to prevent systemic compromise or rogue data manipulation:

1. **Zero Raw Executions:** The system rejects raw code generation strings inside the structural planning phase. Browser navigation runs through strict, declarative Pydantic schemas protecting your application parameters from input injection risks.
2. **Staging Database Isolation Gate:** The browser engine automatically detects and routes incoming workflows through isolated `support-` duplicate databases, keeping your production data entirely untouched.
3. **Visual Observability Monitoring:** The `monitor.py` Observation Layer runs concurrent runtime invariant checking across the event stream, immediately broadcasting error nodes to your dashboard via Server-Sent Events (SSE).
4. **Automated Tracing Alignment:** Connecting valid keys to the `LANGCHAIN_API_KEY` block automatically routes comprehensive nested execution trees to your LangSmith dashboard for flawless programmatic audits.

---

*Internal Proprietary System — Project Sane Engineering Core 2026.*
