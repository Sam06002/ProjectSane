import argparse
import asyncio
import os
from dotenv import load_dotenv

from ai_agent import AIAgent
from browser_agent import BrowserAgent
from doc_writer import generate_report

load_dotenv()

def parse_args():
    parser = argparse.ArgumentParser(description="Odoo Support Ticket Agent")
    parser.add_argument("--ticket", type=str, help="Ticket text as a string")
    parser.add_argument("--file", type=str, help="Path to a .txt file containing the ticket text")
    parser.add_argument("--db-url", type=str, required=True, help="Customer duplicate database URL (required)")
    parser.add_argument("--sh-url", type=str, help="Odoo.sh staging URL (optional)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode (flag, default: False)")
    return parser.parse_args()

def get_ticket_text(args) -> str:
    if args.file:
        with open(args.file, 'r', encoding='utf-8') as f:
            return f.read()
    if args.ticket:
        return args.ticket
        
    print("Paste the ticket text below. Press Enter twice when done:")
    lines = []
    while True:
        try:
            line = input()
            # Stop if two consecutive empty lines
            if not line.strip() and (len(lines) > 0 and not lines[-1].strip()):
                break
            lines.append(line)
        except EOFError:
            break
    return "\n".join(lines).strip()

async def run(args):
    groq_api_key = os.getenv("GROQ_API_KEY")
    gemini_api_key = os.getenv("GEMINI_API_KEY")

    if not groq_api_key:
        print("Error: GROQ_API_KEY environment variable not set.")
        return
    if not gemini_api_key:
        print("Error: GEMINI_API_KEY environment variable not set.")
        return

    os.makedirs("output", exist_ok=True)

    ticket_text = get_ticket_text(args)

    print("\n[1/5] Analysing ticket with Groq (fast extraction)...")
    ai_agent = AIAgent(groq_api_key=groq_api_key, gemini_api_key=gemini_api_key)
    ticket_info = ai_agent.analyse_ticket(ticket_text)
    
    print(f"  Summary: {ticket_info.get('summary')}")
    
    print("\n[2/5] Starting browser investigation...")
    browser_agent = BrowserAgent(headless=args.headless)
    await browser_agent.start()
    db_findings = await browser_agent.investigate_duplicate_db(args.db_url, ticket_info)
    
    runbot_findings = ""
    if ticket_info.get("check_runbot") is True and ticket_info.get("odoo_version") is not None:
        print("\n[3/5] Testing on Runbot...")
        runbot_findings = await browser_agent.test_on_runbot(ticket_info.get("odoo_version"))
        
    await browser_agent.stop()
    
    print("\n[4/5] Synthesising resolution...")
    all_findings = db_findings + "\n\n" + runbot_findings
    resolution = ai_agent.synthesise_resolution(ticket_text, all_findings)
    
    print("\n[5/5] Generating Word report...")
    report_path = generate_report(
        ticket_text=ticket_text,
        ticket_info=ticket_info,
        db_findings=db_findings,
        runbot_findings=runbot_findings,
        resolution=resolution,
        screenshots=browser_agent.screenshots
    )
    
    print(f"\nDone! Report saved to: {report_path}")

if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run(args))
