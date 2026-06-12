import asyncio
import requests
import time
from playwright.async_api import async_playwright

async def main():
    res = requests.get("http://localhost:9225/json/list")
    pages = res.json()
    page_ws_url = next(p["webSocketDebuggerUrl"] for p in pages if "webSocketDebuggerUrl" in p and p.get("type") == "page")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(page_ws_url)
        context = browser.contexts[0]
        
        for _ in range(20):
            if context.pages:
                break
            await asyncio.sleep(0.1)
            
        print(f"Pages after wait: {context.pages}")

asyncio.run(main())
