#!/usr/bin/env python3
"""
IPO Data Fetcher — Fixed for NSE (HTTP/2 bypass), Groww API intercept, dedup.
"""

import json
import re
import time
import logging
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CHROME_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

JSON_HEADERS = {**CHROME_HEADERS, "Accept": "application/json, text/plain, */*", "X-Requested-With": "XMLHttpRequest"}


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE A — Chittorgarh (HTTP)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_chittorgarh() -> list[dict]:
    log.info("━━ SOURCE A: Chittorgarh ━━")
    results = []
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
        r = scraper.get("https://www.chittorgarh.com/ipo/ipo_dashboard.asp", headers=CHROME_HEADERS, timeout=20)
        if r.status_code != 200:
            return results
        soup = BeautifulSoup(r.text, "lxml")
        # Direct table parsing
        for table in soup.find_all("table", class_="table"):
            for row in table.find_all("tr")[1:]:
                cols = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cols) >= 4:
                    results.append({
                        "source": "Chittorgarh",
                        "name": cols[0],
                        "open_date": cols[1] if len(cols) > 1 else "",
                        "close_date": cols[2] if len(cols) > 2 else "",
                        "issue_price": cols[3] if len(cols) > 3 else "",
                        "lot_size": cols[4] if len(cols) > 4 else "",
                        "issue_size": cols[5] if len(cols) > 5 else "",
                    })
        log.info(f"  ✓ Chittorgarh: {len(results)} IPOs")
    except Exception as e:
        log.warning(f"  Chittorgarh error: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE B — Investorgain
# ══════════════════════════════════════════════════════════════════════════════
def fetch_investorgain() -> list[dict]:
    log.info("━━ SOURCE B: Investorgain ━━")
    results = []
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"}, delay=5)
        r = scraper.get("https://investorgain.com/report/live-ipo-gmp/331/", headers=CHROME_HEADERS, timeout=30)
        if r.status_code != 200:
            return results
        soup = BeautifulSoup(r.text, "lxml")
        table = soup.find("table", id=re.compile(r"ipo", re.I)) or soup.find("table")
        if not table:
            return results
        headers_row = [th.get_text(strip=True) for th in table.find_all("th")]
        for row in table.find_all("tr")[1:]:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cols:
                continue
            entry = {"source": "Investorgain", "name": cols[0] if cols else ""}
            for i, h in enumerate(headers_row):
                if i >= len(cols):
                    break
                hl = h.lower()
                if "gmp" in hl:
                    entry["gmp"] = cols[i]
                elif "expected price" in hl:
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
        log.warning(f"  Investorgain error: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE C — NSE India (Playwright with --disable-http2)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_nse() -> list[dict]:
    log.info("━━ SOURCE C: NSE (Playwright + HTTP/1.1) ━━")
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
                    log.info(f"  NSE intercepted: {url.split('/')[-1]}")
                except:
                    pass

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-http2",                # ← force HTTP/1.1
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            ctx = browser.new_context(
                user_agent=CHROME_HEADERS["User-Agent"],
                locale="en-IN",
                viewport={"width": 1366, "height": 768},
                extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
            )
            page = ctx.new_page()
            page.on("response", handle_response)

            # Navigate with 'commit' – minimal waiting
            try:
                page.goto("https://www.nseindia.com/market-data/all-upcoming-issues-ipo", wait_until="commit", timeout=30000)
                time.sleep(4)   # let API calls fire
            except Exception as e:
                log.warning(f"  NSE navigation error (but may still capture APIs): {e}")

            browser.close()

        for url, body in captured:
            parsed = _parse_nse_json(body)
            results.extend(parsed)

        # If no API data, fallback to HTML scraping
        if not results:
            log.info("  NSE: no API data, trying HTML fallback...")
            results = _fetch_nse_html_fallback()

        log.info(f"  ✓ NSE: {len(results)} IPOs")
    except Exception as e:
        log.warning(f"  NSE error: {e}")
    return results


def _parse_nse_json(data) -> list[dict]:
    """Parse NSE JSON (flexible)."""
    results = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                results.append({
                    "source": "NSE",
                    "name": item.get("companyName") or item.get("symbol") or "",
                    "open_date": item.get("openDate") or item.get("bidStartDate") or "",
                    "close_date": item.get("closeDate") or item.get("bidEndDate") or "",
                    "issue_price": item.get("issuePrice") or item.get("price") or "",
                    "listing_date": item.get("listingDate") or "",
                })
    elif isinstance(data, dict):
        for key in ["data", "ipoData", "upcomingIPO", "currentIPO", "allIpo", "ipos"]:
            if key in data:
                return _parse_nse_json(data[key])
        # direct dict with companyName
        if "companyName" in data:
            results.append({
                "source": "NSE",
                "name": data.get("companyName", ""),
                "open_date": data.get("openDate", ""),
                "close_date": data.get("closeDate", ""),
                "issue_price": data.get("issuePrice", ""),
                "listing_date": data.get("listingDate", ""),
            })
    return results


def _fetch_nse_html_fallback() -> list[dict]:
    """Scrape NSE IPO page directly (if API fails)."""
    results = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--disable-http2"])
            page = browser.new_page()
            page.goto("https://www.nseindia.com/market-data/all-upcoming-issues-ipo", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("table", timeout=10000)
            html = page.content()
            browser.close()
        soup = BeautifulSoup(html, "lxml")
        for table in soup.find_all("table"):
            for row in table.find_all("tr")[1:]:
                cols = [td.get_text(strip=True) for td in row.find_all("td")]
                if cols and any(kw in cols[0].lower() for kw in ["ipo", "ltd", "limited"]):
                    results.append({"source": "NSE", "name": cols[0]})
    except Exception as e:
        log.warning(f"  NSE HTML fallback error: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE D — Screener.in (unchanged, works)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_screener() -> list[dict]:
    log.info("━━ SOURCE D: Screener.in ━━")
    results = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            page = browser.new_context(user_agent=CHROME_HEADERS["User-Agent"]).new_page()
            page.goto("https://www.screener.in/ipo/recent/", wait_until="domcontentloaded", timeout=25000)
            page.wait_for_selector("table", timeout=10000)
            html = page.content()
            browser.close()
        soup = BeautifulSoup(html, "lxml")
        results = _parse_generic_ipo_tables(soup, source="Screener")
        log.info(f"  ✓ Screener: {len(results)} IPOs")
    except Exception as e:
        log.warning(f"  Screener error: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE E — Groww (fixed API intercept)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_groww() -> list[dict]:
    log.info("━━ SOURCE E: Groww (API intercept) ━━")
    results = []
    try:
        from playwright.sync_api import sync_playwright

        captured = []

        def handle_response(response):
            url = response.url
            # Groww IPO endpoints (updated)
            if any(kw in url for kw in ["/ipos", "/ipo/detail", "charter/v3", "ipo/list"]):
                try:
                    body = response.json()
                    captured.append((url, body))
                    log.info(f"  Groww intercepted: {url}")
                except:
                    pass

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=CHROME_HEADERS["User-Agent"], viewport={"width": 1366, "height": 768})
            page = ctx.new_page()
            page.on("response", handle_response)
            page.goto("https://groww.in/ipo", wait_until="domcontentloaded", timeout=30000)
            time.sleep(8)   # longer wait for React to load
            browser.close()

        for url, body in captured:
            parsed = _parse_groww_json(body)
            results.extend(parsed)

        if not results:
            log.info("  Groww: no API, DOM fallback...")
            results = _fetch_generic_playwright("https://groww.in/ipo", "Groww", wait_ms=8000)

        log.info(f"  ✓ Groww: {len(results)} IPOs")
    except Exception as e:
        log.warning(f"  Groww error: {e}")
    return results


def _parse_groww_json(data) -> list[dict]:
    results = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and any(k in item for k in ["ipoName", "companyName", "name"]):
                results.append({
                    "source": "Groww",
                    "name": item.get("ipoName") or item.get("companyName") or item.get("name", ""),
                    "open_date": item.get("openDate") or item.get("startDate") or "",
                    "close_date": item.get("closeDate") or item.get("endDate") or "",
                    "issue_price": item.get("issuePrice") or item.get("priceRange") or "",
                    "lot_size": item.get("lotSize") or item.get("minOrderQty") or "",
                    "gmp": item.get("gmp") or item.get("greyMarketPremium") or "",
                    "listing_date": item.get("listingDate", ""),
                })
    elif isinstance(data, dict):
        for key in ["data", "ipos", "ipoList", "upcoming", "open", "result"]:
            if key in data:
                results.extend(_parse_groww_json(data[key]))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE F — IndiaTrade (unchanged but improved dedup later)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_indiatrade() -> list[dict]:
    log.info("━━ SOURCE F: IndiaTrade ━━")
    results = []
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
        r = scraper.get("https://ipo.indiratrade.com/Home", headers=CHROME_HEADERS, timeout=20)
        if r.status_code == 200 and len(r.text) > 2000:
            soup = BeautifulSoup(r.text, "lxml")
            results = _parse_generic_ipo_tables(soup, source="IndiaTrade")
        if not results:
            results = _fetch_generic_playwright("https://ipo.indiratrade.com/Home", "IndiaTrade", wait_ms=5000)
        log.info(f"  ✓ IndiaTrade: {len(results)} IPOs")
    except Exception as e:
        log.warning(f"  IndiaTrade error: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Generic helpers
# ══════════════════════════════════════════════════════════════════════════════
def _parse_generic_ipo_tables(soup: BeautifulSoup, source: str) -> list[dict]:
    results = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
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
                elif "price" in h:
                    entry["issue_price"] = cols[i]
                elif "lot" in h:
                    entry["lot_size"] = cols[i]
                elif "gmp" in h:
                    entry["gmp"] = cols[i]
                elif "list" in h and "date" in h:
                    entry["listing_date"] = cols[i]
            results.append(entry)
    return results


def _fetch_generic_playwright(url: str, source: str, wait_ms: int = 5000) -> list[dict]:
    results = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            html = page.content()
            browser.close()
        soup = BeautifulSoup(html, "lxml")
        results = _parse_generic_ipo_tables(soup, source=source)
    except Exception as e:
        log.warning(f"  Generic PW [{source}] error: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Deduplication (improved)
# ══════════════════════════════════════════════════════════════════════════════
def normalise_name(name: str) -> str:
    if not name:
        return ""
    name = name.lower().strip()
    # remove common suffixes
    name = re.sub(r"\b(limited|ltd|pvt|private|public|co\.?|inc|corp|sme ipo|(\(sme ipo\))|(sme))", "", name)
    name = re.sub(r"[^\w\s]", " ", name)   # remove punctuation
    name = re.sub(r"\s+", " ", name).strip()
    return name


def are_same_ipo(a: dict, b: dict) -> bool:
    """Return True if two IPO records likely refer to the same company."""
    name_a = normalise_name(a.get("name", ""))
    name_b = normalise_name(b.get("name", ""))
    if not name_a or not name_b:
        return False
    # exact match after normalisation
    if name_a == name_b:
        return True
    # fuzzy match if very similar
    if SequenceMatcher(None, name_a, name_b).ratio() > 0.85:
        # additionally check overlapping dates (if both have them)
        open_a = a.get("open_date", "")
        open_b = b.get("open_date", "")
        if open_a and open_b and open_a != open_b:
            return False
        return True
    return False


def deduplicate(all_results: list[dict]) -> list[dict]:
    merged = []
    for item in all_results:
        found = False
        for existing in merged:
            if are_same_ipo(existing, item):
                # Merge: keep the one with more fields
                if sum(bool(v) for v in item.values()) > sum(bool(v) for v in existing.values()):
                    merged.remove(existing)
                    item["source"] = f"{existing.get('source','')}, {item.get('source','')}".strip(", ")
                    merged.append(item)
                else:
                    existing["source"] = f"{existing.get('source','')}, {item.get('source','')}".strip(", ")
                found = True
                break
        if not found:
            merged.append(item)
    return merged


# ══════════════════════════════════════════════════════════════════════════════
# Output
# ══════════════════════════════════════════════════════════════════════════════
def print_results(results: list[dict]):
    if not results:
        print("\n⚠️  No IPO data collected.\n")
        return
    print(f"\n{'═'*70}")
    print(f"  IPO DATA  —  fetched {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"  Total: {len(results)} unique IPOs from {len(set(r['source'] for r in results))} sources")
    print(f"{'═'*70}\n")
    for ipo in results:
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
        print(f"    {src}\n")


def save_json(results: list[dict], path: str = "ipo_data.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"  💾 Saved → {path}")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════
def main():
    all_results = []
    all_results += fetch_chittorgarh()
    all_results += fetch_investorgain()
    all_results += fetch_nse()
    all_results += fetch_screener()
    all_results += fetch_groww()
    all_results += fetch_indiatrade()

    merged = deduplicate(all_results)
    print_results(merged)
    save_json(merged)
    return merged


if __name__ == "__main__":
    main()
