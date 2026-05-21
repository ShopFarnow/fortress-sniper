#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          IPO SNIPER v5.7 — PRODUCTION STEALTH & SCHEMA RESILIENT             ║
║  Live Market Ingestion · Quant Portfolio Engine · Shariah Matrix · TG Alerts ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os
import re
import math
import time
import json
import random
import logging
import sqlite3
import html as html_lib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

# ═══════════════════════════════════════════════════════════
# CONFIGURATION MANAGEMENT
# ═══════════════════════════════════════════════════════════
IPO_DB_PATH      = Path("data/ipo_sniper_v5.db")
FALLBACK_CSV     = Path("data/ipo_fallback_v5.csv")
JSON_EXPORT      = Path("data/ipo_latest_run.json")
VERSION          = "IPO-SNIPER-v5.7-OPEN-ONLY-TELEGRAM"
MC_RUNS          = 50_000
KELLY_FRACTION   = 0.25
MAX_SYNDICATE    = 10
SEED             = 42
np.random.seed(SEED)
random.seed(SEED)

CHITT_LIVE_URLS = {
    "Mainboard": "https://www.chittorgarh.com/report/ipo-subscription-status/10/",
    "SME":       "https://www.chittorgarh.com/report/sme-ipo-subscription-status/10/",
}
CHITT_UPCOMING_URLS = {
    "Mainboard": "https://www.chittorgarh.com/report/upcoming-ipo/6/",
}

NSE_ENDPOINTS = [
    ("https://www.nseindia.com/api/ipo-info",                            "Mainboard"),
    ("https://www.nseindia.com/api/emerge-live?category=ipo",            "SME"),
    ("https://www.nseindia.com/api/live-analysis-data?index=CURRENT+IPO","Mainboard"),
]
NSE_WARMUP = [
    "https://www.nseindia.com",
    "https://www.nseindia.com/market-data/upcoming-issues-ipo",
    "https://www.nseindia.com/market-data/live-equity-market?selected=SME",
]

BASE_WEIGHTS: Dict[str, float] = {
    "gmp": 0.22, "sub": 0.28, "sentiment": 0.18,
    "trend": 0.10, "size": 0.08, "halal": 0.14,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-8s │ %(message)s")
log = logging.getLogger("IPO-SNIPER-v5")

TODAY = datetime.today().date()

# ═══════════════════════════════════════════════════════════
# TYPE-SAFE COERCION UTILITIES
# ═══════════════════════════════════════════════════════════
def _flt(v, default: float = 0.0) -> float:
    try:
        m = re.search(r"[\d.]+", str(v).replace(",", ""))
        return float(m.group()) if m else default
    except Exception: return default

def _int(v, default: int = 0) -> int:
    try:
        m = re.search(r"\d+", str(v).replace(",", ""))
        return int(m.group()) if m else default
    except Exception: return default

def _jitter(lo: float = 1.5, hi: float = 3.5):
    time.sleep(random.uniform(lo, hi))

def _parse_date(text: str) -> Optional[object]:
    text = str(text).strip()
    text = re.sub(r"\s*\(.*?\)", "", text).strip()
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%m-%Y", "%Y-%m-%d", "%b %d, %Y", "%d/%m/%Y", "%B %d, %Y", "%d %B %Y"):
        try: return datetime.strptime(text, fmt).date()
        except ValueError: pass
    return None

def _parse_price_band(text: str) -> Tuple[float, float]:
    nums = re.findall(r"[\d.]+", str(text).replace(",", ""))
    if len(nums) >= 2: return float(nums[0]), float(nums[-1])
    if len(nums) == 1:
        v = float(nums[0])
        return round(v * 0.97, 2), v
    return 95.0, 100.0

def _clean_symbol(raw: str) -> str:
    s = BeautifulSoup(str(raw), "html.parser").get_text(strip=True)
    return re.sub(r"\s+", " ", s).strip()

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8",
    "Cache-Control": "no-cache",
}
SKIP_SYMBOLS = {"company", "name", "issuer", "no records found", "compare", "click here", "", "open", "closed", "upcoming", "sno", "sr", "sr.", "#"}

def _make_session(referer: str = "https://www.google.com/") -> requests.Session:
    s = requests.Session()
    s.headers.update({**BROWSER_HEADERS, "Referer": referer})
    return s

def _sniff_columns(headers: List[str]) -> Dict[str, int]:
    col: Dict[str, int] = {}
    for i, h in enumerate(headers):
        h = h.lower().strip()
        if any(k in h for k in ("company", "issuer", "name", "ipo")): col.setdefault("sym", i)
        elif any(k in h for k in ("issue size", "size", "amt", "cr")): col.setdefault("size", i)
        elif any(k in h for k in ("price band", "price", "band", "rate")): col.setdefault("price", i)
        elif any(k in h for k in ("close date", "closing date", "close", "end")): col.setdefault("close", i)
        elif any(k in h for k in ("open date", "opening", "open")): col.setdefault("open", i)
        elif any(k in h for k in ("lot size", "lot", "qty", "shares")): col.setdefault("lot", i)
        elif "gmp" in h or "premium" in h: col.setdefault("gmp", i)
        elif any(k in h for k in ("subscription", "subscribed", "sub", "times", "x")): col.setdefault("sub", i)
    col.setdefault("sym", 0)
    return col

# ═══════════════════════════════════════════════════════════
# HTML TABLE PARSER ENGINE
# ═══════════════════════════════════════════════════════════
def _parse_html_table(table, ipo_type: str, source_tag: str, is_upcoming: bool = False) -> pd.DataFrame:
    sector = "Mainboard" if "main" in ipo_type.lower() else "SME"
    rows = table.find_all("tr")
    if len(rows) < 2: return pd.DataFrame()

    hdr = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
    col = _sniff_columns(hdr)
    records = []
    
    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) <= max(col.values(), default=0): continue

        def _c(key, default=""):
            idx = col.get(key)
            return cells[idx].get_text(strip=True) if idx is not None and idx < len(cells) else default

        lnk = cells[col["sym"]].find("a")
        symbol = _clean_symbol(lnk.get_text(strip=True) if lnk else cells[col["sym"]].get_text(strip=True))
        if not symbol or symbol.lower() in SKIP_SYMBOLS or len(symbol) < 2: continue

        size = _flt(_c("size", "50"), 50.0)
        if size > 50_000: size /= 1e7
        lo, hi = _parse_price_band(_c("price", ""))
        lot = _int(_c("lot", "")) or (1000 if sector == "SME" else 50)

        # Fix O: Replaces upcoming price defaults with explicit TBD boundaries
        if is_upcoming and hi <= 100 and lo >= 95: lo, hi = 0.0, 0.0

        close_raw = _c("close", "")
        close_dt = _parse_date(close_raw) if close_raw else None
        if close_dt is None:
            close_dt = TODAY + timedelta(days=20 if is_upcoming else 1)

        gmp_raw = _c("gmp", "")
        gmp = (_flt(gmp_raw, 0.0) / 100 if _flt(gmp_raw, 0.0) > 1 else _flt(gmp_raw, 0.0)) if gmp_raw else 0.0
        sub = _flt(_c("sub", "0"), 0.0)

        records.append({
            "Symbol": symbol, "Sector": sector, "IssueSizeCr": round(size, 2),
            "PriceBandLower": lo, "PriceBandUpper": hi, "LotSize": lot,
            "GMP": round(gmp, 4), "gmp_pct": round(gmp * 100, 2), "SubscriptionTimes": round(sub, 2),
            "CloseDate": close_dt.strftime("%Y-%m-%d"), "DaysToClose": (close_dt - TODAY).days,
            "IsUpcoming": is_upcoming, "Source": source_tag,
        })
    return pd.DataFrame(records)

def _parse_ajax_rows(rows_raw: list, ipo_type: str, source_tag: str, is_upcoming: bool = False) -> pd.DataFrame:
    if not rows_raw: return pd.DataFrame()
    sector = "Mainboard" if "main" in ipo_type.lower() else "SME"
    records = []
    
    for raw in rows_raw[:80]:
        try:
            if isinstance(raw, dict):
                cells_clean = {k: _clean_symbol(str(v)) for k, v in raw.items()}
                sym_key   = next((k for k in cells_clean if any(x in k.lower() for x in ("company","name","issuer","ipo"))), list(cells_clean.keys())[0])
                size_key  = next((k for k in cells_clean if any(x in k.lower() for x in ("size","cr","amt"))), None)
                price_key = next((k for k in cells_clean if any(x in k.lower() for x in ("price","band"))), None)
                close_key = next((k for k in cells_clean if any(x in k.lower() for x in ("close","end"))), None)
                lot_key   = next((k for k in cells_clean if any(x in k.lower() for x in ("lot","qty"))), None)
                gmp_key   = next((k for k in cells_clean if "gmp" in k.lower() or "premium" in k.lower()), None)
                sub_key   = next((k for k in cells_clean if any(x in k.lower() for x in ("sub","times","subscri"))), None)

                symbol = _clean_symbol(cells_clean.get(sym_key, ""))
                size = _flt(cells_clean.get(size_key, "50") if size_key else "50", 50.0)
                lo, hi = _parse_price_band(cells_clean.get(price_key, "100") if price_key else "100")
                lot = _int(cells_clean.get(lot_key, "") if lot_key else "") or (1000 if sector == "SME" else 50)
                close_dt = _parse_date(cells_clean.get(close_key, "") if close_key else "") or (TODAY + timedelta(days=20 if is_upcoming else 10))
                sub = _flt(cells_clean.get(sub_key, "0") if sub_key else "0", 0.0)
                gmp_raw = cells_clean.get(gmp_key, "")
                gmp = (_flt(gmp_raw, 0.0) / 100 if _flt(gmp_raw, 0.0) > 1 else _flt(gmp_raw, 0.0)) if gmp_raw else 0.0
            else:
                clean = [_clean_symbol(str(c)) for c in raw]
                if not clean or len(clean) < 4: continue
                
                symbol = clean[0]
                size, lo, hi, lot, sub, gmp = 50.0, 95.0, 100.0, (1000 if sector=="SME" else 50), 0.0, 0.0
                close_dt = TODAY + timedelta(days=20 if is_upcoming else 10)

                for i, cell in enumerate(clean[1:], start=1):
                    cell = cell.strip()
                    if not cell: continue
                    d = _parse_date(cell)
                    if d and i >= 3: close_dt = d; continue
                    if re.search(r"\d+\s*[-–]\s*\d+", cell): lo, hi = _parse_price_band(cell); continue
                    nums = re.findall(r"[\d.]+", cell.replace(",",""))
                    if not nums: continue
                    v = float(nums[0])
                    if "x" in cell.lower() or ("." in cell and 0.1 <= v <= 500 and i >= 4): sub = v
                    elif v > 10 and v < 10_000 and size == 50.0: size = v
                    elif v == int(v) and 10 <= v <= 5000: lot = int(v)
                    elif v < 5 and "%" in cell: gmp = v / 100

            if not symbol or symbol.lower() in SKIP_SYMBOLS or len(symbol) < 2: continue
            if size > 50_000: size /= 1e7

            records.append({
                "Symbol": _clean_symbol(symbol), "Sector": sector, "IssueSizeCr": round(size, 2),
                "PriceBandLower": lo, "PriceBandUpper": hi, "LotSize": lot,
                "GMP": round(gmp, 4), "gmp_pct": round(gmp * 100, 2), "SubscriptionTimes": round(sub, 2),
                "CloseDate": close_dt.strftime("%Y-%m-%d"), "DaysToClose": (close_dt - TODAY).days,
                "IsUpcoming": is_upcoming, "Source": source_tag + "_ajax",
            })
        except Exception as e: log.debug(f"  AJAX structural parsing row trace error: {e}")
    return pd.DataFrame(records)

# ═══════════════════════════════════════════════════════════
# SYSTEM EXTRACTION DATA SEED VECTOR FEEDS
# ═══════════════════════════════════════════════════════════
def _fetch_chitt_playwright(url: str, ipo_type: str, source_tag: str, is_upcoming: bool = False) -> pd.DataFrame:
    if not PLAYWRIGHT_OK: return pd.DataFrame()
    log.info(f"  PW [{ipo_type}] → {url}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"], extra_http_headers={"Accept-Language": "en-IN,en-GB;q=0.9"}, viewport={"width": 1280, "height": 900})
            page = ctx.new_page()

            intercepted: List[dict] = []
            def _on_resp(resp):
                if resp.status == 200 and "chittorgarh" in resp.url and "json" in resp.headers.get("content-type", ""):
                    try:
                        rows = resp.json().get("data", resp.json().get("aaData", []))
                        if rows: intercepted.extend(rows)
                    except Exception: pass
            page.on("response", _on_resp)

            page.goto(url, wait_until="networkidle", timeout=55_000)
            try: page.wait_for_selector("table tbody tr td:not(.dataTables_empty)", timeout=15_000)
            except PWTimeout: pass

            if intercepted:
                browser.close()
                return _parse_ajax_rows(intercepted, ipo_type, source_tag, is_upcoming)

            soup = BeautifulSoup(page.content(), "html.parser")
            browser.close()
            for tbl in soup.find_all("table"):
                if len(tbl.find_all("tr")) > 3:
                    df = _parse_html_table(tbl, ipo_type, source_tag + "_html", is_upcoming)
                    if not df.empty: return df
    except Exception as e: log.warning(f"  PW dynamic memory runtime instance drop: {e}")
    return pd.DataFrame()

def _fetch_chitt_http(url: str, ipo_type: str, source_tag: str, is_upcoming: bool = False) -> pd.DataFrame:
    sess = _make_session("https://www.chittorgarh.com/")
    try:
        sess.get("https://www.chittorgarh.com/", timeout=12)
        _jitter(1.5, 3.0)
        resp = sess.get(url, timeout=25)
        if resp.status_code != 200: return pd.DataFrame()
        soup = BeautifulSoup(resp.text, "html.parser")
        for sel in ["table.table-striped", "table.table-bordered", ".table-responsive table", "table"]:
            for tbl in soup.select(sel):
                if len(tbl.find_all("tr")) > 3:
                    df = _parse_html_table(tbl, ipo_type, source_tag + "_http", is_upcoming)
                    if not df.empty: return df
    except Exception as e: log.warning(f"  HTTP raw stream exception context fallback: {e}")
    return pd.DataFrame()

def fetch_source_a_chittorgarh() -> pd.DataFrame:
    log.info("━━ SOURCE A: Chittorgarh Live Feeds Ingestion ━━")
    frames: List[pd.DataFrame] = []
    for itype, url in CHITT_LIVE_URLS.items():
        tag = f"chitt_live_{itype.lower()}"
        df = _fetch_chitt_playwright(url, itype, tag, is_upcoming=False)
        if df.empty: df = _fetch_chitt_http(url, itype, tag, is_upcoming=False)
        if not df.empty: log.info(f"  ✅ Live Ingested [{itype}]: {len(df)} rows"); frames.append(df)
        _jitter(2.0, 4.0)

    for itype, url in CHITT_UPCOMING_URLS.items():
        tag = f"chitt_upcoming_{itype.lower()}"
        df = _fetch_chitt_playwright(url, itype, tag, is_upcoming=True)
        if df.empty: df = _fetch_chitt_http(url, itype, tag, is_upcoming=True)
        if not df.empty: log.info(f"  ✅ Upcoming Ingested [{itype}]: {len(df)} rows"); frames.append(df)
        _jitter(1.5, 3.0)

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def fetch_source_b_investorgain() -> pd.DataFrame:
    log.info("━━ SOURCE B: Investorgain Live Metrics Ingestion ━━")
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/"

    def _parse_ig_soup(soup: BeautifulSoup) -> pd.DataFrame:
        table = (soup.find("table", {"id": "mainTable"}) or soup.find("table", {"id": re.compile(r"ipo|gmp", re.I)}) or
                 max(soup.find_all("table"), key=lambda t: len(t.find_all("tr")), default=None))
        if not table: return pd.DataFrame()

        rows = table.find_all("tr")
        if len(rows) < 2: return pd.DataFrame()
        hdr = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        col = _sniff_columns(hdr)
        records = []
        
        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells or len(cells) <= max(col.values(), default=0): continue

            # Fix N: Pre-screens metrics strictly prior to allowing string parsing maps to filter tags
            sym_raw = cells[col["sym"]].get_text(strip=True)
            status_match = re.search(r"\b(?:BSE|NSE)\s+SME([A-Z]{1,3})\b|IPOL\b|IPO([A-Z])\b", sym_raw, re.I)
            status_code = status_match.group(1).upper() if (status_match and status_match.group(1)) else status_match.group(2).upper() if (status_match and status_match.group(2)) else ""
            if "IPOL" in sym_raw.upper(): status_code = "L"

            if status_code in ("L", "C", "CT") or bool(re.search(r"@[\d.]+\s*\([+-]?[\d.]+%\)", sym_raw)) or bool(re.search(r"\bAllotted\b", sym_raw, re.I)):
                continue

            symbol = _clean_symbol(sym_raw)
            if not symbol or len(symbol) < 3 or symbol.lower() in SKIP_SYMBOLS: continue

            def _c(key, default=""):
                idx = col.get(key)
                return cells[idx].get_text(strip=True) if idx is not None and idx < len(cells) else default

            gmp_raw = _c("gmp", "")
            gmp = (_flt(gmp_raw, 0.0) / 100 if _flt(gmp_raw, 0.0) > 1 else _flt(gmp_raw, 0.0)) if gmp_raw else 0.0
            lo, hi = _parse_price_band(_c("price", "100"))
            sub = _flt(_c("sub", "0"), 0.0)
            size = _flt(_c("size", "50"), 50.0)
            lot = _int(_c("lot", "")) or 1000
            close_dt = _parse_date(_c("close", "")) or (TODAY + timedelta(days=7))

            records.append({
                "Symbol": symbol, "Sector": "Mainboard" if (hi > 250 or lot < 200) else "SME", "IssueSizeCr": round(size, 2),
                "PriceBandLower": lo, "PriceBandUpper": hi, "LotSize": lot, "GMP": round(gmp, 4), "gmp_pct": round(gmp * 100, 2),
                "SubscriptionTimes": round(sub, 2), "CloseDate": close_dt.strftime("%Y-%m-%d"), "DaysToClose": (close_dt - TODAY).days,
                "IsUpcoming": status_code == "U", "Source": "investorgain_gmp",
            })
        return pd.DataFrame(records)

    sess = _make_session("https://www.investorgain.com/")
    try:
        resp = sess.get(url, timeout=25)
        if resp.status_code == 200 and not resp.headers.get("x-deny-reason", ""):
            df = _parse_ig_soup(BeautifulSoup(resp.text, "html.parser"))
            if not df.empty: log.info(f"  ✅ SOURCE B Ingested: {len(df)} rows"); return df
    except Exception as e: log.warning(f"  Investorgain connectivity exception: {e}")

    if PLAYWRIGHT_OK:
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"]).new_page()
                page.goto(url, wait_until="networkidle", timeout=45_000)
                try: page.wait_for_selector("table tr td", timeout=12_000)
                except PWTimeout: pass
                df = _parse_ig_soup(BeautifulSoup(page.content(), "html.parser"))
                browser.close()
                if not df.empty: log.info(f"  ✅ SOURCE B (Playwright Assisted): {len(df)} rows"); return df
        except Exception as e: log.warning(f"  Investorgain browser layout interface error: {e}")
    return pd.DataFrame()

def fetch_source_c_nse() -> pd.DataFrame:
    """
    FIX P Core Patch: Implements custom user browser contexts asynchronously
    to handle remote JS session cookie validation challenge grids natively.
    """
    log.info("━━ SOURCE C: NSE India API Stealth Interception ━━")
    if not PLAYWRIGHT_OK: return pd.DataFrame()

    NSE_IPO_PAGE = "https://www.nseindia.com/market-data/upcoming-issues-ipo"
    NSE_API_PATTERNS = ["/api/getAllIpo", "/api/ipo-detail", "/api/ipo", "/api/ipo-info", "/api/emerge-live", "/api/live-analysis-data"]

    records, intercepted_data = [], []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled", "--disable-web-security"])
            ctx = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", locale="en-IN", timezone_id="Asia/Kolkata", viewport={"width": 1366, "height": 768})
            page = ctx.new_page()

            def _on_nse_resp(resp):
                try:
                    if any(pat in resp.url for pat in NSE_API_PATTERNS) and resp.status == 200:
                        if "json" in resp.headers.get("content-type", ""): # Patched response content validation layer
                            items = resp.json()
                            if not isinstance(items, list): items = items.get("data", items.get("ipoData", items.get("allIpo", items.get("ipo", []))))
                            if isinstance(items, dict): items = [items]
                            if isinstance(items, list) and items:
                                intercepted_data.extend(items)
                                log.info(f"  NSE Intercept Node Captured: {len(items)} items from {resp.url.split('/')[-1][:30]}")
                except Exception: pass

            page.on("response", _on_nse_resp)
            page.goto("https://www.nseindia.com/", wait_until="domcontentloaded", timeout=30_000)
            _jitter(1.5, 2.5)
            page.goto(NSE_IPO_PAGE, wait_until="networkidle", timeout=45_000)
            _jitter(2.0, 3.0)

            if intercepted_data:
                seen = set()
                for item in intercepted_data:
                    if not isinstance(item, dict): continue
                    sym = str(item.get("symbol", item.get("companyName", item.get("issuerName", item.get("name", ""))))).strip()
                    if not sym or len(sym) < 2 or sym in seen: continue

                    lo, hi = _parse_price_band(str(item.get("priceBand", item.get("issuePrice", "100"))))
                    size = _flt(item.get("issueSize", item.get("issueSizeCrores", item.get("totalIssueSizeCr", 50.0))), 50.0)
                    if size > 50_000: size /= 1e7
                    lot = _int(item.get("lotSize", item.get("minBidQuantity", 0))) or 50
                    sub_raw = str(item.get("subscriptionTimes", item.get("subscriptionStatus", "0")))
                    sub = _flt(re.search(r"[\d.]+", sub_raw).group() if re.search(r"[\d.]+", sub_raw) else "0")
                    close_dt = _parse_date(str(item.get("closeDate", item.get("biddingEndDate", item.get("closingDate", ""))))) or (TODAY + timedelta(days=10))
                    
                    seen.add(sym)
                    records.append({
                        "Symbol": sym, "Sector": "Mainboard" if size > 150 else "SME", "IssueSizeCr": round(size, 2),
                        "PriceBandLower": lo, "PriceBandUpper": hi, "LotSize": lot, "GMP": 0.0, "gmp_pct": 0.0,
                        "SubscriptionTimes": round(sub, 2), "CloseDate": close_dt.strftime("%Y-%m-%d"), "DaysToClose": (close_dt - TODAY).days,
                        "IsUpcoming": sub == 0.0 or close_dt > TODAY + timedelta(days=2), "Source": "nse_playwright",
                    })
            else:
                soup = BeautifulSoup(page.content(), "html.parser")
                for tbl in soup.find_all("table"):
                    if len(tbl.find_all("tr")) > 3:
                        df_tbl = _parse_html_table(tbl, "Mainboard", "nse_html", is_upcoming=False)
                        if not df_tbl.empty: browser.close(); return df_tbl
            browser.close()
        return pd.DataFrame(records)
    except Exception as e: log.warning(f"  NSE stealth execution proxy error: {e}")
    return pd.DataFrame()

def _rebuild_fallback_csv() -> pd.DataFrame:
    FALLBACK_CSV.parent.mkdir(parents=True, exist_ok=True)
    seed = [
        {"Symbol": "Fallback System Asset Alpha Ltd", "IssueSizeCr": 70.0, "PriceBandLower": 140, "PriceBandUpper": 148, "LotSize": 1000, "GMP": 0.15, "SubscriptionTimes": 4.5, "Sector": "SME", "CloseDate": (TODAY + timedelta(3)).strftime("%Y-%m-%d"), "IsUpcoming": False},
        {"Symbol": "Fallback System Asset Beta Corp",  "IssueSizeCr": 200.0, "PriceBandLower": 300, "PriceBandUpper": 320, "LotSize": 50,   "GMP": 0.35, "SubscriptionTimes": 12.2, "Sector": "Mainboard", "CloseDate": (TODAY + timedelta(5)).strftime("%Y-%m-%d"), "IsUpcoming": False},
    ]
    df = pd.DataFrame(seed)
    df["Source"] = "FALLBACK_SEED_EMERGENCY"
    df.to_csv(FALLBACK_CSV, index=False)
    return df

# ═══════════════════════════════════════════════════════════
# PIPELINE ENRICHMENT AND SANITIZATION LAYER
# ═══════════════════════════════════════════════════════════
def _validate_row(row: pd.Series) -> Tuple[bool, str]:
    sym = str(row.get("Symbol", "")).strip()
    if not sym or len(sym) < 2 or sym.lower() in ("unknown", "nan", "none", ""): return False, "invalid_symbol"
    if float(row.get("PriceBandUpper", 0)) < 0: return False, "price_out_of_bounds"
    if int(row.get("DaysToClose", 0)) < 0: return False, "deal_closed_historical"
    if bool(row.get("IsUpcoming", False)) and int(row.get("DaysToClose", 0)) > 30: return False, "upcoming_horizon_excessive"
    return True, ""

def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    for col, val in REQUIRED_DEFAULTS.items():
        if col not in df.columns: df[col] = val
    for c in ("IssueSizeCr", "PriceBandLower", "PriceBandUpper", "LotSize", "GMP", "gmp_pct", "SubscriptionTimes", "DaysToClose"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(REQUIRED_DEFAULTS.get(c, 0))

    if "source" in df.columns and "Source" not in df.columns: df["Source"] = df["source"]
    if "IsUpcoming" not in df.columns: df["IsUpcoming"] = False

    df["gmp_pct"] = df["GMP"].apply(lambda g: round(float(g) * 100, 2))
    df["DaysToClose"] = df["CloseDate"].apply(lambda x: (lambda d: (d - TODAY).days if d else -999)(_parse_date(str(x))))

    valid_rows = [row for _, row in df.iterrows() if _validate_row(row)[0]]
    return pd.DataFrame(valid_rows).reset_index(drop=True) if valid_rows else pd.DataFrame()

def fetch_unified_calendar() -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    
    df_a = fetch_source_a_chittorgarh()
    if not df_a.empty: frames.append(df_a)
    df_b = fetch_source_b_investorgain()
    if not df_b.empty: frames.append(df_b)
    df_c = fetch_source_c_nse()
    if not df_c.empty: frames.append(df_c)

    if frames:
        raw = pd.concat(frames, ignore_index=True)
        enriched = _enrich(raw)
        if not enriched.empty:
            best_gmp = enriched[enriched["gmp_pct"] > 0].sort_values("gmp_pct", ascending=False).drop_duplicates("Symbol", keep="first")[["Symbol", "GMP", "gmp_pct"]]
            enriched["_prio"] = enriched["IsUpcoming"].apply(lambda x: 1 if x else 0)
            deduped = enriched.sort_values(["_prio", "SubscriptionTimes"], ascending=[True, False]).drop_duplicates("Symbol", keep="first").drop(columns=["_prio"]).reset_index(drop=True)
            
            if not best_gmp.empty:
                deduped = deduped.drop(columns=["GMP", "gmp_pct"], errors="ignore").merge(best_gmp, on="Symbol", how="left")
                deduped["GMP"] = deduped["GMP"].fillna(0.0)
                deduped["gmp_pct"] = deduped["gmp_pct"].fillna(0.0)

            return deduped

    return _enrich(_rebuild_fallback_csv())

# ═══════════════════════════════════════════════════════════
# QUANT PORTFOLIO CALCULATORS (BAYESIAN + KELLY RATIOS)
# ═══════════════════════════════════════════════════════════
def bayesian_weight_update(df: pd.DataFrame) -> Dict[str, float]:
    w = BASE_WEIGHTS.copy()
    if df.empty: return w
    live = df[~df["IsUpcoming"].fillna(False).astype(bool)]
    avg_sub = live["SubscriptionTimes"].mean() if not live.empty else 1.0
    if avg_sub > 80:
        w["sub"] += 0.10; w["gmp"] -= 0.05; w["halal"] -= 0.05
        log.info(f"📈 Adaptive Weight Matrix: HYPER-BULL (avg sub={avg_sub:.1f}x)")
    elif avg_sub < 15:
        w["gmp"] += 0.10; w["sub"] -= 0.10; w["halal"] += 0.05
        log.info(f"📉 Adaptive Weight Matrix: TEPID (avg sub={avg_sub:.1f}x)")
    total = sum(w.values())
    return {k: round(v / total, 6) for k, v in w.items()}

def monte_carlo_allotment(sub, lot, size_cr, price):
    if sub <= 0 or lot <= 0 or price <= 0 or size_cr <= 0: return 0.0, 0.0, 0.0
    retail = size_cr * 1e7 * 0.35
    avail = max(1, int(retail / (lot * price)))
    total = max(avail + 1, int(avail * sub))
    p_true = avail / total
    
    hits = np.random.binomial(1, p_true, MC_RUNS)
    p_hat = hits.mean()
    z = 1.96
    denom = 1 + z**2 / MC_RUNS
    center = (p_hat + z**2 / (2 * MC_RUNS)) / denom
    spread = (z * math.sqrt(p_hat * (1 - p_hat) / MC_RUNS + z**2 / (4 * MC_RUNS**2))) / denom
    return round(p_hat, 6), max(0.0, round(center - spread, 6)), min(1.0, round(center + spread, 6))

def compute_allotment(row: pd.Series) -> AllotmentProfile:
    sub = max(0.1, float(row["SubscriptionTimes"]))
    price = float(row["PriceBandUpper"])
    lot = int(row["LotSize"])
    size = float(row["IssueSizeCr"])
    gmp = float(row["GMP"])

    if price <= 0: return AllotmentProfile(str(row["Symbol"]), 0.0, {}, 1, 0.0, 0.0, 0.0, (0.0, 0.0))

    p_mc, ci_lo, ci_hi = monte_carlo_allotment(sub, lot, size, price)
    matrix = {k: round(1 - (1 - p_mc) ** k, 6) for k in range(1, MAX_SYNDICATE + 1)}
    gain = gmp * price * lot
    
    # REPAIRED KELLY EDGE RATIO: Sizing tracks specific lock deadlines and listing deviations
    days_locked = max(6, int(row.get("DaysToClose", 7))) + 2
    opp_cost    = (lot * price) * 0.055 * (days_locked / 365)
    gap_risk    = price * lot * 0.025
    effective_risk = max(1.0, opp_cost + gap_risk)
    
    b_odds = gain / effective_risk
    cost = lot * price

    best_k, best_ev = 1, -float("inf")
    for k, p_win in matrix.items():
        ev = p_win * gain - k * (cost + 500.0)
        if ev > best_ev: best_ev = ev; best_k = k

    p_opt = matrix[best_k]
    f_star = (b_odds * p_opt - (1 - p_opt)) / max(0.01, b_odds)
    
    return AllotmentProfile(
        symbol=str(row["Symbol"]), p_single_mc=p_mc, syndicate_matrix=matrix, optimal_syndicate=best_k,
        kelly_pct=round(max(0.0, KELLY_FRACTION * f_star) * 100, 2), ev_inr=round(p_opt * gain, 2), roi_pct=round((round(p_opt * gain, 2) / max(1.0, cost * best_k)) * 100, 4),
        ci_95=(ci_lo, ci_hi)
    )

def run_shariah(row: pd.Series) -> ShariahVerdict:
    gmp, sub, size = float(row["GMP"]), float(row["SubscriptionTimes"]), float(row["IssueSizeCr"])
    barakah = 100.0
    issues = []
    
    if gmp > 0.40 and sub > 80: barakah -= 25; issues.append("Speculative Demand Bubble (Najash Active)")
    if size < 20 and size > 0: barakah -= 15; issues.append("Microcap Liquidity Hazard (<₹20 Cr)")
    if str(row["Sector"]) == "SME" and sub > 200: barakah -= 10; issues.append("SME Hyper-Pump Risk (Sub>200x)")

    return ShariahVerdict(
        str(row["Symbol"]), "TIER_1_SHARIAH_COMPLIANT" if barakah >= 80 else "TIER_2_CONDITIONAL", max(0.0, barakah), gmp > 0.40 and sub > 80,
        "QABDA MANAGEMENT DIRECTIVE: Secondary transaction loops are locked until physical share delivery credits the Demat account ledger completely.", issues
    )

def master_score(row, allot, shariah, w) -> Dict:
    days = max(0, int(row["DaysToClose"]))
    tf = 1.0 if days >= 7 else (0.5 + 0.5 * days / 7)
    gmp, sub, size = float(row["GMP"]), float(row["SubscriptionTimes"]), float(row["IssueSizeCr"])
    is_upcoming = bool(row.get("IsUpcoming", False))

    s_gmp = min(100.0, gmp * 200)
    s_sub = min(100.0, sub) * tf
    s_sent = min(100.0, 40.0 + (20 if sub > 50 else 10 if sub > 25 else 0) + (20 if gmp > 0.40 else 10 if gmp > 0.20 else 0))
    s_size = 100 if size <= 20 else 80 if size <= 50 else 50 if size <= 100 else 20
    s_hal = shariah.barakah_index

    raw = (s_gmp * w["gmp"] + s_sub * w["sub"] + s_sent * w["sentiment"] + 50.0 * w["trend"] + s_size * w["size"] + s_hal * w["halal"])
    final = min(100.0, max(0.0, round(raw, 1)))

    if is_upcoming and final > 59.0: final = 59.0
    return {"FinalScore": final, "Verdict": "🔥 PEARL" if final >= 80 else "✅ STRONG BUY" if final >= 70 else "📈 MODERATE" if final >= 60 else "🕐 UPCOMING" if is_upcoming else "❌ SKIP"}

# ═══════════════════════════════════════════════════════════
# SCHEMA MIGRATION & TELEGRAM DISPATCH LAYER
# ═══════════════════════════════════════════════════════════
def init_db():
    IPO_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(IPO_DB_PATH)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ipo_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT, symbol TEXT, sector TEXT, final_score REAL, verdict TEXT, is_upcoming INTEGER,
                subscription_x REAL, gmp_pct REAL, issue_size_cr REAL, price_upper REAL, lot_size INTEGER, close_date TEXT, days_to_close INTEGER,
                p_single_mc REAL, ci_lo REAL, ci_hi REAL, optimal_syndicate INTEGER, kelly_pct REAL, ev_inr REAL, roi_pct REAL,
                barakah REAL, halal_tier TEXT, najash_alert INTEGER, source TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(run_date, symbol)
            )
        """)
        existing_cols = {row[1] for row in con.execute("PRAGMA table_info(ipo_scans)")}
        migrations = {
            "is_upcoming": "ALTER TABLE ipo_scans ADD COLUMN is_upcoming INTEGER DEFAULT 0",
            "source": "ALTER TABLE ipo_scans ADD COLUMN source TEXT DEFAULT 'unknown'",
            "days_to_close": "ALTER TABLE ipo_scans ADD COLUMN days_to_close INTEGER DEFAULT 0",
            "barakah": "ALTER TABLE ipo_scans ADD COLUMN barakah REAL DEFAULT 0",
            "halal_tier": "ALTER TABLE ipo_scans ADD COLUMN halal_tier TEXT DEFAULT ''",
            "najash_alert": "ALTER TABLE ipo_scans ADD COLUMN najash_alert INTEGER DEFAULT 0",
            "optimal_syndicate": "ALTER TABLE ipo_scans ADD COLUMN optimal_syndicate INTEGER DEFAULT 1",
            "kelly_pct": "ALTER TABLE ipo_scans ADD COLUMN kelly_pct REAL DEFAULT 0",
            "ev_inr": "ALTER TABLE ipo_scans ADD COLUMN ev_inr REAL DEFAULT 0",
            "roi_pct": "ALTER TABLE ipo_scans ADD COLUMN roi_pct REAL DEFAULT 0",
            "p_single_mc": "ALTER TABLE ipo_scans ADD COLUMN p_single_mc REAL DEFAULT 0",
            "ci_lo": "ALTER TABLE ipo_scans ADD COLUMN ci_lo REAL DEFAULT 0",
            "ci_hi": "ALTER TABLE ipo_scans ADD COLUMN ci_hi REAL DEFAULT 0",
        }
        for field_col, ddl in migrations.items():
            if field_col not in existing_cols: con.execute(ddl); log.info(f"🗄  Altered schema cache: injected field -> '{field_col}'")
    log.info("🗄  DB initialized.")

def _tg_clean_symbol(sym: str) -> str:
    """
    FIX L REPAIR: Cleans trailing meta elements from Investorgain frames cleanly via end-anchors,
    preventing any destructive mid-string mutations on valid alpha characters.
    """
    sym = re.sub(r"(?<=[A-Za-z0-9.])\s*(?:BSE|NSE)\s*(?:SME[A-Z]{0,3}|EMERGE[A-Z]{0,3})(?:@[\d.]+\s*\([+-]?[\d.]+%\))?\s*$", "", sym, flags=re.IGNORECASE).strip()
    sym = re.sub(r"(?<=[A-Za-z0-9.])IPO[A-Z]?(?:@[\d.]+\s*\([+-]?[\d.]+%\))?\s*$", "", sym, flags=re.IGNORECASE).strip()
    sym = re.sub(r"@[\d.,]+\s*\([+-]?[\d.%]+\)\s*$", "", sym).strip()
    return re.sub(r"\s+", " ", sym).strip() or "UNKNOWN"

def send_telegram_alerts(df: pd.DataFrame, allots: dict, shariahs: dict):
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id: return

    live_df = df[~df["IsUpcoming"].fillna(False).astype(bool)]
    upco_df = df[df["IsUpcoming"].fillna(False).astype(bool)]

    # 1. Pipeline Index Notification Frame
    header = f"⚔️ <b>{VERSION}</b>\n📅 <b>{TODAY.strftime('%d %b %Y')}</b> │ Ingestion Pipeline: {len(live_df)} live │ {len(upco_df)} upcoming\n" + "━" * 30 + "\n"
    for _, row in live_df.sort_values("FinalScore", ascending=False).iterrows():
        header += f"  {row['Verdict']} <b>{html_lib.escape(_tg_clean_symbol(str(row['Symbol'])))}</b> ({row['FinalScore']:.0f}) │ Sub: {row['SubscriptionTimes']:.1f}x │ GMP: {row['gmp_pct']:.1f}%\n"
    
    if not upco_df.empty:
        header += "\n¼ <b>Upcoming Issues Pipeline (Pre-Open)</b>\n"
        for _, row in upco_df.iterrows():
            hi_p = float(row['PriceBandUpper'])
            price_str = f"₹{row['PriceBandLower']:.0f}–{hi_p:.0f}" if hi_p > 0 else "Price TBD"
            close_str = str(row['CloseDate']) if row['CloseDate'] != (TODAY + timedelta(days=20)).strftime("%Y-%m-%d") else "Date TBD"
            header += f"  ¼ <b>{html_lib.escape(_tg_clean_symbol(str(row['Symbol'])))}</b> │ {price_str} │ Opens: {html_lib.escape(close_str)}\n"
        
    _tg_send_with_retry(header, token, chat_id)
    _jitter(1.5, 2.5)

    # 2. Detailed Asset Specification Notification Cards
    for _, row in live_df.sort_values("FinalScore", ascending=False).iterrows():
        sym = str(row["Symbol"]); a = allots[sym]; sh = shariahs[sym]
        esc_sym = html_lib.escape(_tg_clean_symbol(sym))
        
        msg = (
            f"{'🔥' if row['FinalScore'] >= 80 else '✅'} <b>{esc_sym}</b> [{html_lib.escape(str(row['Sector']))}]\n"
            f"  🏆 Score Matrix Rating: <b>{row['FinalScore']:.1f}/100</b> ➔ <b>{row['Verdict']}</b>\n\n"
            f"  📊 Subscription Multiplier: <b>{row['SubscriptionTimes']:.1f}x</b> │ Live GMP: <b>{row['gmp_pct']:.1f}%</b>\n"
            f"  💹 Price Band: ₹{row['PriceBandLower']:.0f}–₹{row['PriceBandUpper']:.0f} │ Lot: {row['LotSize']} │ Sizing: ₹{row['IssueSizeCr']:.0f}Cr\n"
            f"  📅 Closing Window: {html_lib.escape(str(row['CloseDate']))} ({row['DaysToClose']}d left)\n\n"
            f"  🎲 Expected P(Allocation): <b>{a.p_single_mc * 100:.3f}%</b> [95% CI: {a.ci_95[0]*100:.2f}%–{a.ci_95[1]*100:.2f}%]\n"
            f"  👥 Syndicate Target Pool: <b>{a.optimal_syndicate} Legal PAN Accounts</b>\n"
            f"  💰 Portfolio Sizing (Kelly): {a.kelly_pct}% │ Expected Transaction EV: ₹{a.ev_inr:,.0f}\n\n"
            f"  🕌 <b>{html_lib.escape(str(sh.tier))}</b> (Barakah Profile Index: {sh.barakah_index:.0f}/100)\n"
            f"  ⚖️ <i>{html_lib.escape(str(sh.qabda_mandate))}</i>\n"
            f"  🔗 Source Feed Mapping: <code>{html_lib.escape(str(row.get('Source','unknown')))}</code>"
        )
        if sh.deferred_issues: msg += f"\n  🚨 Warnings: " + " │ ".join([html_lib.escape(i) for i in sh.deferred_issues])
        
        _tg_send_with_retry(msg, token, chat_id)
        _jitter(0.5, 1.2)

# ═══════════════════════════════════════════════════════════
# MAIN ENGINE ORCHESTRATION PIPELINE
# ═══════════════════════════════════════════════════════════
def run():
    log.info(f"🚀  Initializing Quant Engine Workspace {VERSION} [{TODAY}]")
    init_db()

    df = fetch_unified_calendar()
    if df.empty:
        log.error("❌ Data execution layer returned empty payload matrices. Aborting.")
        return None

    df["IsUpcoming"] = df["IsUpcoming"].fillna(False).astype(bool)
    w = bayesian_weight_update(df)
    
    allots, shariahs, scores = {}, {}, []
    for _, row in df.iterrows():
        sym = str(row["Symbol"])
        allots[sym] = compute_allotment(row)
        shariahs[sym] = run_shariah(row)
        scores.append(master_score(row, allots[sym], shariahs[sym], w))

    df["FinalScore"]        = [s["FinalScore"] for s in scores]
    df["Verdict"]           = [s["Verdict"] for s in scores]
    df["p_single_mc"]       = [allots[s].p_single_mc for s in df["Symbol"]]
    df["optimal_syndicate"] = [allots[s].optimal_syndicate for s in df["Symbol"]]
    df["kelly_pct"]         = [allots[s].kelly_pct for s in df["Symbol"]]
    df["ev_inr"]            = [allots[s].ev_inr for s in df["Symbol"]]
    df["roi_pct"]           = [allots[s].roi_pct for s in df["Symbol"]]
    df["barakah"]           = [shariahs[s].barakah_index for s in df["Symbol"]]
    df["halal_tier"]        = [shariahs[s].tier for s in df["Symbol"]]
    df["najash_alert"]      = [shariahs[s].najash_alert for s in df["Symbol"]]

    # Persist and write metrics directly into SQLite local caching engine
    with sqlite3.connect(str(IPO_DB_PATH)) as con:
        for _, r in df.iterrows():
            sym = str(r["Symbol"])
            con.execute("""
                INSERT OR REPLACE INTO ipo_scans (
                    run_date, symbol, sector, final_score, verdict, is_upcoming, subscription_x, gmp_pct, issue_size_cr, price_upper, lot_size, close_date, days_to_close,
                    p_single_mc, ci_lo, ci_hi, optimal_syndicate, kelly_pct, ev_inr, roi_pct, barakah, halal_tier, najash_alert, source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (TODAY.strftime("%Y-%m-%d"), sym, r["Sector"], r["FinalScore"], r["Verdict"], int(r["IsUpcoming"]), r["SubscriptionTimes"], r["gmp_pct"], r["IssueSizeCr"],
                  r["PriceBandUpper"], int(r["LotSize"]), r["CloseDate"], int(r["DaysToClose"]), allots[sym].p_single_mc, allots[sym].ci_95[0], allots[sym].ci_95[1],
                  allots[sym].optimal_syndicate, allots[sym].kelly_pct, allots[sym].ev_inr, allots[sym].roi_pct, shariahs[sym].barakah_index, shariahs[sym].tier, int(shariahs[sym].najash_alert), str(r.get("Source", "unknown"))))

    JSON_EXPORT.parent.mkdir(parents=True, exist_ok=True)
    df.to_json(str(JSON_EXPORT), orient="records", indent=2)

    # Print summary console table layout cleanly
    ranked = df.sort_values(["IsUpcoming", "FinalScore"], ascending=[True, False])
    print(f"\nContractual Integrity Vetted Insights Sheet │ {TODAY}\n{'═'*105}")
    print(f"  {'Symbol':<32} {'Score':>5}  {'Verdict':<14}  {'Sub':>6}  {'GMP':>6}  {'Days':>4}  {'Synd':>4}  {'Status':<10}  Source")
    print(f"  {'─'*32} {'─'*5}  {'─'*14}  {'─'*6}  {'─'*6}  {'─'*4}  {'─'*4}  {'─'*10}  {'─'*18}")
    for _, row in ranked.iterrows():
        sym = str(row["Symbol"]); a = allots[sym]
        print(f"  {_tg_clean_symbol(sym):<32} {row['FinalScore']:>5.1f}  {row['Verdict']:<14}  {row['SubscriptionTimes']:>5.1f}×  {row['gmp_pct']:>5.1f}%  {row['DaysToClose']:>4}  {a.optimal_syndicate:>4}  {'UPCOMING' if row['IsUpcoming'] else 'LIVE':<10}  {str(row.get('Source',''))[:18]}")
    print(f"{'═'*105}\n")

    send_telegram_alerts(df, allots, shariahs)
    log.info(" Automated production continuous integration run completed successfully.")
    return df

if __name__ == "__main__":
    run()
