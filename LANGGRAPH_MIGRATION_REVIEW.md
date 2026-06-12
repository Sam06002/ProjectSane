# Project Sane — v1.6 LangGraph Migration Review

## Reviewed by: Engineering (Devin)
## For discussion with: CTO (Gemini), CEO (Claude), COO (Shivam)
## Scope: Post-migration audit of commit `df15053` ("integrate LangGraph state machine, LangSmith tracing")
## Type: Read-only audit + recommendations

### Severity legend
- ✅ No issue
- ⚠️ Minor — should fix, won't break production on its own
- 🔴 Critical — breaks functionality or undermines a documented guarantee

---

## 1. What the migration actually changed

The architecture moved from a **deterministic Playwright execution engine** to a
**LangChain / LangGraph multi-agent flow with LangSmith tracing**. Confirmed from
`df15053`:

- **Added:** `graph_agent.py`, `logger.py`, `monitor.py`, `schema.py`, `stream_manager.py`
- **Rewrote:** `browser_agent.py` (now a thin CDP session manager), `server.py`
- **Deprecated:** old `executor.py` → `.deprecated_v1_legacy/executor.py`
- **Deps:** added `langgraph`, `langsmith`, `langchain-google-genai`

**Important nuance:** Playwright was *not* removed. It still owns the real browser
session — CDP connect, support-gateway login, and duplicate-DB creation all run via
Playwright in `server.py` and `browser_agent.py`. What was removed is the per-step
*execution* engine (the old `executor.py` + `browser_agent.investigate_duplicate_db` /
`test_on_runbot` / `__ai_exec`). That responsibility now nominally belongs to the
LangGraph state machine.

Current v1.6 stack:
`Playwright (live browser) + Groq/Gemini + LangGraph (investigation) + LangSmith (tracing)`

---

## 2. Top issues (ranked)

### 🔴 #1 — The LangGraph executor is decoupled from the live browser
`graph_agent.py` nodes only use `ChatGoogleGenerativeAI`. The `executor_node`
(graph_agent.py:91) prompts Gemini to *"describe what was observed"* — it never
receives the live Playwright `page` or the captured screenshot, and never performs a
real click/navigation. So after `server.py` does the real work of opening the
duplicate DB, the actual "investigation" is LLM narration that is **not grounded in the
live sandbox**. The `reviewer_node`'s "is the error reproduced?" decision is therefore
judging a simulation, not the real DB.

> Impact: the core product promise ("access duplicate DB → reproduce error → find
> solution") is only half-wired post-migration. The browser reaches the DB, but the
> investigation no longer acts on it.

**Options to discuss:**
- (A) Pass the live `page` (and the base64 screenshot already captured in `server.py`)
  into `GraphState`, and give the executor node real tools (navigate/click/extract/
  screenshot) — effectively a tool-calling agent over Playwright. Reuses the
  `SELECTOR_REGISTRY` + `Action` schema that already exist.
- (B) Keep the graph as a reasoning/triage layer only, and re-introduce a deterministic
  Playwright executor (port the deprecated `executor.py`) for the steps in the validated
  `Plan`. Graph plans, executor executes, graph reviews real results.
- (C) Explicitly scope v1.6 as "analysis-only" and update docs to say no live execution
  happens yet (lowest effort, but reduces the tool to a reasoning assistant).

### 🔴 #2 — Validated `Plan` is produced but never executed
`planner.py` returns a strict Pydantic `Plan` (sequential steps, max 10, no raw
selectors) and `server.py` enforces a confidence gate (< 0.6 aborts). But the
validated `plan.steps` are never executed — execution is handed to the graph, which
ignores them and re-plans from `plan_full` (the raw Groq text). The careful schema
validation is currently decorative.

### 🔴 #3 — Documented HITL `exec()` approval gate is absent
`lessons.md` #001 calls the human approval gate before any AI-generated `exec()`
"non-negotiable / blocking." Post-migration there is no `/confirm` endpoint, no
`approval_required` SSE event, no Approve/Skip UI, and no `exec()` in the live path.
`ai_agent.generate_playwright_execution_async()` (ai_agent.py:320) — the thing that
gate was meant to protect — is now dead code. This is currently *safe* (nothing is
executed), but the docs promise a control the code doesn't implement. Decision needed:
remove the gate from the docs, or restore it alongside option #1A/#1B.

### ⚠️ #4 — `main.py` (CLI) is broken
Still imports/uses the pre-migration v2 API: `BrowserManager(headless=...)` (no such
arg), `investigate_duplicate_db()`, `test_on_runbot()`, `.screenshots`. Crashes on
start. Either fix it to drive the v3 pipeline or move it to `.deprecated_v1_legacy/`.

### ⚠️ #5 — Docs / version / model drift
- `README.md` + `CTO_REVIEW.md` say **v1.5**, Groq **llama-3.3-70b**, Gemini **2.5 Pro**.
- Code says **v3/v1.6**, Groq **llama-3.1-8b-instant**, Gemini **2.5-flash**.
- README says the user downloads a **Word (.docx)** report; the web flow never calls
  `doc_writer.generate_report` — the UI downloads **.txt**. `doc_writer.py` is only
  referenced by the broken `main.py`.
- `.env.example` defaults to `AI_PROVIDER=lmstudio` / `gemma-3-12b-it`, but the live
  path reads `GROQ_API_KEY` / `GEMINI_API_KEY` directly and ignores
  `AI_PROVIDER` / `ACTIVE_PROVIDER`.

### ⚠️ #6 — LangSmith tracing is config-only
`requirements.txt` adds `langsmith`; `.env.example` sets `LANGCHAIN_TRACING_V2=true`
with a placeholder `LANGCHAIN_API_KEY=lsv2_sane_trace_placeholder`. Tracing no-ops /
warns until a real key is set. No code explicitly creates traced runs beyond the env
auto-instrumentation. Confirm whether we want real LangSmith projects wired.

### ⚠️ #7 — Portability & hygiene
- `browser_agent.py` hardcodes macOS paths (`/Applications/Google Chrome.app`,
  `~/Library/.../Chrome/Profile 3/Cookies`). Not portable to Linux/Windows CI.
- Tests are smoke scripts only (`test_server.py`, `test_index.py`, `test_cdp.py`) — no
  assertions, no pytest suite, no CI workflow, no linter/pre-commit config.
- `server.py` is ~890 lines mixing routes, duplication-gate polling, SSE generators,
  and portal state machine — candidate for modular split.

---

## 3. Summary table

| Area | Status | Note |
|------|--------|------|
| Browser session (Playwright/CDP) | ✅ | Real, robust (self-healing, retries) |
| URL validation / confidence gate | ✅ | Solid input guards |
| LangGraph investigation grounding | 🔴 | Decoupled from live page (#1) |
| Validated Plan execution | 🔴 | Plan built, never executed (#2) |
| HITL exec approval gate | 🔴 | Documented, not implemented (#3) |
| CLI (`main.py`) | ⚠️ | Broken against v3 API (#4) |
| Docs / model / report drift | ⚠️ | v1.5 docs vs v1.6 code (#5) |
| LangSmith tracing | ⚠️ | Config-only placeholder (#6) |
| Portability / tests / CI | ⚠️ | macOS-only, smoke tests only (#7) |

---

## 4. Recommended next step for the team

The single highest-leverage decision is **#1**: do we want v1.6 to actually act on the
live duplicate DB, or is it intentionally an analysis-only reasoning layer for now?

- If **act on live DB** → I'd propose option **1B** (graph plans/reviews, deterministic
  Playwright executor runs the validated `Plan`). This reuses the existing `schema.py`
  `Action` model and the deprecated `executor.py` registry, makes #2 disappear, and
  gives the reviewer real results to judge. The HITL gate (#3) then bolts on cleanly
  before any state-mutating action.
- If **analysis-only for now** → we update docs (#5), delete/relocate dead code
  (`generate_playwright_execution_async`, `doc_writer.py`, `main.py`), and remove the
  HITL language from `lessons.md` until execution returns.

Either way, #4 (CLI) and #5 (docs) are cheap cleanups I can do immediately on approval.

Happy to take any of these as follow-up PRs once Gemini/Claude weigh in.
