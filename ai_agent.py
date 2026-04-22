import requests
import json
import time

# ── Master Brain Configuration (COO + CTO approved — v1.2) ───────────────────
SYSTEM_PROMPT = """
You are an 'Odoo Support Assistant' and 'Senior Functional Support' expert acting as the core intelligence of an automated troubleshooting agent. 

Your primary role is to assist support agents by analyzing customer tickets, autonomously navigating duplicate customer databases or 'Runbot' instances, and resolving complex functional issues within the Odoo ecosystem.

PURPOSE AND GOALS:
* Expert Functional Support: Provide high-level assistance for Odoo, leveraging deep knowledge of core modules.
* Problem Resolution: Resolve user tickets by diagnosing issues and offering precise, step-by-step functional solutions derived from official documentation.
* Source Fidelity: Base all guidance strictly on official Odoo Enterprise features. No custom code workarounds.

CRITICAL CONSTRAINTS:
1. VERSION SCOPE: You strictly support Odoo versions 16.0, 17.0, 18.0, 19.0, 19.1, 19.2, and 'master'. 
2. SEARCH GROUNDING: Before answering, YOU MUST use the Google Search tool to search for 'Odoo [VERSION] documentation [TICKET TOPIC]'. 
3. TONE: Maintain an authoritative, efficient, and precise Support Analyst tone. Use enterprise software terminology.

TWO-STEP GENERATION PIPELINE:

STEP 1: THE FUNCTIONAL BLUEPRINT
When asked to provide the functional steps to solve a ticket, you must strictly output your response starting with your internal reasoning, followed by the Markdown format.
Before outputting your final Markdown, you must write out your internal reasoning inside <thinking>...</thinking> tags. Look at the provided screenshot of the Odoo UI. This is your starting location. Inside your <thinking> tags, you MUST first state exactly what screen you are currently on. Then, formulate your step-by-step UI plan from this exact starting point. Explain what you searched for, what the Odoo documentation states, and how you plan to navigate the UI.

After the </thinking> tag, you must strictly output Markdown in this exact format:

### 1. Request Summary
[Restate the user's inquiry clearly: what the customer is requesting and what they want to achieve.]

### 2. Diagnosis & Cause
[Classify the query as an information request, configuration hurdle, or functional bug. Explain the standard behavior based on official documentation.]

### 3. Solution Path
[Provide precise, step-by-step UI navigation instructions to resolve the issue. Example: 'Go to **Settings** > **Translations** > **Languages**'. These exact steps will be used to write browser automation code, so menu accuracy is paramount.]

STEP 2: CODE TRANSLATION
When provided with a 'Solution Path', translate those exact steps into a robust, async Playwright Python snippet to automate the UI clicks inside the Odoo database.
"""


class AIAgent:
    def __init__(self, groq_api_key: str = None, gemini_api_key: str = None):
        # Option C: Hybrid Routing
        # Groq: fast, high quota — used for structured JSON extraction (analyse_ticket)
        # Gemini: quality + search grounding — used for synthesis & script generation
        self.groq_api_key = groq_api_key
        self.gemini_api_key = gemini_api_key

        # Groq configuration (fast extraction)
        self.groq_base_url = "https://api.groq.com/openai/v1/chat/completions"
        self.groq_model = "llama-3.3-70b-versatile"
        self.groq_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {groq_api_key}"
        } if groq_api_key else None

        # Gemini configuration (quality synthesis + search grounding)
        self.gemini_base_url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        self.gemini_model = "gemini-2.5-pro"
        self.gemini_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {gemini_api_key}"
        } if gemini_api_key else None

    def _call_groq(self, system: str, user: str, max_tokens: int = 1024,
                   retries: int = 3) -> str:
        """Call Groq API — fast, high quota, good for structured JSON extraction."""
        if not self.groq_api_key or not self.groq_headers:
            print("[Groq] No API key configured")
            return ""

        payload = {
            "model": self.groq_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1
        }
        wait = 5  # Groq has high rate limits, shorter wait
        for attempt in range(1, retries + 1):
            try:
                response = requests.post(self.groq_base_url, headers=self.groq_headers, json=payload)
                if response.status_code == 429:
                    print(f"[Groq] Rate limit (attempt {attempt}/{retries}). Waiting {wait}s...")
                    time.sleep(wait)
                    wait *= 2
                    continue
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]
            except Exception as e:
                if attempt == retries:
                    print(f"Error calling Groq after {retries} attempts: {e}")
                    return ""
                print(f"[Groq] Error attempt {attempt}: {e}. Retrying in {wait}s...")
                time.sleep(wait)
                wait *= 2
        return ""

    def _call_gemini(self, system: str, user: str, max_tokens: int = 1024,
                     retries: int = 3, model: str = None) -> str:
        """Call Gemini API — high quality, used for synthesis and search grounding."""
        if not self.gemini_api_key or not self.gemini_headers:
            print("[Gemini] No API key configured")
            return ""

        model = model or self.gemini_model
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1
        }
        wait = 10  # Gemini has stricter rate limits
        for attempt in range(1, retries + 1):
            try:
                response = requests.post(self.gemini_base_url, headers=self.gemini_headers, json=payload)
                if response.status_code == 429:
                    print(f"[Gemini/{model}] Rate limit (attempt {attempt}/{retries}). Waiting {wait}s...")
                    time.sleep(wait)
                    wait *= 2
                    continue
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]
            except Exception as e:
                if attempt == retries:
                    print(f"Error calling Gemini/{model} after {retries} attempts: {e}")
                    return ""
                print(f"[Gemini/{model}] Error attempt {attempt}: {e}. Retrying in {wait}s...")
                time.sleep(wait)
                wait *= 2
        return ""

    def analyse_ticket(self, ticket_text: str) -> dict:
        """
        Structured JSON extraction — uses Groq for fast, reliable structured output.
        SYSTEM_PROMPT is not used here because this task requires strict JSON.
        """
        extraction_prompt = (
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
        )

        # Option C: Use Groq for fast JSON extraction (high quota, proven reliable)
        response_text = self._call_groq(extraction_prompt, ticket_text)

        # Strip any possible markdown fences if the AI disobeys
        response_text = response_text.strip()
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
        response_text = response_text.strip()

        defaults = {
            "summary": None,
            "odoo_version": None,
            "module": None,
            "error_message": None,
            "steps_to_reproduce": [],
            "check_runbot": False,
            "config_keys_to_check": []
        }

        try:
            parsed = json.loads(response_text)
            for key in defaults:
                if key not in parsed:
                    parsed[key] = defaults[key]
            return parsed
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON from Gemini: {e}\nResponse was: {response_text}")
            return defaults

    async def generate_playwright_execution_async(self, ticket_info: dict, url: str, stream_callback=None, screenshot_b64: str=None) -> str:
        """
        Two-step pipeline — SYSTEM_PROMPT governs both steps.

        Step 1 (FUNCTIONAL BLUEPRINT): Gemini + Google Search Grounding searches
                the official Odoo docs in real-time and outputs a structured
                Markdown blueprint (Summary / Diagnosis / Solution Path).

        Step 2 (CODE TRANSLATION): SYSTEM_PROMPT instructs Gemini to translate
                the Solution Path into a raw async Playwright Python snippet.
        """
        from google import genai
        from google.genai import types

        odoo_version = ticket_info.get("odoo_version") or "17.0"
        topic = ticket_info.get("module") or ticket_info.get("summary") or "general feature"
        summary = ticket_info.get("summary", "")
        steps_requested = ticket_info.get("steps_to_reproduce", [])

        # ── Step 1: Search-grounded Functional Blueprint ───────────────────────
        # SYSTEM_PROMPT is passed as system_instruction so the model knows its role
        # and output format before it even reads the user message.
        step1_user = (
            f"Before answering, YOU MUST use the Google Search tool to search for "
            f"'Odoo {odoo_version} documentation {topic}'. "
            f"Based strictly on the official Odoo documentation you find, "
            f"produce the Functional Blueprint for this ticket.\n\n"
            f"Ticket summary: {summary}\n"
            f"Steps requested by analyst: {steps_requested}"
        )

        functional_blueprint = ""
        try:
            import time
            import asyncio
            import os
            
            provider = os.getenv("ACTIVE_PROVIDER", "gemini")
            
            if provider == "local":
                import openai
                client = openai.AsyncOpenAI(
                    base_url=os.getenv("LOCAL_API_BASE", "http://127.0.0.1:1234/v1"),
                    api_key="local" # LM studio ignores
                )
                
                user_msg_content = [{"type": "text", "text": step1_user}]
                if screenshot_b64:
                    user_msg_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}
                    })
                
                wait = 20
                for attempt in range(1, 4):
                    try:
                        response = await client.chat.completions.create(
                            model="local-model",
                            messages=[
                                {"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": user_msg_content}
                            ],
                            stream=True
                        )
                        in_thinking = False
                        last_stream_idx = 0
                        async for chunk in response:
                            if getattr(chunk.choices[0].delta, 'content', None):
                                text_chunk = chunk.choices[0].delta.content
                                functional_blueprint += text_chunk
                            if "<thinking>" in functional_blueprint:
                                start_idx = functional_blueprint.find("<thinking>") + len("<thinking>")
                                end_idx = functional_blueprint.find("</thinking>")
                                current_end = end_idx if end_idx != -1 else len(functional_blueprint)
                                
                                new_text = functional_blueprint[max(start_idx, last_stream_idx):current_end]
                                if new_text and stream_callback:
                                    await stream_callback("thinking_stream", new_text)
                                
                                last_stream_idx = max(start_idx, last_stream_idx) + len(new_text)
                        
                        print(f"[Step 1 — Local Model Blueprint]:\n{functional_blueprint[:500]}...")
                        break
                    except Exception as e:
                        print(f"[Step 1] Request failed (attempt {attempt}/3). Waiting {wait}s... {e}")
                        await asyncio.sleep(wait)
                        wait *= 2
                        if attempt == 3: raise
            
            else: # Gemini path
                client = genai.Client(api_key=self.gemini_api_key)
                
                contents = []
                if screenshot_b64:
                    from google.genai.types import Part
                    import base64
                    contents.append(Part.from_bytes(data=base64.b64decode(screenshot_b64), mime_type='image/png'))
                contents.append(step1_user)
                
                wait = 20
                for attempt in range(1, 4):
                    try:
                        response = await client.aio.models.generate_content_stream(
                            model=self.gemini_model,
                            contents=contents,
                            config=types.GenerateContentConfig(
                                system_instruction=SYSTEM_PROMPT,
                                tools=[types.Tool(google_search=types.GoogleSearch())],
                            ),
                        )
                        in_thinking = False
                        last_stream_idx = 0
                        async for chunk in response:
                            if chunk.text:
                                functional_blueprint += chunk.text
                            if "<thinking>" in functional_blueprint:
                                start_idx = functional_blueprint.find("<thinking>") + len("<thinking>")
                                end_idx = functional_blueprint.find("</thinking>")
                                current_end = end_idx if end_idx != -1 else len(functional_blueprint)
                                
                                new_text = functional_blueprint[max(start_idx, last_stream_idx):current_end]
                                if new_text and stream_callback:
                                    await stream_callback("thinking_stream", new_text)
                                
                                last_stream_idx = max(start_idx, last_stream_idx) + len(new_text)
                        
                        print(f"[Step 1 — Grounded Blueprint]:\n{functional_blueprint[:500]}...")
                        break
                    except Exception as e:
                        if "429" in str(e) or "quota" in str(e).lower():
                            print(f"[Step 1] Rate limit hit (attempt {attempt}/3). Waiting {wait}s...")
                            await asyncio.sleep(wait)
                            wait *= 2
                        else:
                            raise
        except Exception as e:
            print(f"[Step 1] Search grounding failed — falling back to ticket summary: {e}")
            functional_blueprint = (
                f"### 1. Request Summary\n{summary}\n\n"
                f"### 3. Solution Path\n"
                + "\n".join(f"- {s}" for s in steps_requested)
            )


        # ── Step 2: CODE TRANSLATION ───────────────────────────────────────────
        # SYSTEM_PROMPT already instructs the model on what Step 2 means.
        # We activate it by referencing 'Solution Path' from the blueprint.
        step2_user = (
            f"STEP 2 — CODE TRANSLATION\n\n"
            f"Using the Solution Path from the Functional Blueprint below, "
            f"generate a raw async Playwright Python snippet to automate the UI clicks "
            f"inside the Odoo database. The active Playwright Page is `self.page`, "
            f"already logged into Odoo at: {url}\n\n"
            f"Return ONLY raw Python code. No markdown fences. Under 20 lines.\n\n"
            f"--- FUNCTIONAL BLUEPRINT ---\n{functional_blueprint}"
        )

        import asyncio
        loop = asyncio.get_event_loop()
        import functools
        import os
        
        provider = os.getenv("ACTIVE_PROVIDER", "gemini")
        if provider == "local":
            async def call_local_step2():
                import openai
                c = openai.AsyncOpenAI(
                    base_url=os.getenv("LOCAL_API_BASE", "http://127.0.0.1:1234/v1"),
                    api_key="local"
                )
                r = await c.chat.completions.create(
                    model="local-model",
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": step2_user}
                    ]
                )
                return r.choices[0].message.content
            code = await call_local_step2()
        else:
            code = await loop.run_in_executor(None, functools.partial(self._call_gemini, SYSTEM_PROMPT, step2_user, 1024))

        # Sanitise markdown fences defensively
        for fence in ("```python", "```"):
            if code.startswith(fence):
                code = code[len(fence):]
        if code.endswith("```"):
            code = code[:-3]

        return code.strip()

    def synthesise_resolution(self, ticket_text: str, findings: str) -> str:
        """
        Final report synthesis — SYSTEM_PROMPT governs the output format
        (Request Summary / Diagnosis & Cause / Solution Path sections).
        """
        user_message = (
            "TICKET:\n"
            f"{ticket_text}\n\n"
            "INVESTIGATION FINDINGS (from automated browser agent):\n"
            f"{findings}\n\n"
            "Based on the findings above and official Odoo documentation, "
            "produce the full Resolution Guide in the required Markdown format."
        )

        return self._call_gemini(SYSTEM_PROMPT, user_message, max_tokens=2048)
