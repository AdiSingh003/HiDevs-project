"""Browser end-to-end check of the arena UI (Playwright + Chromium).

Drives every page against a running server and saves screenshots:
  1. Arena: red-team negotiation -> blocks/redactions -> signed contract (God view, then Supplier view)
  2. Contracts: overview, signatures verify, forged copy is detected
  3. Telemetry: signed cyclone event -> live renegotiation -> amendment v2
  4. Multi-vendor RFQ: three lanes -> Pareto award -> winner contract
  5. Audit: chain verifies, simulated tampering is detected
  6. Arena: hardball deadlock -> Nash mediation -> CFO co-signature

Usage:
  python -m uvicorn backend.app.main:app --port 8000        # with the UI built into frontend/dist
  python scripts/e2e_ui.py --base http://127.0.0.1:8000 --out e2e-screenshots
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import Page, async_playwright, expect

SET_RANGE = """(el, v) => {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  setter.call(el, String(v));
  el.dispatchEvent(new Event('input', { bubbles: true }));
}"""


async def shot(page: Page, out: Path, name: str) -> None:
    await page.evaluate("window.scrollTo(0, 0)")  # sticky header renders mid-page in full-page captures otherwise
    await page.wait_for_timeout(400)
    await page.screenshot(path=str(out / f"{name}.png"), full_page=True)
    print(f"  screenshot {name}.png")


async def run(base: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1680, "height": 1050})
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("console", lambda m: problems.append(f"console: {m.text}") if m.type == "error" else None)
        page.set_default_timeout(90_000)

        print("1. Arena - red-team negotiation")
        await page.goto(f"{base}/#/arena")
        await page.get_by_text("Automotive MCU spot PO").first.click()
        await page.get_by_text("Red-team mode").click()
        await page.locator("input[type=range]").first.evaluate(SET_RANGE, 60)
        await page.get_by_role("button", name="Start negotiation").click()
        await expect(page.get_by_text("Contract compiled & signed")).to_be_visible()
        await expect(page.get_by_text("Legal Arbiter blocked the buyer's counter").first).to_be_visible()
        await expect(page.get_by_text("Pareto gap")).to_be_visible()
        await expect(page.get_by_text("Concession analytics")).to_be_visible()
        await expect(page.get_by_text("Delivered in bounds")).to_be_visible()
        await expect(page.get_by_text(re.compile(r"accuracy \d+%"))).to_be_visible()
        await shot(page, out, "01-arena-god-view")
        await page.get_by_role("button", name="Replay").click()
        await expect(page.get_by_text(re.compile(r"event \d+/\d+"))).to_be_visible()
        await page.get_by_role("button", name="Pause").click()
        await shot(page, out, "01b-arena-replay")
        await page.locator("input[type=range]").nth(1).evaluate(SET_RANGE, 10_000)  # jump back to the end

        await page.get_by_role("button", name="Supplier", exact=True).click()
        await expect(page.get_by_text("Private mandate breach").first).to_be_visible()
        await expect(page.get_by_text("only visible in the")).to_be_visible()
        await shot(page, out, "02-arena-supplier-view")
        await page.get_by_role("button", name="God", exact=True).click()

        print("2. Contracts")
        await page.get_by_role("button", name="Open contract").click()
        await expect(page.get_by_text("Contract register")).to_be_visible()
        await expect(page.get_by_text("Drafting (Lyzr Automata)")).to_be_visible()
        await shot(page, out, "03-contract-overview")
        await page.get_by_role("button", name="Integrity & signatures").click()
        await expect(page.get_by_text("Authentic: content hash and every Ed25519 signature verify")).to_be_visible()
        await page.locator(".note input[type=number]").fill("1")
        await page.get_by_role("button", name="Forge & verify").click()
        await expect(page.get_by_text("Forgery detected")).to_be_visible()
        await shot(page, out, "04-contract-integrity")
        await page.get_by_role("button", name="Executable SLA").click()
        await expect(page.get_by_text("LD-DELAY").first).to_be_visible()
        await shot(page, out, "05-contract-sla")

        print("3. Telemetry -> renegotiation -> amendment")
        await page.get_by_role("button", name="Overview").click()
        await page.get_by_role("button", name="Send telemetry event").click()
        await expect(page.get_by_text("Telemetry webhook")).to_be_visible()
        await page.get_by_role("button", name="Send signed event").click()
        await expect(page.get_by_text("Legal Arbiter assessment")).to_be_visible()
        await expect(page.get_by_text("Amendment v2 compiled & signed")).to_be_visible()
        await shot(page, out, "06-telemetry-amendment")

        print("4. Multi-vendor RFQ")
        await page.goto(f"{base}/#/rfq")
        await page.locator("input[type=range]").first.evaluate(SET_RANGE, 50)
        await page.get_by_role("button", name="Run sourcing event").click()
        await expect(page.get_by_text("Award decision")).to_be_visible()
        await expect(page.get_by_text("Winner contract")).to_be_visible()
        await shot(page, out, "07-rfq-award")

        print("5. Audit")
        await page.goto(f"{base}/#/audit")
        await expect(page.get_by_text("Chain intact").first).to_be_visible()
        await page.get_by_role("button", name="Tamper & verify").click()
        await expect(page.get_by_text("Detected: entry #5")).to_be_visible()
        await page.get_by_role("button", name="Reconcile with Lyzr AIMS").click()
        await expect(page.get_by_text(re.compile("AIMS anchoring is off|matches all"))).to_be_visible()
        await shot(page, out, "08-audit-ledger")

        print("6. Arena - deadlock -> mediation -> CFO approval")
        await page.goto(f"{base}/#/arena")
        await page.get_by_text("Lithium cells hardball").first.click()
        await page.locator("input[type=range]").first.evaluate(SET_RANGE, 50)
        await page.get_by_role("button", name="Start negotiation").click()
        await expect(page.get_by_text("Deadlock detected")).to_be_visible()
        await expect(page.get_by_text("Contract compiled & signed")).to_be_visible()
        await shot(page, out, "09-arena-mediation")
        await page.get_by_role("button", name="Open contract").click()
        await expect(page.get_by_text("CFO co-signature required", exact=True)).to_be_visible()
        await page.get_by_placeholder("Approver name").fill("Priya Nair, CFO")
        await page.get_by_role("button", name="Approve & co-sign").click()
        await expect(page.locator(".badge", has_text="executed").first).to_be_visible()
        await shot(page, out, "10-contract-cfo-approved")

        await browser.close()
    benign = [p for p in problems if "fonts.g" not in p and "ERR_NAME_NOT_RESOLVED" not in p]
    if benign:
        print("\nBrowser errors:\n  " + "\n  ".join(benign))
        return 1
    print("\nE2E OK - no browser errors")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--out", default="e2e-screenshots")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.base, Path(args.out))))
