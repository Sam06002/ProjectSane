# ANTIGRAVITY — CODE REVIEW REQUEST
## Project: Odoo Support Ticket Agent (Project Sane)
## Issued by: Senior CTO (Claude) + Chief of Operations (Sam)
## Date: 2026-04-11
## Type: Full Codebase Audit — Read Only

---

## YOUR ROLE FOR THIS TASK

This is a **read-only audit**. You are NOT writing any new code.
You are NOT fixing anything. You are NOT refactoring anything.
Your only job is to read every file, analyse it honestly, and produce a
structured report for the Senior CTO to review.

If you find something broken, document it. Do not fix it.
If you find something missing, document it. Do not add it.
Wait for instruction before touching any file.

---

## 1. WHAT TO REVIEW

Audit every file in the Project Sane workspace. For each file, answer the
questions in Section 2. At minimum, review these files:

```
main.py
server.py
ai_agent.py
browser_agent.py
chrome_launcher.py
setup_session.py
doc_writer.py / report_generator.py
templates/index.html
requirements.txt
.env.example
```

If additional files exist in the workspace not listed above, include them too.

---

## 2. REVIEW CRITERIA

For every file, evaluate against these five dimensions:

### A — Security
- Are any real API keys, passwords, or credentials hardcoded anywhere?
- Are there any secrets that could be accidentally committed to git?
- Does `.env.example` contain only placeholder values?
- Is user input from the web form sanitised before being used in code?
- Are there any `exec()` calls running without a human approval gate?

### B — Bugs & Failure Points
- Are there any known broken references (e.g. wrong class names, missing imports)?
- Are there any functions that could crash on real Odoo data?
- Are there any hardcoded selectors that are brittle or likely to break?
- Are there any missing `try/except` blocks around browser actions?
- Are there any async/await mismatches that could cause silent failures?
- Are there race conditions in the SSE streaming pipeline?

### C — Code Quality & Structure
- Are there duplicate functions doing the same thing across files?
- Are there unused imports or dead code?
- Are there functions that are too long (over 60 lines) and should be split?
- Is error handling consistent across all files?
- Are all class and method signatures consistent with how they are called?

### D — Alignment with Project Sane Vision
The core purpose is: **access duplicate DB → reproduce error → find solution**
- Does `browser_agent.py` reliably navigate to `/_odoo/support`?
- Does the duplicate DB creation logic handle all cases (new, existing, expired)?
- Does the vision loop actually steer navigation or is it still brittle?
- Does the final Word report contain enough information to resolve a ticket?
- Is the HITL (human-in-the-loop) approval gate working correctly?

### E — Inter-file Consistency
- Does `server.py` instantiate `AIAgent` and `BrowserAgent` with the correct
  signatures matching their current `__init__` definitions?
- Do data structures passed between files match what the receiving file expects?
- Does `doc_writer.py` / `report_generator.py` receive screenshots in the
  correct format from `browser_agent.py`?
- Are `.env` variable names consistent between `.env.example`, `server.py`,
  and `ai_agent.py`?

---

## 3. DELIVERABLE FORMAT

Produce a single structured Artifact with this exact format:

```
# CODE REVIEW REPORT — Project Sane
## Date: [today]
## Reviewed by: Engineering

---

## FILE: main.py
### A — Security
[findings or "No issues found"]
### B — Bugs & Failure Points
[findings or "No issues found"]
### C — Code Quality
[findings or "No issues found"]
### D — Vision Alignment
[findings or "No issues found"]
### E — Inter-file Consistency
[findings or "No issues found"]

---

## FILE: server.py
[same structure]

---

[repeat for every file]

---

## SUMMARY TABLE

| File | Security | Bugs | Quality | Vision | Consistency | Priority |
|------|----------|------|---------|--------|-------------|----------|
| main.py | ✅/⚠️/🔴 | ... | ... | ... | ... | High/Med/Low |
[one row per file]

---

## TOP 5 ISSUES (ranked by severity)

1. [Most critical issue — file, description, why it matters]
2. ...
3. ...
4. ...
5. ...

---

## FILES NOT REVIEWED (if any)
[list any files skipped and why]
```

### Severity legend:
- ✅ No issues
- ⚠️ Minor issue — won't break production but should be fixed
- 🔴 Critical issue — breaks functionality or is a security risk

---

## 4. OPERATING RULES FOR THIS TASK

1. **Read only** — do not edit, create, or delete any file
2. **Be honest** — if something is broken, say so clearly
3. **Be specific** — cite the exact function name, line concept, or variable
   where the issue exists. Vague findings are not useful.
4. **No fixes** — document issues only, wait for CTO instruction
5. **No summaries** — the Artifact must contain the full structured report,
   not a high-level overview

---

## 5. VERIFICATION

Before submitting the Artifact, confirm:
- [ ] Every file listed in Section 1 has been reviewed
- [ ] Summary table is complete with one row per file
- [ ] Top 5 issues are ranked by severity
- [ ] No code was modified during this review

---

## END OF CODE REVIEW REQUEST

Label your Artifact:
**"CODE REVIEW REPORT — Project Sane Full Audit"**
