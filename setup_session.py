import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

# Dedicated Chrome profile for Project Sane
# Separate from Sam's main Chrome — no conflicts
DEDICATED_PROFILE = str(
    Path.home() / "Library" / "Application Support" / "Google" / "ChromeProjectSane"
)

async def setup():
    print("=" * 60)
    print("PROJECT SANE — ONE-TIME SESSION SETUP")
    print("=" * 60)
    print()
    print("A dedicated Chrome profile will be created for this tool.")
    print("Please log in with your Odoo employee Google account.")
    print("You have 120 seconds to complete login.")
    print()
    print(f"Profile location: {DEDICATED_PROFILE}")
    print()
    print("Starting Chrome...")

    from chrome_launcher import launch_native_chrome
    launch_native_chrome(9222)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0] if browser.contexts else browser

        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto("https://www.odoo.com/web/login")
        print("Chrome opened. Please log in now...")
        print("Waiting up to 120 seconds...")

        try:
            await page.wait_for_url(
                lambda url: "login" not in url and "odoo.com" in url,
                timeout=120_000,
            )
            print()
            print("Login detected! Saving session...")
            await asyncio.sleep(3)
        except Exception:
            print()
            print("WARNING: Login not detected within 120 seconds.")
            print("If you completed login, the session may still be saved.")
            print("Try running the agent anyway.")

        await browser.close()

    print()
    print("Session saved to:", DEDICATED_PROFILE)
    print("You do NOT need to run this script again unless your session expires.")
    print("If it ever expires, simply run this script again and log in.")

if __name__ == "__main__":
    asyncio.run(setup())
