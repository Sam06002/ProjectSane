from playwright.sync_api import sync_playwright
import os


def main():
    # Define a path for the persistent profile data
    # This directory will be created if it doesn't exist
    user_data_dir = os.path.join(os.getcwd(), 'playwright_profile')

    with sync_playwright() as p:
        print(f"Launching persistent context using directory: {user_data_dir}")

        # Launch persistent context instead of a standard browser
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,  # Set to True to run in headless mode
            # You can add other browser args or options here if needed
        )

        # A persistent context often starts with a default page already open.
        # We can try to use that first page, or create a new one.
        if context.pages:
            page = context.pages[0]
        else:
            page = context.new_page()

        # Navigate to a website
        page.goto("https://example.com")

        print(f"Successfully navigated to: {page.url}")
        print(f"Page Title: {page.title()}")

        # Keep it open for a few seconds to see it working
        page.wait_for_timeout(3000)

        # Close the context
        context.close()


if __name__ == "__main__":
    main()
