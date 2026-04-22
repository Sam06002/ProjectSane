# Project Sane — Engineering Lessons

---

## Lesson #002 — Hybrid API Routing (Option C) — IMPLEMENTED

**Date:** 2026-04-08
**Status:** ✅ Implemented

### The Decision

Implemented Option C (Hybrid Groq + Gemini Routing) per Delivery Report recommendation:

| Task | Model | Rationale |
|------|-------|-----------|
| `analyse_ticket()` | **Groq** `llama-3.3-70b` | Fast structured JSON extraction, high free quota (no rate limits) |
| `generate_playwright_execution()` | **Gemini 2.5 Pro** + Search | Quality code generation + live doc search |
| `synthesise_resolution()` | **Gemini 2.5 Pro** | High-quality synthesis, official doc grounding |

### Result
- Zero cost (both APIs on free tier)
- No rate limit blocking on ticket analysis
- Maintains high-quality synthesis and script generation
- Unblocks end-to-end testing immediately

---

## Lesson #001 — exec() Safety Flag (from Delivery #006)

**Date:** 2026-04-06
**Raised by:** Senior CTO (Claude)
**Status:** ⚠️ Mandatory for next implementation

### The Rule

Any feature that pipes **AI-generated code into Python's `exec()`** MUST include a hard safety confirmation step before execution. This is non-negotiable.

### Why

- LLMs can generate syntactically valid but semantically destructive Playwright code (e.g., clicking "Delete", "Archive", navigating to Settings and changing critical configs).
- The customer's duplicate database is live and writable. A bad AI script could corrupt data irreversibly.
- `exec()` has no sandboxing by default — it inherits the full process context including file system and network access.

### Implementation Pattern

When prompting the next engineer to implement this, the confirmation step must:

1. **Display the generated script** to Sam on the frontend UI before any execution begins.
2. **Wait for explicit human approval** — a "Run Script" button click, not a passive timeout.
3. **Only after approval** call `exec()` with the AI code.
4. Provide a **"Skip / Cancel"** option so Sam can bypass the AI script and let the report generate with only the static findings.

### Pseudocode Contract

```
ai_script = ai_agent.generate_playwright_execution(ticket_info, url)

# Show Sam the script in the UI and WAIT for his click
approved = await frontend.request_confirmation(
    title="AI-Generated Script — Review Before Execution",
    code=ai_script
)

if approved:
    exec(ai_script)   # safe — explicit human decision
else:
    findings.append("AI script execution skipped by user.")
```

### Frontend Requirement

The SSE stream should emit a new event type (e.g. `"confirm_required"`) containing the script text. The frontend must render it in a code block with "Approve" and "Skip" buttons, and POST Sam's decision back to a `/confirm/{job_id}` endpoint before the backend unblocks.

### Priority

This is a **blocking requirement** before the `exec()` path is used in any customer-facing workflow. Until implemented, the AI script generation should run in **dry-run / logging mode only** — generate and log the script to the report, but never execute it.

---
