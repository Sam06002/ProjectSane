"""
planner.py — LLM Interaction and Plan Generation for Project Sane v3.

Routed to Engine 2: Gemini 2.5-flash.
Enforces strict JSON output matching the Pydantic Plan schema.
"""

import logging
import os
import asyncio
from google import genai
from google.genai import types
from pydantic import ValidationError

from schema import Plan


logger = logging.getLogger(__name__)

# System prompt is strictly engineered to guide Gemini in generating the ideal Odoo plan.
SYSTEM_PROMPT = """
You are a highly analytical Odoo Support AI and expert functional analyst.
Analyze the provided Odoo support ticket and database URL, and generate a precise, step-by-step browser investigation plan to reproduce, diagnose, and isolate the reported functional issue.

Odoo Navigation Rules:
- To access modules, navigate to standard routes. For example:
  - Contacts app: Navigate to "/odoo/contacts" or "/odoo/action-contacts" or "/odoo?action=contacts".
  - CRM app: Navigate to "/odoo/crm".
  - Sales app: Navigate to "/odoo/sales".
- Never navigate to settings (/odoo/settings) unless the issue is explicitly configuration-based or you need to inspect user views/fields.
- To act on elements, use clear click targets (e.g. "New" or "Create" or "Save").
- To extract data, use descriptive labels (e.g., "Warning banner text" or "Email field").

Output MUST strictly conform to the provided Plan schema.
"""

class Planner:
    def __init__(self, api_key: str = None):
        resolved_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.client = genai.Client(api_key=resolved_key)
        self.model = "gemini-2.5-flash"

    async def generate_plan(self, ticket_text: str, database_url: str) -> Plan:
        """
        Calls Gemini 2.5-flash to generate a structured investigation plan.
        Leverages native schema constraint to guarantee perfect Pydantic compliance.
        """
        import knowledge.modules
        import knowledge.navigation
        import knowledge.settings
        import knowledge.issues
        import json

        # Format structured Odoo knowledge context for prompt grounding
        knowledge_context = (
            f"Odoo Module Metadata:\n{json.dumps(knowledge.modules.MODULES, indent=2)}\n\n"
            f"Odoo Navigation Routes:\n{json.dumps(knowledge.navigation.NAVIGATION_PATHS, indent=2)}\n\n"
            f"Known Odoo Configurations:\n{json.dumps(knowledge.settings.KNOWN_SETTINGS, indent=2)}\n\n"
            f"Known Odoo Issues & Version Rules:\n{json.dumps(knowledge.issues.KNOWN_ISSUES, indent=2)}"
        )

        user_message = (
            f"Database URL: {database_url}\n\n"
            f"TICKET:\n{ticket_text}\n\n"
            f"STRUCTURED ODOO KNOWLEDGE CONTEXT:\n{knowledge_context}\n\n"
            "Use the provided Odoo knowledge context to generate a structured, deterministic JSON execution plan to investigate this issue."
        )

        try:
            logger.info("Calling Gemini for structured plan generation...")
            
            max_retries = 3
            response = None
            
            for attempt in range(max_retries):
                try:
                    response = await asyncio.to_thread(
                        self.client.models.generate_content,
                        model=self.model,
                        contents=[
                            types.Content(role="user", parts=[types.Part.from_text(text=f"{SYSTEM_PROMPT}\n\n{user_message}")])
                        ],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=Plan,
                            temperature=0.1,
                        )
                    )
                    content = response.text if response else None
                    if not content:
                        raise ValueError("Gemini returned empty content.")
                        
                    logger.info("Parsing and validating structured Plan...")
                    plan = Plan.model_validate_json(content)
                    return plan
                except Exception as e:
                    error_msg = str(e)
                    if ("503" in error_msg or "429" in error_msg or "UNAVAILABLE" in error_msg or "RESOURCE_EXHAUSTED" in error_msg) and attempt < max_retries - 1:
                        backoff = 2 ** attempt
                        logger.warning(f"Gemini API high demand (attempt {attempt+1}/{max_retries}). Retrying in {backoff}s... Error: {e}")
                        await asyncio.sleep(backoff)
                        continue
                    raise  # Re-raise if out of retries or it's a different error

        except Exception as e:
            logger.error(f"Gemini plan generation failed: {e}")
            raise
