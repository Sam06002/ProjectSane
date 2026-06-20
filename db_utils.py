import os
import logging
from typing import Any, Optional
from urllib.parse import urlparse
from playwright.async_api import Page
from exceptions import DuplicationError

logger = logging.getLogger(__name__)

def get_subdomain(url: str) -> str:
    """Extracts the subdomain/first segment of the hostname from a URL."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or parsed.path or ""
        # Handle case where scheme might be missing, e.g. "sane1.odoo.com"
        if not hostname and "." in url:
            hostname = url.split("/")[0]
        return hostname.split(".")[0]
    except Exception:
        return ""

def get_database_name(url: str) -> str:
    """
    Extracts the Odoo database name from a URL.
    Checks the 'db' query parameter first, falling back to the subdomain of the hostname.
    """
    try:
        parsed = urlparse(url)
        from urllib.parse import parse_qs
        qs = parse_qs(parsed.query)
        if "db" in qs and qs["db"]:
            return qs["db"][0]
            
        hostname = parsed.hostname or parsed.path or ""
        if not hostname and "." in url:
            hostname = url.split("/")[0]
        return hostname.split(".")[0]
    except Exception:
        return ""

def is_duplicate_database(url_or_text: str, prod_url: Optional[str] = None) -> bool:
    """
    Checks if a URL or text represents a duplicate database.
    Looks for standard Odoo support/staging/duplicate subdomain patterns.
    """
    if not url_or_text:
        return False
        
    url_lower = url_or_text.lower()
    
    # 1. Check for standard duplicate keywords in URL/text
    indicators = ["-support-", "-neutralized-", "-copy-", "-staging-", "support-", "neutralized", "copy"]
    has_indicators = any(ind in url_lower for ind in indicators)
    
    # 2. If prod_url is supplied and is not itself a duplicate, perform a more strict check
    if prod_url and not is_duplicate_database(prod_url):
        prod_db = get_database_name(prod_url).lower()
        current_db = get_database_name(url_or_text).lower()
        if current_db == prod_db:
            return False
        is_db_match = current_db.startswith(f"{prod_db}-")
        return is_db_match or (prod_db in url_lower and has_indicators)
        
    return has_indicators

async def assert_duplicate_database(
    current_url: str,
    prod_url: str,
    page: Optional[Page] = None,
    run_logger: Optional[Any] = None
) -> None:
    """
    Asserts that the current database URL is a valid duplicate/staging database.
    If the original prod_url was classified as production, and current_url
    is not a valid duplicate, raises DuplicationError.
    
    Logs identifiers and captures duplicate confirmation screenshot when successful.
    """
    # Verify if original URL is actually a production database
    is_prod_original = not is_duplicate_database(prod_url)
    if not is_prod_original:
        # Staging/local/duplicate URL directly provided as source - assertion is a no-op
        return

    prod_id = get_database_name(prod_url)
    curr_id = get_database_name(current_url)

    is_dup_url = is_duplicate_database(current_url, prod_url)
    has_neutral_banner = False
    if page:
        try:
            import odoo_selectors as selectors
            for sel in selectors.get_selector("neutral_banner"):
                if await page.locator(sel).count() > 0:
                    has_neutral_banner = True
                    break
        except Exception as e:
            logger.warning(f"Error checking neutralization banner: {e}")

    if not is_dup_url and not has_neutral_banner:
        log_msg = (
            f"FATAL: Execution blocked! Attempted to run against production database.\n"
            f" - Target URL: {current_url}\n"
            f" - Production URL: {prod_url}"
        )
        logger.error(log_msg)
        if run_logger:
            if hasattr(run_logger, "log_error"):
                run_logger.log_error(message=log_msg, context="production_safety_check")
            elif hasattr(run_logger, "error"):
                run_logger.error(log_msg)
        raise DuplicationError(log_msg, "production_execution_blocked")

    # Log confirmation details
    log_msg = (
        f"DUPLICATE CONFIRMED:\n"
        f" - Production DB Identifier: {prod_id}\n"
        f" - Duplicate DB Identifier: {curr_id}\n"
        f" - Duplicate URL: {current_url}"
    )
    logger.info(log_msg)
    if run_logger and hasattr(run_logger, "info"):
        run_logger.info(log_msg)

    # Capture duplicate confirmation screenshot
    if page:
        try:
            screenshot_path = "logs/screenshots/duplicate_confirmed.png"
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
            await page.screenshot(path=screenshot_path)
            logger.info(f"Duplicate confirmation screenshot saved: {screenshot_path}")
            if run_logger and hasattr(run_logger, "screenshots"):
                run_logger.screenshots.append(screenshot_path)
        except Exception as e:
            logger.warning(f"Failed to capture duplicate confirmation screenshot: {e}")
