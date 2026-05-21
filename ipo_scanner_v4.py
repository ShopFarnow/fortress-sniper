#!/usr/bin/env python3
"""
IPO Data Fetcher — Multi-Source with Anti-Bot Bypass
=====================================================
Sources:
  A) Chittorgarh  — HTTP (most reliable, no JS needed)
  B) Investorgain — cloudscraper bypass
  C) NSE India    — 2-step cookie warmup + API intercept
  D) Screener.in  — Playwright with domcontentloaded (NOT networkidle)
  E) Groww        — Playwright API intercept

Key fixes vs previous attempts:
  - NEVER use waitUntil="networkidle" on these sites → always timeout
  - NSE needs a real browser warmup (Referer + cookie chain) before API call
  - cloudscraper handles Cloudflare JS challenges for investorgain/chittorgarh
  - Playwright intercepts XHR/fetch instead of parsing rendered DOM where possible

Requirements:
    pip install requests beautifulsoup4 lxml cloudscraper playwright
    playwright install chromium
"""

import json
import re
import sys
import time
import logging
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Shared browser headers ──────────────────────────────────────────────────
CHROME_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

JSON_HEADERS = {
    **CHROME_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE A — Chittorgarh (most reliable, pure HTTP)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_chittorgarh() -> list[dict]:
    """
    Chittorgarh serves full HTML without JS rendering.
    Covers open + upcoming IPOs in one page.
    """
    log.info("━━ SOURCE A: Chittorgarh (HTTP) ━━")
    results = []
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        r = scraper.get(
            "https://www.chittorgarh.com/ipo/ipo_dashboard.asp",
            headers=CHROME_HEADERS,
            timeout=20,
        )
        log.info(f"  Chittorgarh → {r.status_code}")
        if r.status_code != 200:
            log.warning("  ⚠️  Chittorgarh: non-200 response")
            return results

        soup = BeautifulSoup(r.text, "lxml")

        # Find all IPO tables (Open, Upcoming, Recently Closed)
        for section in soup.find_all("div", class_=re.compile(r"ipo.*table|table.*ipo", re.I)):
            heading = section.find_previous(["h2", "h3", "h4"])
            section_name = heading.get_text(strip=True) if heading else "Unknown"

            for row in section.find_all("tr")[1:]:  # skip header
                cols = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cols) >= 4:
                    results.append({
                        "source": "Chittorgarh",
                        "section": section_name,
                        "name": cols[0],
                        "open_date": cols[1] if len(cols) > 1 else "",
                        "close_date": cols[2] if len(cols) > 2 else "",
                        "issue_price": cols[3] if len(cols) > 3 else "",
                        "lot_size": cols[4] if len(cols) > 4 else "",
                        "issue_size": cols[5] if len(cols) > 5 else "",
                    })

        # Fallback: parse any table with IPO-like columns
        if not results:
            results = _parse_generic_ipo_tables(soup, source="Chittorgarh")

        log.info(f"  ✓ Chittorgarh: {len(results)} IPOs")
    except Exception as e:
        log.warning(f"  ⚠️  Chittorgarh error: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE B — Investorgain GMP (cloudscraper bypass)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_investorgain() -> list[dict]:
    """
    Investorgain uses Cloudflare — cloudscraper handles the JS challenge.
    Do NOT use Playwright networkidle here (it always times out).
    """
    log.info("━━ SOURCE B: Investorgain GMP (cloudscraper) ━━")
    results = []
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            delay=5,
        )
        r = scraper.get(
            "https://investorgain.com/report/live-ipo-gmp/331/",
            headers=CHROME_HEADERS,
            timeout=30,
        )
        log.info(f"  Investorgain → {r.status_code}")
        if r.status_code != 200:
            log.warning(f"  ⚠️  Investorgain: {r.status_code}")
            return results

        soup = BeautifulSoup(r.text, "lxml")
        table = soup.find("table", id=re.compile(r"ipo", re.I)) or soup.find("table")
        if not table:
            log.warning("  ⚠️  Investorgain: no table found")
            return results

        headers_row = [th.get_text(strip=True) for th in table.find_all("th")]
        for row in table.find_all("tr")[1:]:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if cols and len(cols) >= 3:
                entry = {
                    "source": "Investorgain",
                    "name": cols[0] if cols else "",
                    "gmp": "",
                    "expected_price": "",
                    "open_date": "",
                    "close_date": "",
                    "status": "",
                }
                # Map columns dynamically
                for i, h in enumerate(headers_row):
                    if i < len(cols):
                        hl = h.lower()
                        if "gmp" in hl:
                            entry["gmp"] = cols[i]
                        elif "price" in hl and "expected" in hl:
                            entry["expected_price"] = cols[i]
                        elif "open" in hl:
                            entry["open_date"] = cols[i]
                        elif "close" in hl:
                            entry["close_date"] = cols[i]
                        elif "status" in hl:
                            entry["status"] = cols[i]
                results.append(entry)

        log.info(f"  ✓ Investorgain: {len(results)} IPOs")
    except Exception as e:
        log.warning(f"  ⚠️  Investorgain error: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE C — NSE India (cookie warmup → JSON API)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_nse() -> list[dict]:
    """
    NSE India fix:
    1) Hit the main page to get cookies (bm_sz, nsit, nseappid, etc.)
    2) Hit a secondary page to refresh the token
    3) Call the API with the exact Referer NSE expects
    4) If API 403/404, fall back to Playwright with domcontentloaded (NOT networkidle)
    """
    log.info("━━ SOURCE C: NSE India (cookie warmup + API) ━━")
    results = []
    session = requests.Session()

    warmup_headers = {
        **CHROME_HEADERS,
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

    try:
        # Step 1: warm up main page
        log.info("  NSE step 1: warming up main page…")
        r0 = session.get("https://www.nseindia.com", headers=warmup_headers, timeout=15)
        log.info(f"  NSE main page: {r0.status_code}, cookies: {list(session.cookies.keys())}")
        time.sleep(1.5)

        # Step 2: visit the IPO page (builds the right Referer + more cookies)
        log.info("  NSE step 2: visiting IPO page…")
        ipo_page_headers = {
            **warmup_headers,
            "Referer": "https://www.nseindia.com",
            "Sec-Fetch-Site": "same-origin",
        }
        r1 = session.get(
            "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
            headers=ipo_page_headers,
            timeout=15,
        )
        log.info(f"  NSE IPO page: {r1.status_code}")
        time.sleep(1.5)

        # Step 3: call the API with the IPO page as Referer
        api_headers = {
            **JSON_HEADERS,
            "Referer": "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        endpoints = [
            "https://www.nseindia.com/api/ipo-current-allotment",
            "https://www.nseindia.com/api/getIpoData?category=ipo",
            "https://www.nseindia.com/api/ipo-detail?symbol=IPO",
        ]

        for ep in endpoints:
            try:
                r2 = session.get(ep, headers=api_headers, timeout=12)
                log.info(f"  NSE API [{ep.split('/')[-1]}] → {r2.status_code}")
                if r2.status_code == 200 and r2.text.strip():
                    data = r2.json()
                    results = _parse_nse_json(data)
                    if results:
                        log.info(f"  ✓ NSE: {len(results)} IPOs from {ep.split('/')[-1]}")
                        return results
            except Exception as ep_e:
                log.debug(f"  NSE endpoint {ep}: {ep_e}")

        log.warning("  NSE HTTP APIs exhausted, trying Playwright…")
        results = _fetch_nse_playwright(session.cookies)

    except Exception as e:
        log.warning(f"  ⚠️  NSE error: {e}")
    return results


def _parse_nse_json(data: dict | list) -> list[dict]:
    """Parse various NSE JSON response shapes."""
    results = []
    # Shape 1: list of dicts
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                results.append({
                    "source": "NSE",
                    "name": item.get("companyName", item.get("symbol", "")),
                    "open_date": item.get("openDate", item.get("bidStartDate", "")),
                    "close_date": item.get("closeDate", item.get("bidEndDate", "")),
                    "issue_price": item.get("issuePrice", item.get("price", "")),
                    "status": item.get("status", ""),
                    "listing_date": item.get("listingDate", ""),
                })
    # Shape 2: dict with nested key
    elif isinstance(data, dict):
        for key in ["data", "ipoData", "upcomingIPO", "currentIPO", "allIpo"]:
            if key in data:
                return _parse_nse_json(data[key])
    return results


def _fetch_nse_playwright(existing_cookies=None) -> list[dict]:
    """
    Playwright fallback for NSE.
    FIX: use domcontentloaded, NOT networkidle (networkidle always times out on NSE).
    Intercept XHR responses instead of parsing DOM.
    """
    results = []
    try:
        from playwright.sync_api import sync_playwright

        captured = []

        def handle_response(response):
            url = response.url
            if "nseindia.com/api" in url and response.status == 200:
                try:
                    body = response.json()
                    captured.append((url, body))
                    log.info(f"  NSE PW intercepted: {url.split('/')[-1]}")
                except Exception:
                    pass

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = browser.new_context(
                user_agent=CHROME_HEADERS["User-Agent"],
                locale="en-IN",
                viewport={"width": 1366, "height": 768},
                extra_http_headers={
                    "Accept-Language": "en-IN,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                },
            )
            # Inject existing cookies if available
            if existing_cookies:
                cookie_list = [
                    {"name": c.name, "value": c.value, "domain": ".nseindia.com", "path": "/"}
                    for c in existing_cookies
                ]
                if cookie_list:
                    ctx.add_cookies(cookie_list)

            page = ctx.new_page()
            page.on("response", handle_response)

            # KEY FIX: domcontentloaded, not networkidle
            try:
                page.goto(
                    "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
                    wait_until="domcontentloaded",  # ← FIXED
                    timeout=30_000,
                )
                # Wait a bit for XHR calls to fire
                page.wait_for_timeout(5000)
            except Exception as nav_e:
                log.warning(f"  NSE PW nav (non-fatal): {nav_e}")

            browser.close()

        for url, body in captured:
            parsed = _parse_nse_json(body)
            if parsed:
                results.extend(parsed)

    except ImportError:
        log.warning("  Playwright not installed. Run: pip install playwright && playwright install chromium")
    except Exception as e:
        log.warning(f"  NSE Playwright error: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE D — Screener.in (Playwright, domcontentloaded, wait for selector)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_screener() -> list[dict]:
    """
    Screener.in fix:
    - Use domcontentloaded (NOT networkidle — always timeouts)
    - Wait for the actual table selector to appear
    - Parse the Django-rendered HTML table (no JS needed once DOM loads)
    """
    log.info("━━ SOURCE D: Screener.in (Playwright fixed) ━━")
    results = []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = browser.new_context(
                user_agent=CHROME_HEADERS["User-Agent"],
                locale="en-IN",
                viewport={"width": 1366, "height": 768},
            )
            page = ctx.new_page()

            # Mask automation
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = { runtime: {} };
            """)

            try:
                page.goto(
                    "https://www.screener.in/ipo/recent/",
                    wait_until="domcontentloaded",  # ← FIXED (was networkidle)
                    timeout=25_000,
                )
                # Wait for the IPO table — Screener renders it server-side
                page.wait_for_selector("table", timeout=10_000)
                log.info("  Screener: table found in DOM")
            except Exception as nav_e:
                log.warning(f"  Screener nav (continuing anyway): {nav_e}")

            html = page.content()
            browser.close()

        soup = BeautifulSoup(html, "lxml")
        results = _parse_generic_ipo_tables(soup, source="Screener")
        log.info(f"  ✓ Screener: {len(results)} IPOs")

    except ImportError:
        log.warning("  Playwright not installed. Run: pip install playwright && playwright install chromium")
    except Exception as e:
        log.warning(f"  ⚠️  Screener error: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE E — Groww (Playwright, intercept internal API)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_groww() -> list[dict]:
    """
    Groww is a React SPA — DOM parsing won't work.
    Strategy: intercept the internal API calls Groww makes on page load.
    Look for /api/ipo or /api/v1/ipo in network requests.
    """
    log.info("━━ SOURCE E: Groww (Playwright API intercept) ━━")
    results = []
    try:
        from playwright.sync_api import sync_playwright

        captured = []

        def handle_response(response):
            url = response.url
            # Groww internal API patterns
            if any(kw in url for kw in ["/ipo", "IPO", "allotment"]) and response.status == 200:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    try:
                        body = response.json()
                        captured.append((url, body))
                        log.info(f"  Groww intercepted: {url}")
                    except Exception:
                        pass

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = browser.new_context(
                user_agent=CHROME_HEADERS["User-Agent"],
                locale="en-IN",
                viewport={"width": 1366, "height": 768},
            )
            page = ctx.new_page()
            page.on("response", handle_response)

            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)

            try:
                # Load the Groww IPO page
                page.goto(
                    "https://groww.in/ipo",
                    wait_until="domcontentloaded",  # NOT networkidle
                    timeout=30_000,
                )
                # Give React time to fetch data
                page.wait_for_timeout(6000)
            except Exception as nav_e:
                log.warning(f"  Groww nav (non-fatal): {nav_e}")

            # Also try to scrape what rendered into the DOM as a fallback
            html = page.content()
            browser.close()

        # Parse intercepted JSON
        for url, body in captured:
            parsed = _parse_groww_json(body)
            results.extend(parsed)
            log.info(f"  Groww API hit: {len(parsed)} items from {url}")

        # DOM fallback if no API intercepted
        if not results:
            log.info("  Groww: no API intercepted, trying DOM parse…")
            soup = BeautifulSoup(html, "lxml")
            results = _parse_generic_ipo_tables(soup, source="Groww")

        log.info(f"  ✓ Groww: {len(results)} IPOs")

    except ImportError:
        log.warning("  Playwright not installed. Run: pip install playwright && playwright install chromium")
    except Exception as e:
        log.warning(f"  ⚠️  Groww error: {e}")
    return results


def _parse_groww_json(data) -> list[dict]:
    """Parse Groww's internal API response (shape varies by endpoint)."""
    results = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and ("ipoName" in item or "companyName" in item or "name" in item):
                results.append({
                    "source": "Groww",
                    "name": item.get("ipoName", item.get("companyName", item.get("name", ""))),
                    "open_date": item.get("openDate", item.get("startDate", "")),
                    "close_date": item.get("closeDate", item.get("endDate", "")),
                    "issue_price": item.get("issuePrice", item.get("price", "")),
                    "lot_size": item.get("lotSize", item.get("minOrderQty", "")),
                    "status": item.get("status", item.get("ipoStatus", "")),
                    "listing_date": item.get("listingDate", ""),
                    "gmp": item.get("gmp", item.get("greyMarketPremium", "")),
                })
    elif isinstance(data, dict):
        for key in ["data", "ipos", "ipoList", "upcoming", "open", "result"]:
            if key in data:
                results.extend(_parse_groww_json(data[key]))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE F — IndiaTrade IPO (HTTP + cloudscraper)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_indiatrade() -> list[dict]:
    """
    IndiaTrade IPO page — try HTTP with cloudscraper first,
    fall back to Playwright with domcontentloaded.
    """
    log.info("━━ SOURCE F: IndiaTrade (HTTP + PW fallback) ━━")
    results = []
    url = "https://ipo.indiratrade.com/Home"
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        r = scraper.get(url, headers=CHROME_HEADERS, timeout=20)
        log.info(f"  IndiaTrade HTTP → {r.status_code}")

        if r.status_code == 200 and len(r.text) > 2000:
            soup = BeautifulSoup(r.text, "lxml")
            results = _parse_generic_ipo_tables(soup, source="IndiaTrade")

        if not results:
            log.info("  IndiaTrade: falling back to Playwright…")
            results = _fetch_generic_playwright(url, source="IndiaTrade", wait_ms=5000)

        log.info(f"  ✓ IndiaTrade: {len(results)} IPOs")
    except Exception as e:
        log.warning(f"  ⚠️  IndiaTrade error: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Generic helpers
# ══════════════════════════════════════════════════════════════════════════════
def _parse_generic_ipo_tables(soup: BeautifulSoup, source: str) -> list[dict]:
    """
    Generic table parser — finds any HTML table that looks IPO-related
    and maps columns heuristically.
    """
    results = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        # Must look like an IPO table
        if not any(kw in " ".join(headers) for kw in ["ipo", "company", "open", "price", "lot"]):
            continue
        for row in table.find_all("tr")[1:]:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cols or not cols[0]:
                continue
            entry = {"source": source, "name": cols[0]}
            for i, h in enumerate(headers):
                if i >= len(cols):
                    break
                if "open" in h and "date" in h:
                    entry["open_date"] = cols[i]
                elif "close" in h and "date" in h:
                    entry["close_date"] = cols[i]
                elif "price" in h and "issue" in h:
                    entry["issue_price"] = cols[i]
                elif "lot" in h:
                    entry["lot_size"] = cols[i]
                elif "size" in h or "amount" in h:
                    entry["issue_size"] = cols[i]
                elif "status" in h:
                    entry["status"] = cols[i]
                elif "gmp" in h:
                    entry["gmp"] = cols[i]
                elif "list" in h and "date" in h:
                    entry["listing_date"] = cols[i]
            results.append(entry)
    return results


def _fetch_generic_playwright(url: str, source: str, wait_ms: int = 5000) -> list[dict]:
    """Generic Playwright fetcher — domcontentloaded + wait_for_selector table."""
    results = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent=CHROME_HEADERS["User-Agent"],
                locale="en-IN",
                viewport={"width": 1366, "height": 768},
            )
            page = ctx.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(wait_ms)
            except Exception:
                pass
            html = page.content()
            browser.close()
        soup = BeautifulSoup(html, "lxml")
        results = _parse_generic_ipo_tables(soup, source=source)
    except Exception as e:
        log.warning(f"  Generic PW [{source}] error: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Dedup + output
# ══════════════════════════════════════════════════════════════════════════════
def deduplicate(all_results: list[dict]) -> list[dict]:
    """
    Merge records across sources by company name (fuzzy).
    Priority: keep the entry with the most fields filled.
    """
    seen: dict[str, dict] = {}
    for item in all_results:
        name = re.sub(r"\s+", " ", (item.get("name") or "")).strip().lower()
        name = re.sub(r"\b(ipo|limited|ltd|pvt|private|public|co\.?)\b", "", name).strip()
        if not name:
            continue
        if name not in seen:
            seen[name] = item
        else:
            # Keep whichever has more non-empty fields
            existing = seen[name]
            if sum(bool(v) for v in item.values()) > sum(bool(v) for v in existing.values()):
                # Merge: fill blanks from the existing record
                merged = {**existing, **{k: v for k, v in item.items() if v}}
                merged["source"] = f"{existing.get('source','')}, {item.get('source','')}"
                seen[name] = merged
    return list(seen.values())


def print_results(results: list[dict]):
    if not results:
        print("\n⚠️  No IPO data collected.\n")
        return

    # Group by section/status
    groups: dict[str, list] = {}
    for r in results:
        key = r.get("section", r.get("status", "IPO Data"))
        groups.setdefault(key, []).append(r)

    print(f"\n{'═'*70}")
    print(f"  IPO DATA  —  fetched {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"  Total: {len(results)} IPOs from {len(set(r['source'] for r in results))} sources")
    print(f"{'═'*70}\n")

    for group, items in groups.items():
        print(f"  ◆ {group.upper()}  ({len(items)} IPOs)")
        print(f"  {'─'*66}")
        for ipo in items:
            name = ipo.get("name", "N/A")
            dates = ""
            if ipo.get("open_date") or ipo.get("close_date"):
                dates = f"  {ipo.get('open_date','')} → {ipo.get('close_date','')}"
            price = f"  ₹{ipo.get('issue_price','')}" if ipo.get("issue_price") else ""
            lot = f"  Lot:{ipo.get('lot_size','')}" if ipo.get("lot_size") else ""
            gmp = f"  GMP:{ipo.get('gmp','')}" if ipo.get("gmp") else ""
            listing = f"  Listing:{ipo.get('listing_date','')}" if ipo.get("listing_date") else ""
            src = f"  [{ipo.get('source','')}]"
            print(f"  • {name}")
            if any([dates, price, lot, gmp, listing]):
                print(f"    {dates}{price}{lot}{gmp}{listing}")
            print(f"    {src}")
        print()


def save_json(results: list[dict], path: str = "ipo_data.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"  💾 Saved → {path}")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════
def main():
    all_results = []

    # HTTP-only sources (no Playwright needed, faster)
    all_results += fetch_chittorgarh()
    all_results += fetch_investorgain()

    # NSE (cookie warmup + API, with PW fallback)
    all_results += fetch_nse()

    # Playwright sources (slower, run last)
    all_results += fetch_screener()
    all_results += fetch_groww()
    all_results += fetch_indiatrade()

    # Merge duplicates across sources
    merged = deduplicate(all_results)

    print_results(merged)
    save_json(merged)

    return merged


if __name__ == "__main__":
    main()
