"""
ai_agent.py — Dual Cloud Engine for Project Sane v3.

Engine 1 ── Groq Triage          (llama-3.1-8b-instant)
    Fast JSON metadata extraction from raw support tickets.
    Synchronous streaming generator, ~200-400ms latency.

Engine 2 ── Gemini AI Studio      (gemini-2.5-flash)
    Multimodal visual conductor. Receives base64 screenshots +
    Groq's JSON payload, diagnoses Odoo DB state, and outputs
    a structured Functional Blueprint + Playwright resolution code.
    Uses response_schema for deterministic JSON output.
"""

from __future__ import annotations

import json
import re
import time
import os
from typing import Optional, Generator

# ── Engine 1: Official Groq SDK ───────────────────────────────────────────────
from groq import Groq  # Verified import

# ── Engine 2: Google Generative AI SDK ───────────────────────────────────────
from google import genai
from google.genai import types

SYSTEM_PROMPT = """
You are the elite 'Odoo Senior Functional Support Expert' and Multi-Modal Troubleshooting Engine for Project Sane. 
Your mission is to analyze customer issues, inspect the live database UI via screenshots, and plan exact functional solutions.

=======================================================================
CORE MANDATE 1: STRICT BOUNDED SEARCH GROUNDING (ANTI-HALLUCINATION)
=======================================================================
Before proposing any functional solution or step, YOU MUST execute a Google Search.
* Your search queries MUST be explicitly prefixed with: site:odoo.com/documentation/
* Example: "site:odoo.com/documentation/17.0 inventory valuation configuration"
* CRITICAL: You are strictly FORBIDDEN from using or referencing data from third-party blogs, forums, Reddit, or unauthorized video tutorials. If a solution is not documented in the official Odoo Enterprise manual for that exact version, it does not exist.

=======================================================================
CORE MANDATE 2: VISUAL ANCHORING GATE (THE "EYES")
=======================================================================
You will be given a base64 screenshot of the current Odoo sandbox landing page alongside the customer ticket.
* Before writing a single execution step, you must visually inspect the screen.
* In your thinking block, you must explicitly identify:
  1. The exact App/Module layout currently active.
  2. Visible top navigation menus and sidebar elements.
  3. Any blocking elements (e.g., unexpected welcome wizards, error modals, setup popups).
* If the UI does not match the starting requirements of the ticket, your first planned steps must explicitly navigate back to the Main Home Dashboard.

=======================================================================
TWO-STEP DISCIPLINED OUTPUT FORMAT
=======================================================================
You must stream your output strictly adhering to these two phases:

<thinking>
[PHASE 1: VISUAL TRACE]
* State exactly what screen/app is visible in the provided screenshot. Identify any discrepancies between this screen and the ticket's target module.

[PHASE 2: DOCUMENTATION SYNTHESIS]
* Detail the exact search queries executed under site:odoo.com/documentation/.
* Cite the specific functional rules, settings paths, or configuration parameters retrieved from the documentation for Odoo Version [VERSION].

[PHASE 3: TARGETED ACTION BLUEPRINT]
* Map out the logical path from the current screen to the final destination.
* Prioritize targeting stable, persistent elements (e.g., string matching on button names like 'Settings', 'Save', or explicit tags like data-menu-xmlid) over fragile, dynamic CSS class names.
</thinking>

### 1. Request Summary
[Provide a highly focused technical distillation of the customer's exact functional goal]

### 2. Diagnosis & Cause
[Classify as: Configuration Hurdle, Information Request, or Functional Bug. Detail the underlying root cause based strictly on official Odoo mechanics]

### 3. Solution Path
[Provide a precise, bulleted list of UI navigation steps. Every step must be unambiguous. Example: 
* Click the main App Switcher button in the top left corner.
* Select the **Settings** App from the dashboard menu.
* Locate the **Invoicing** section in the left sidebar and click it.
* Check the box labeled **Automatic Post** under the generic settings block.]
"""


class AIAgent:
    """
    Dual Cloud Engine coordinator.

    Engine 1 (Groq)   — ticket triage, streaming JSON extraction
    Engine 2 (Gemini) — multimodal diagnosis, blueprint + code generation
    """

    def __init__(
        self,
        groq_api_key: str = None,
        gemini_api_key: str = None,
        # Legacy provider fields kept for backward compatibility with server.py
        ai_provider: str = "groq",
        ai_model: str = "llama-3.1-8b-instant",
        vision_provider: str = "gemini",
        vision_model: str = "gemini-2.5-flash",
    ):
        # ── Engine 1: Groq SDK client ─────────────────────────────────────────
        self._groq_key = groq_api_key or os.getenv("GROQ_API_KEY", "")
        self._groq_model = "llama-3.1-8b-instant"
        self._groq_client: Optional[Groq] = None
        if self._groq_key:
            self._groq_client = Groq(api_key=self._groq_key)

        # ── Engine 2: Gemini SDK client ───────────────────────────────────────
        self._gemini_key = gemini_api_key or os.getenv("GEMINI_API_KEY", "")
        self._gemini_model = "gemini-2.5-flash"
        self._gemini_client: Optional[genai.Client] = None
        if self._gemini_key:
            self._gemini_client = genai.Client(api_key=self._gemini_key)

        # Legacy aliases (server.py still references these)
        self.ai_provider = ai_provider
        self.ai_model = self._groq_model
        self.vision_provider = vision_provider
        self.vision_model = self._gemini_model
        self.groq_api_key = self._groq_key
        self.gemini_api_key = self._gemini_key

    # ══════════════════════════════════════════════════════════════════════════
    # ENGINE 1 — GROQ TRIAGE (llama-3.1-8b-instant)
    # ══════════════════════════════════════════════════════════════════════════

    def _groq_stream(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        run_logger=None,
        purpose: str = "",
    ) -> Generator[str, None, None]:
        """
        Engine 1 core — streams tokens from Groq using the official SDK.
        Falls back to empty string on any error; always logs via RunLogger.
        """
        if not self._groq_client:
            print("[Groq] No API key configured — skipping Engine 1 call.")
            yield ""
            return

        full_response = ""
        error_str: Optional[str] = None
        t0 = time.time()

        is_local = os.getenv("ACTIVE_PROVIDER") == "local"
        if is_local:
            max_tokens = 8192

        try:
            stream = self._groq_client.chat.completions.create(
                model=self._groq_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens,
                temperature=0.1,
                stream=True,
            )
            for chunk in stream:
                token = chunk.choices[0].delta.content or ""
                if token:
                    full_response += token
                    yield token

        except Exception as e:
            error_str = str(e)
            # Detect Groq rate-limit specifically for the logger
            if "429" in error_str or "rate_limit" in error_str.lower():
                print(f"[Groq Engine 1] Rate limit hit: {e}")
            else:
                print(f"[Groq Engine 1] Error: {e}")
            yield ""

        finally:
            latency_ms = (time.time() - t0) * 1000
            if run_logger is not None:
                payload = {
                    "model": self._groq_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.1,
                    "stream": True,
                }
                run_logger.log_llm_call(
                    model=self._groq_model,
                    provider="groq",
                    system_prompt=system,
                    user_prompt=user,
                    raw_response=full_response,
                    latency_ms=latency_ms,
                    purpose=purpose,
                    error=error_str,
                    final_payload=payload,
                )

    def analyse_ticket_stream(
        self, ticket_text: str, run_logger=None
    ) -> Generator[str, None, None]:
        """
        Engine 1 — Streaming ticket triage.
        Yields tokens. Caller must collect full_response and call _parse_ticket_json().
        """
        system = (
            "You are an expert Odoo support analyst. Think through the ticket carefully, "
            "then extract structured information. First share your reasoning, then on a new line "
            "output a JSON block wrapped in ```json ... ``` with these exact keys:\n"
            '- "summary": one sentence describing the issue\n'
            '- "odoo_version": the Odoo version mentioned (e.g. "17.0") or null\n'
            '- "module": the Odoo module involved or null\n'
            '- "error_message": the exact error text if present, or null\n'
            '- "steps_to_reproduce": list of steps, or []\n'
            '- "check_runbot": true if standard behaviour needs verification, false otherwise\n'
            '- "config_keys_to_check": list of config settings to check, or []'
        )
        yield from self._groq_stream(
            system, ticket_text, max_tokens=1024,
            run_logger=run_logger, purpose="ticket_triage_groq",
        )

    def generate_plan_stream(
        self, ticket_text: str, ticket_info: dict, run_logger=None
    ) -> Generator[str, None, None]:
        """
        Engine 1 — Streaming investigation plan generation.
        Yields tokens forming a numbered action list.
        """
        system = (
            "You are an expert Odoo support analyst planning a browser investigation. "
            "Think through the ticket carefully, then write a clear numbered investigation plan. "
            "First share your reasoning about what could be causing the issue. "
            "Then write the plan as a numbered list of concrete browser actions. "
            "Maximum 8 steps. Each step must be a single specific action."
        )
        user = (
            f"Ticket summary: {ticket_info.get('summary', '')}\n"
            f"Module: {ticket_info.get('module', '')}\n"
            f"Odoo version: {ticket_info.get('odoo_version', '')}\n"
            f"Error: {ticket_info.get('error_message', '')}\n"
            f"Steps to reproduce: {ticket_info.get('steps_to_reproduce', [])}\n\n"
            "Write your reasoning and then the investigation plan."
        )
        yield from self._groq_stream(
            system, user, max_tokens=512,
            run_logger=run_logger, purpose="plan_generation_groq",
        )

    def analyse_ticket(self, ticket_text: str, run_logger=None) -> dict:
        """
        Engine 1 — Synchronous (non-streaming) ticket triage.
        Collects stream internally and returns parsed JSON dict.
        Used by main.py CLI path.
        """
        full_response = ""
        for token in self._groq_stream(
            system=(
                "You are an expert Odoo support analyst. Extract structured information from the\n"
                "support ticket. Return ONLY valid JSON with these exact keys:\n"
                '- "summary": one sentence describing the issue\n'
                '- "odoo_version": the Odoo version mentioned (e.g. "17.0") or null if not found\n'
                '- "module": the Odoo module involved (e.g. "Accounting", "Inventory") or null\n'
                '- "error_message": the exact error text if present, or null\n'
                '- "steps_to_reproduce": list of steps to reproduce the issue, or empty list []\n'
                '- "check_runbot": true if standard Odoo behaviour needs verification, false otherwise\n'
                '- "config_keys_to_check": list of configuration settings to check, or empty list []\n'
                "Return only the JSON object. No explanation, no markdown, no code fences."
            ),
            user=ticket_text,
            max_tokens=1024,
            run_logger=run_logger,
            purpose="ticket_triage_groq_sync",
        ):
            full_response += token
        return self._parse_ticket_json(full_response)

    def _parse_ticket_json(self, full_response: str) -> dict:
        """Extract and parse the JSON block from a streamed Engine 1 response."""
        defaults = {
            "summary": "Could not parse ticket summary",
            "odoo_version": None,
            "module": None,
            "error_message": None,
            "steps_to_reproduce": [],
            "check_runbot": False,
            "config_keys_to_check": [],
        }
        # Try ```json ... ``` fence first
        match = re.search(r"```json\s*(.*?)\s*```", full_response, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                for key in defaults:
                    if key not in parsed:
                        parsed[key] = defaults[key]
                return parsed
            except Exception:
                pass
        # Fallback: bare { ... } block
        match = re.search(r"\{.*\}", full_response, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                for key in defaults:
                    if key not in parsed:
                        parsed[key] = defaults[key]
                return parsed
            except Exception:
                pass
        return defaults

    # ══════════════════════════════════════════════════════════════════════════
    # ENGINE 2 — GEMINI AI STUDIO (gemini-2.5-flash)
    # ══════════════════════════════════════════════════════════════════════════

    async def generate_playwright_execution_async(
        self,
        ticket_info: dict,
        url: str,
        stream_callback=None,
        screenshot_b64: str = None,
        run_logger=None,
    ) -> str:
        """
        Engine 2 — Multimodal visual conductor.

        Step 1: Receives Groq's JSON + base64 screenshot → produces a
                Functional Blueprint (Request Summary / Diagnosis / Solution Path)
                using Google Search grounding against official Odoo docs.

        Step 2: Translates the Solution Path into a raw async Playwright snippet.

        Args:
            ticket_info:    Dict produced by Engine 1 (Groq triage).
            url:            Active Odoo duplicate DB URL (already authenticated).
            stream_callback: async callable(event_type, text_chunk) for SSE streaming.
            screenshot_b64: Base64 PNG of the current Odoo DB screen.
            run_logger:     RunLogger instance for structured audit trail.

        Returns:
            Raw Python string — the Playwright automation snippet.
        """
        import asyncio
        import base64

        if not self._gemini_client:
            print("[Gemini Engine 2] No API key — cannot generate execution plan.")
            return ""

        odoo_version = ticket_info.get("odoo_version") or "17.0"
        topic = ticket_info.get("module") or ticket_info.get("summary") or "general feature"
        summary = ticket_info.get("summary", "")
        steps_requested = ticket_info.get("steps_to_reproduce", [])

        # ── STEP 1: Search-Grounded Functional Blueprint ───────────────────────
        step1_user = (
            f"Before answering, YOU MUST use the Google Search tool to search for "
            f"'Odoo {odoo_version} documentation {topic}'. "
            f"Based strictly on the official Odoo documentation you find, "
            f"produce the Functional Blueprint for this ticket.\n\n"
            f"Ticket summary: {summary}\n"
            f"Steps requested by analyst: {steps_requested}"
        )

        # Build multimodal contents list (screenshot + text)
        contents = []
        if screenshot_b64:
            try:
                contents.append(
                    types.Part.from_bytes(
                        data=base64.b64decode(screenshot_b64),
                        mime_type="image/png",
                    )
                )
            except Exception as e:
                print(f"[Gemini Engine 2] Screenshot decode failed: {e}")
        contents.append(step1_user)

        functional_blueprint = ""
        step1_error: Optional[str] = None
        t0 = time.time()

        is_local = os.getenv("ACTIVE_PROVIDER") == "local"
        config_kwargs = {
            "system_instruction": SYSTEM_PROMPT,
            "tools": [types.Tool(google_search=types.GoogleSearch())]
        }
        if is_local:
            config_kwargs["temperature"] = 0.1
            config_kwargs["max_output_tokens"] = 8192

        wait = 20
        for attempt in range(1, 4):
            try:
                response = await self._gemini_client.aio.models.generate_content_stream(
                    model=self._gemini_model,
                    contents=contents,
                    config=types.GenerateContentConfig(**config_kwargs),
                )

                last_stream_idx = 0
                async for chunk in response:
                    if chunk.text:
                        functional_blueprint += chunk.text

                    # Stream <thinking> tokens to frontend in real-time
                    if "<thinking>" in functional_blueprint:
                        start_idx = functional_blueprint.find("<thinking>") + len("<thinking>")
                        end_idx = functional_blueprint.find("</thinking>")
                        current_end = end_idx if end_idx != -1 else len(functional_blueprint)
                        new_text = functional_blueprint[
                            max(start_idx, last_stream_idx):current_end
                        ]
                        if new_text and stream_callback:
                            await stream_callback("thinking_stream", new_text)
                        last_stream_idx = max(start_idx, last_stream_idx) + len(new_text)

                print(f"[Gemini Engine 2 — Step 1 Blueprint]:\n{functional_blueprint[:300]}...")
                break

            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "quota" in err_str.lower():
                    print(
                        f"[Gemini Engine 2] Rate limit (attempt {attempt}/3). "
                        f"Waiting {wait}s..."
                    )
                    if run_logger:
                        run_logger.log_engine2_rate_limit(
                            attempt=attempt, wait_s=wait, error=err_str
                        )
                    await asyncio.sleep(wait)
                    wait *= 2
                else:
                    step1_error = err_str
                    print(f"[Gemini Engine 2] Step 1 fatal error: {e}")
                    raise

        # Log Step 1 call
        if run_logger:
            latency_ms = (time.time() - t0) * 1000
            run_logger.log_llm_call(
                model=self._gemini_model,
                provider="gemini",
                system_prompt=SYSTEM_PROMPT,
                user_prompt=step1_user,
                raw_response=functional_blueprint,
                latency_ms=latency_ms,
                purpose="blueprint_generation_gemini",
                error=step1_error,
            )

        # Graceful fallback if Step 1 produced nothing
        if not functional_blueprint.strip():
            functional_blueprint = (
                f"### 1. Request Summary\n{summary}\n\n"
                f"### 3. Solution Path\n"
                + "\n".join(f"- {s}" for s in steps_requested)
            )

        # ── STEP 2: Code Translation ───────────────────────────────────────────
        step2_user = (
            f"STEP 2 — CODE TRANSLATION\n\n"
            f"Using the Solution Path from the Functional Blueprint below, "
            f"generate a raw async Playwright Python snippet to automate the UI clicks "
            f"inside the Odoo database. The active Playwright Page is `self.page`, "
            f"already logged into Odoo at: {url}\n\n"
            f"Return ONLY raw Python code. No markdown fences. Under 20 lines.\n\n"
            f"--- FUNCTIONAL BLUEPRINT ---\n{functional_blueprint}"
        )

        code = ""
        step2_error: Optional[str] = None
        t1 = time.time()

        config_kwargs_2 = {"system_instruction": SYSTEM_PROMPT}
        if is_local:
            config_kwargs_2["temperature"] = 0.1
            config_kwargs_2["max_output_tokens"] = 8192

        try:
            response2 = await self._gemini_client.aio.models.generate_content(
                model=self._gemini_model,
                contents=[step2_user],
                config=types.GenerateContentConfig(**config_kwargs_2),
            )
            code = response2.text or ""
        except Exception as e:
            step2_error = str(e)
            print(f"[Gemini Engine 2] Step 2 error: {e}")
            if run_logger:
                run_logger.log_engine2_schema_exception(
                    context="step2_code_translation", error=str(e)
                )

        # Log Step 2 call
        if run_logger:
            latency_ms = (time.time() - t1) * 1000
            run_logger.log_llm_call(
                model=self._gemini_model,
                provider="gemini",
                system_prompt=SYSTEM_PROMPT,
                user_prompt=step2_user,
                raw_response=code,
                latency_ms=latency_ms,
                purpose="code_translation_gemini",
                error=step2_error,
            )

        # Sanitise any markdown fences defensively
        for fence in ("```python", "```"):
            if code.startswith(fence):
                code = code[len(fence):]
        if code.endswith("```"):
            code = code[:-3]

        return code.strip()

    def synthesise_resolution(
        self, ticket_text: str, findings: str, run_logger=None
    ) -> str:
        """
        Engine 2 — Synchronous final report synthesis (used by main.py CLI).
        Runs Gemini synchronously via the standard (non-async) client method.
        """
        if not self._gemini_client:
            return "Gemini not configured — cannot synthesise resolution."

        user_message = (
            "TICKET:\n"
            f"{ticket_text}\n\n"
            "INVESTIGATION FINDINGS (from automated browser agent):\n"
            f"{findings}\n\n"
            "Based on the findings above and official Odoo documentation, "
            "produce the full Resolution Guide in the required Markdown format."
        )

        code = ""
        error_str: Optional[str] = None
        t0 = time.time()
        wait = 10

        is_local = os.getenv("ACTIVE_PROVIDER") == "local"
        config_kwargs = {"system_instruction": SYSTEM_PROMPT}
        if is_local:
            config_kwargs["temperature"] = 0.1
            config_kwargs["max_output_tokens"] = 8192

        for attempt in range(1, 4):
            try:
                response = self._gemini_client.models.generate_content(
                    model=self._gemini_model,
                    contents=[user_message],
                    config=types.GenerateContentConfig(**config_kwargs),
                )
                code = response.text or ""
                break
            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "quota" in error_str.lower():
                    print(
                        f"[Gemini Engine 2] Rate limit on synthesis "
                        f"(attempt {attempt}/3). Waiting {wait}s..."
                    )
                    if run_logger:
                        run_logger.log_engine2_rate_limit(
                            attempt=attempt, wait_s=wait, error=error_str
                        )
                    time.sleep(wait)
                    wait *= 2
                else:
                    print(f"[Gemini Engine 2] Synthesis error: {e}")
                    break

        if run_logger:
            latency_ms = (time.time() - t0) * 1000
            run_logger.log_llm_call(
                model=self._gemini_model,
                provider="gemini",
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_message,
                raw_response=code,
                latency_ms=latency_ms,
                purpose="resolution_synthesis_gemini",
                error=error_str,
            )

        return code
