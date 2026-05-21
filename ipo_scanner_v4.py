#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║        IPO SNIPER v5.3 — STALE-DATA FIXED PRODUCTION EDITION               ║
║                                                                              ║
║  ROOT CAUSES FIXED vs v5.2:                                                  ║
║  1. URLs changed from DRHP archive → live subscription-status pages only    ║
║  2. Date guard added: DaysToClose < 0 rows dropped (closed IPOs removed)     ║
║  3. Fake GMP injection via np.random REMOVED — GMP=0.0 when not on page     ║
║  4. CloseDate now parsed from actual table data (multi-format strptime)      ║
║  5. Fallback CSV re-generated with today-relative dates every run             ║
║                                                                              ║
║  FETCH CHAIN (in order):                                                     ║
║    A. Chittorgarh live subscription pages (Playwright headless)              ║
║    B. Investorgain live GMP page                                             ║
║    C. NSE India API (ipo + emerge-ipo endpoints)                             ║
║    D. Fallback CSV (clearly labeled, today-relative dates)                   ║
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
from dataclasses import dataclass, field

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
# CONFIG
# ═══════════════════════════════════════════════════════════
IPO_DB_PATH      = Path("data/ipo_sniper_v5.db")
FALLBACK_CSV     = Path("data/ipo_fallback_v5.csv")   # NEW file, not the stale v3 one
JSON_EXPORT      = Path("data/ipo_latest_run.json")
VERSION          = "IPO-SNIPER-v5.3-STALE-DATA-FIXED"
MONTE_CARLO_RUNS = 50_000
KELLY_FRACTION   = 0.25
MAX_SYNDICATE    = 10
SEED             = 42
np.random.seed(SEED)

# ── LIVE SUBSCRIPTION URLS (FIX #1: not the DRHP archive) ─────────────────
# These pages show ONLY currently open / subscription-active IPOs.
CHITT_LIVE_URLS = {
    "Mainboard": "https://www.chittorgarh.com/report/ipo-subscription-status/10/",
    "SME":       "https://www.chittorgarh.com/report/sme-ipo-subscription-status/10/",
}
# Secondary: upcoming IPOs (open for application, not yet listed)
CHITT_UPCOMING_URLS = {
    "Mainboard": "https://www.chittorgarh.com/report/upcoming-ipo/6/",
    "SME":       "https://www.chittorgarh.com/report/upcoming-sme-ipo/",
}

NSE_ENDPOINTS = [
    ("https://www.nseindia.com/api/ipo",         "Mainboard"),
    ("https://www.nseindia.com/api/emerge-ipo",  "SME"),
]
NSE_WARMUP = [
    "https://www.nseindia.com",
    "https://www.nseindia.com/market-data/upcoming-issues-ipo",
]

BASE_WEIGHTS: Dict[str, float] = {
    "gmp": 0.22, "sub": 0.28, "sentiment": 0.18,
    "trend": 0.10, "size": 0.08, "halal": 0.14,
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s │ %(levelname)-8s │ %(message)s")
log = logging.getLogger("IPO-SNIPER-v5")

TODAY = datetime.today().date()

# ═══════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════
def _flt(v, default: float = 0.0) -> float:
    try:
        m = re.search(r"[\d.]+", str(v).replace(",", ""))
        return float(m.group()) if m else default
    except Exception:
        return default

def _int(v, default: int = 0) -> int:
    try:
        m = re.search(r"\d+", str(v).replace(",", ""))
        return int(m.group()) if m else default
    except Exception:
        return default

def _jitter(lo: float = 1.5, hi: float = 3.5):
    time.sleep(random.uniform(lo, hi))

def _parse_date(text: str) -> Optional[object]:
    """Parse a date string in any common Indian format. Returns date or None."""
    text = str(text).strip()
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%m-%Y", "%Y-%m-%d",
                "%b %d, %Y", "%d/%m/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None

def _parse_price_band(text: str) -> Tuple[float, float]:
    nums = re.findall(r"[\d.]+", str(text).replace(",", ""))
    if len(nums) >= 2:
        return float(nums[0]), float(nums[-1])
    if len(nums) == 1:
        v = float(nums[0])
        return round(v * 0.97, 2), v
    return 95.0, 100.0

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8",
    "Cache-Control": "no-cache",
}

def _make_session(referer: str = "https://www.google.com/") -> requests.Session:
    s = requests.Session()
    s.headers.update({**BROWSER_HEADERS, "Referer": referer})
    return s

# ═══════════════════════════════════════════════════════════
# TABLE PARSER  (FIX #3 + #4: no fake GMP, real dates)
# ═══════════════════════════════════════════════════════════
SKIP_SYMBOLS = {
    "company", "name", "issuer", "no records found",
    "compare", "click here", "", "open", "closed", "upcoming",
}

def _parse_html_table(table, ipo_type: str, source_tag: str) -> pd.DataFrame:
    """
    Parse a BeautifulSoup <table> into a normalised DataFrame.

    FIX #3: GMP is set to 0.0 when not present on the page.
             np.random is NEVER used to fill GMP.
    FIX #4: CloseDate is parsed from the actual table cell using _parse_date().
             A default of today+10 is used only when the cell is truly empty.
    """
    sector = "Mainboard" if "main" in ipo_type.lower() else "SME"
    rows = table.find_all("tr")
    if len(rows) < 2:
        return pd.DataFrame()

    hdr = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
    col: Dict[str, int] = {}
    for i, h in enumerate(hdr):
        if any(k in h for k in ("company", "issuer", "name", "ipo")):
            col.setdefault("sym", i)
        elif any(k in h for k in ("size", "cr", "amt")):
            col.setdefault("size", i)
        elif any(k in h for k in ("price", "band", "rate")):
            col.setdefault("price", i)
        elif any(k in h for k in ("close", "end date", "closing")):
            col.setdefault("close", i)
        elif any(k in h for k in ("open", "start", "opening")):
            col.setdefault("open", i)
        elif any(k in h for k in ("lot", "qty", "shares")):
            col.setdefault("lot", i)
        elif "gmp" in h or "premium" in h:
            col.setdefault("gmp", i)
        elif any(k in h for k in ("sub", "times", "overall", "x")):
            col.setdefault("sub", i)

    if "sym" not in col:
        col["sym"] = 0

    records = []
    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        def _c(key, default=""):
            idx = col.get(key)
            return cells[idx].get_text(strip=True) if idx is not None and idx < len(cells) else default

        lnk    = cells[col["sym"]].find("a")
        symbol = (lnk.get_text(strip=True) if lnk else cells[col["sym"]].get_text(strip=True)).strip()
        symbol = re.sub(r"\s+", " ", symbol)
        if not symbol or symbol.lower() in SKIP_SYMBOLS or len(symbol) < 2:
            continue

        # Issue size
        size = _flt(_c("size", "50"), 50.0)
        if size > 50_000:
            size /= 1e7  # raw rupees → crores

        # Price band
        lo, hi = _parse_price_band(_c("price", "100"))

        # Lot size
        lot = _int(_c("lot", "")) or (1000 if sector == "SME" else 50)

        # ── FIX #4: Parse real close date ─────────────────────────────────
        close_raw = _c("close", "")
        close_dt  = _parse_date(close_raw) if close_raw else None
        if close_dt is None:
            close_dt = TODAY + timedelta(days=10)   # fallback only when cell truly empty

        # ── FIX #3: GMP — 0.0 when not on page, NEVER random ─────────────
        gmp_raw = _c("gmp", "")
        if gmp_raw:
            gmp_v = _flt(gmp_raw, 0.0)
            gmp   = gmp_v / 100 if gmp_v > 1 else gmp_v   # handle % vs fraction
        else:
            gmp   = 0.0  # genuinely unknown — do NOT invent a value

        # Subscription (0.0 means not yet started, which is valid)
        sub = _flt(_c("sub", "0"), 0.0)

        records.append({
            "Symbol":           symbol,
            "Sector":           sector,
            "IssueSizeCr":      round(size, 2),
            "PriceBandLower":   lo,
            "PriceBandUpper":   hi,
            "LotSize":          lot,
            "GMP":              round(gmp, 4),
            "gmp_pct":          round(gmp * 100, 2),
            "SubscriptionTimes":round(sub, 2),
            "CloseDate":        close_dt.strftime("%Y-%m-%d"),
            "DaysToClose":      (close_dt - TODAY).days,   # can be negative → filtered later
            "Source":           source_tag,
        })

    return pd.DataFrame(records)

# ═══════════════════════════════════════════════════════════
# SOURCE A — CHITTORGARH (Playwright + HTTP fallback)
# ═══════════════════════════════════════════════════════════

def _fetch_chitt_playwright(url: str, ipo_type: str, source_tag: str) -> pd.DataFrame:
    if not PLAYWRIGHT_OK:
        return pd.DataFrame()
    log.info(f"  PW [{ipo_type}] → {url}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"]
            )
            ctx = browser.new_context(
                user_agent=BROWSER_HEADERS["User-Agent"],
                extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()

            # Intercept AJAX data payloads
            intercepted: List[dict] = []
            def _on_resp(resp):
                if resp.status == 200 and "chittorgarh" in resp.url:
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct:
                        try:
                            body = resp.json()
                            rows = body.get("data", body.get("aaData", []))
                            if rows:
                                intercepted.extend(rows)
                                log.info(f"  PW AJAX: {len(rows)} rows intercepted")
                        except Exception:
                            pass
            page.on("response", _on_resp)

            page.goto(url, wait_until="networkidle", timeout=55_000)
            try:
                page.wait_for_selector("table tbody tr td:not(.dataTables_empty)", timeout=15_000)
            except PWTimeout:
                pass

            if intercepted:
                # Parse AJAX rows (list-of-lists or list-of-dicts)
                sector = "Mainboard" if "main" in ipo_type.lower() else "SME"
                records = []
                for row_data in intercepted[:60]:
                    cells = row_data if isinstance(row_data, list) else list(row_data.values())
                    clean = [BeautifulSoup(str(c), "html.parser").get_text(strip=True) for c in cells]
                    if not clean or len(clean[0]) < 2:
                        continue
                    symbol = clean[0]
                    if symbol.lower() in SKIP_SYMBOLS:
                        continue
                    size   = _flt(clean[1] if len(clean) > 1 else "50", 50.0)
                    lo, hi = _parse_price_band(clean[2] if len(clean) > 2 else "100")
                    lot    = _int(clean[3] if len(clean) > 3 else "") or (1000 if sector == "SME" else 50)
                    # FIX #4: parse real close date from AJAX cell
                    close_dt = _parse_date(clean[4] if len(clean) > 4 else "") or (TODAY + timedelta(days=10))
                    sub    = _flt(clean[5] if len(clean) > 5 else "0", 0.0)
                    # FIX #3: GMP only if cell exists and non-empty
                    gmp_raw = clean[6] if len(clean) > 6 else ""
                    gmp_v   = _flt(gmp_raw, 0.0) if gmp_raw else 0.0
                    gmp     = gmp_v / 100 if gmp_v > 1 else gmp_v

                    records.append({
                        "Symbol": symbol, "Sector": sector,
                        "IssueSizeCr": round(size, 2),
                        "PriceBandLower": lo, "PriceBandUpper": hi,
                        "LotSize": lot,
                        "GMP": round(gmp, 4), "gmp_pct": round(gmp * 100, 2),
                        "SubscriptionTimes": round(sub, 2),
                        "CloseDate": close_dt.strftime("%Y-%m-%d"),
                        "DaysToClose": (close_dt - TODAY).days,
                        "Source": source_tag + "_ajax",
                    })
                browser.close()
                return pd.DataFrame(records)

            # Fallback: parse rendered HTML
            soup = BeautifulSoup(page.content(), "html.parser")
            browser.close()
            for tbl in soup.find_all("table"):
                if len(tbl.find_all("tr")) > 3:
                    df = _parse_html_table(tbl, ipo_type, source_tag + "_html")
                    if not df.empty:
                        return df
    except Exception as exc:
        log.warning(f"  PW error [{ipo_type}]: {exc}")
    return pd.DataFrame()

def _fetch_chitt_http(url: str, ipo_type: str, source_tag: str) -> pd.DataFrame:
    sess = _make_session("https://www.chittorgarh.com/")
    try:
        sess.get("https://www.chittorgarh.com/", timeout=12)
        _jitter(1.5, 3.0)
        resp = sess.get(url, timeout=25)
        log.info(f"  HTTP [{ipo_type}] → {resp.status_code} ({url})")
        if resp.status_code != 200:
            return pd.DataFrame()
        deny = resp.headers.get("x-deny-reason", "")
        if deny:
            log.warning(f"  Blocked: {deny}")
            return pd.DataFrame()
        soup = BeautifulSoup(resp.text, "html.parser")
        for sel in ["table.table-striped", "table.table-bordered",
                    ".table-responsive table", "table"]:
            for tbl in soup.select(sel):
                if len(tbl.find_all("tr")) > 3:
                    df = _parse_html_table(tbl, ipo_type, source_tag + "_http")
                    if not df.empty:
                        return df
    except Exception as exc:
        log.warning(f"  HTTP error [{ipo_type}]: {exc}")
    return pd.DataFrame()

def fetch_source_a_chittorgarh() -> pd.DataFrame:
    """SOURCE A: live subscription + upcoming pages on Chittorgarh."""
    log.info("━━ SOURCE A: Chittorgarh live subscription pages ━━")
    all_frames: List[pd.DataFrame] = []

    # Priority: live subscription pages (only open IPOs appear here)
    for itype, url in CHITT_LIVE_URLS.items():
        tag = f"chitt_live_{itype.lower()}"
        df  = _fetch_chitt_playwright(url, itype, tag)
        if df.empty:
            df = _fetch_chitt_http(url, itype, tag)
        if not df.empty:
            log.info(f"  ✅ Live sub [{itype}]: {len(df)} rows")
            all_frames.append(df)
        _jitter(2.0, 4.0)

    # Secondary: upcoming (may have 0 subscription if not yet open)
    for itype, url in CHITT_UPCOMING_URLS.items():
        tag = f"chitt_upcoming_{itype.lower()}"
        df  = _fetch_chitt_playwright(url, itype, tag)
        if df.empty:
            df = _fetch_chitt_http(url, itype, tag)
        if not df.empty:
            log.info(f"  ✅ Upcoming [{itype}]: {len(df)} rows")
            all_frames.append(df)
        _jitter(1.5, 3.0)

    if all_frames:
        combined = pd.concat(all_frames, ignore_index=True)
        log.info(f"  ✅ SOURCE A total before filter: {len(combined)} raw rows")
        return combined
    log.warning("  ⚠️  SOURCE A: no data")
    return pd.DataFrame()

# ═══════════════════════════════════════════════════════════
# SOURCE B — INVESTORGAIN GMP
# ═══════════════════════════════════════════════════════════

def fetch_source_b_investorgain() -> pd.DataFrame:
    """SOURCE B: Investorgain live GMP page."""
    log.info("━━ SOURCE B: Investorgain GMP ━━")
    url  = "https://www.investorgain.com/report/live-ipo-gmp/331/"
    sess = _make_session("https://www.investorgain.com/")
    try:
        resp = sess.get(url, timeout=25)
        log.info(f"  Investorgain → HTTP {resp.status_code}")
        if resp.status_code != 200:
            return pd.DataFrame()
        deny = resp.headers.get("x-deny-reason", "")
        if deny:
            log.warning(f"  Blocked: {deny}")
            return pd.DataFrame()

        soup  = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table")
        if not table:
            return pd.DataFrame()

        rows = table.find_all("tr")
        if len(rows) < 2:
            return pd.DataFrame()

        hdr = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        col: Dict[str, int] = {}
        for i, h in enumerate(hdr):
            if any(k in h for k in ("ipo", "company", "name")):  col.setdefault("sym",   i)
            elif "gmp" in h:                                       col.setdefault("gmp",   i)
            elif any(k in h for k in ("sub", "times")):            col.setdefault("sub",   i)
            elif "price" in h:                                     col.setdefault("price", i)
            elif any(k in h for k in ("close", "date", "end")):    col.setdefault("close", i)
            elif any(k in h for k in ("size", "cr")):              col.setdefault("size",  i)
            elif "lot" in h:                                        col.setdefault("lot",   i)
        col.setdefault("sym", 0)

        records = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells or len(cells) < 2:
                continue

            def _c(key, default=""):
                idx = col.get(key)
                return cells[idx].get_text(strip=True) if idx is not None and idx < len(cells) else default

            symbol = re.sub(r"<[^>]+>", "", cells[col["sym"]].get_text(strip=True)).strip()
            if not symbol or len(symbol) < 3 or symbol.lower() in SKIP_SYMBOLS:
                continue

            # FIX #3: real GMP only, no imputation
            gmp_raw = _c("gmp", "")
            if gmp_raw:
                gmp_v = _flt(gmp_raw, 0.0)
                gmp   = gmp_v / 100 if gmp_v > 1 else gmp_v
            else:
                gmp = 0.0

            lo, hi   = _parse_price_band(_c("price", "100"))
            sub      = _flt(_c("sub", "0"), 0.0)
            size     = _flt(_c("size", "50"), 50.0)
            lot      = _int(_c("lot", "")) or 1000

            # FIX #4: real close date
            close_dt = _parse_date(_c("close", "")) or (TODAY + timedelta(days=7))

            records.append({
                "Symbol":            symbol,
                "Sector":            "Mainboard" if hi > 250 or lot < 200 else "SME",
                "IssueSizeCr":       round(size, 2),
                "PriceBandLower":    lo,
                "PriceBandUpper":    hi,
                "LotSize":           lot,
                "GMP":               round(gmp, 4),
                "gmp_pct":           round(gmp * 100, 2),
                "SubscriptionTimes": round(sub, 2),
                "CloseDate":         close_dt.strftime("%Y-%m-%d"),
                "DaysToClose":       (close_dt - TODAY).days,
                "Source":            "investorgain_gmp",
            })

        df = pd.DataFrame(records)
        log.info(f"  ✅ SOURCE B: {len(df)} rows" if not df.empty else "  ⚠️  SOURCE B: no data")
        return df

    except Exception as exc:
        log.warning(f"  Investorgain error: {exc}")
        return pd.DataFrame()

# ═══════════════════════════════════════════════════════════
# SOURCE C — NSE API
# ═══════════════════════════════════════════════════════════

def fetch_source_c_nse() -> pd.DataFrame:
    """SOURCE C: NSE India official JSON API."""
    log.info("━━ SOURCE C: NSE India API ━━")
    sess = _make_session("https://www.nseindia.com/")
    sess.headers.update({
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/market-data/upcoming-issues-ipo",
    })
    for url in NSE_WARMUP:
        try:
            sess.get(url, timeout=12)
        except Exception:
            pass
        _jitter(1.5, 2.5)

    records: List[dict] = []
    seen: set = set()

    for endpoint, sector in NSE_ENDPOINTS:
        try:
            resp = sess.get(endpoint, timeout=20)
            log.info(f"  NSE [{sector}] → {resp.status_code}")
            if resp.status_code != 200 or len(resp.content) < 30:
                continue
            deny = resp.headers.get("x-deny-reason", "")
            if deny:
                continue
            data  = resp.json()
            items = data if isinstance(data, list) else data.get("data", [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                sym = str(item.get("symbol",
                          item.get("companyName",
                          item.get("issuerName", "")))).strip()
                if not sym or len(sym) < 2 or sym in seen:
                    continue

                lo, hi = _parse_price_band(str(item.get("priceBand",
                                              item.get("issuePrice", "100"))))
                size_raw = item.get("issueSize", item.get("totalIssueSizeCr", 50.0))
                size     = _flt(size_raw, 50.0)
                if size > 50_000:
                    size /= 1e7
                lot = _int(item.get("lotSize", item.get("minBidQuantity", 0))) or (1000 if sector == "SME" else 50)
                sub_raw = str(item.get("subscriptionTimes", item.get("subscriptionStatus", "0")))
                sub     = _flt(re.search(r"[\d.]+", sub_raw).group() if re.search(r"[\d.]+", sub_raw) else "0")
                close_dt = _parse_date(str(item.get("closeDate",
                                           item.get("biddingEndDate", "")))) or (TODAY + timedelta(days=10))
                seen.add(sym)
                records.append({
                    "Symbol": sym, "Sector": sector,
                    "IssueSizeCr": round(size, 2),
                    "PriceBandLower": lo, "PriceBandUpper": hi,
                    "LotSize": lot,
                    "GMP": 0.0, "gmp_pct": 0.0,   # NSE doesn't provide GMP
                    "SubscriptionTimes": round(sub, 2),
                    "CloseDate": close_dt.strftime("%Y-%m-%d"),
                    "DaysToClose": (close_dt - TODAY).days,
                    "Source": "nse_api",
                })
            _jitter(1.5, 3.0)
        except Exception as exc:
            log.warning(f"  NSE endpoint error: {exc}")

    df = pd.DataFrame(records)
    log.info(f"  ✅ SOURCE C: {len(df)} rows" if not df.empty else "  ⚠️  SOURCE C: no data")
    return df

# ═══════════════════════════════════════════════════════════
# FALLBACK CSV  (FIX #5: fresh dates always, clearly labeled)
# ═══════════════════════════════════════════════════════════

def _rebuild_fallback_csv() -> pd.DataFrame:
    """
    FIX #5: Fallback CSV is rebuilt with today-relative dates on every run
    so it never returns genuinely stale data. Source is clearly labeled
    'FALLBACK_SEED' so the caller can warn the user.
    Note: these are PLACEHOLDER companies — real data is always preferred.
    """
    FALLBACK_CSV.parent.mkdir(parents=True, exist_ok=True)
    seed = [
        {"Symbol": "Placeholder IPO Alpha",  "IssueSizeCr": 70.0,  "PriceBandLower": 140, "PriceBandUpper": 148, "LotSize": 1000, "GMP": 0.0, "SubscriptionTimes": 0.0, "Sector": "SME",       "CloseDate": (TODAY + timedelta(3)).strftime("%Y-%m-%d")},
        {"Symbol": "Placeholder IPO Beta",   "IssueSizeCr": 200.0, "PriceBandLower": 300, "PriceBandUpper": 320, "LotSize": 50,   "GMP": 0.0, "SubscriptionTimes": 0.0, "Sector": "Mainboard", "CloseDate": (TODAY + timedelta(5)).strftime("%Y-%m-%d")},
        {"Symbol": "Placeholder IPO Gamma",  "IssueSizeCr": 45.0,  "PriceBandLower": 80,  "PriceBandUpper": 85,  "LotSize": 1200, "GMP": 0.0, "SubscriptionTimes": 0.0, "Sector": "SME",       "CloseDate": (TODAY + timedelta(7)).strftime("%Y-%m-%d")},
    ]
    df = pd.DataFrame(seed)
    df["Source"] = "FALLBACK_SEED_PLACEHOLDER"
    df.to_csv(FALLBACK_CSV, index=False)
    log.warning("⚠️  Fallback CSV rebuilt with placeholder data — live fetch failed entirely.")
    return df

# ═══════════════════════════════════════════════════════════
# DATA VALIDATION + ENRICHMENT  (FIX #2: date guard)
# ═══════════════════════════════════════════════════════════

REQUIRED_DEFAULTS = {
    "Symbol": "UNKNOWN", "Sector": "SME", "IssueSizeCr": 50.0,
    "PriceBandLower": 95.0, "PriceBandUpper": 100.0, "LotSize": 1000,
    "GMP": 0.0, "gmp_pct": 0.0, "SubscriptionTimes": 0.0,
    "CloseDate": (TODAY + timedelta(days=7)).strftime("%Y-%m-%d"),
    "DaysToClose": 7, "Source": "unknown",
}

def _validate_row(row: pd.Series) -> Tuple[bool, str]:
    """Return (is_valid, rejection_reason)."""
    sym = str(row.get("Symbol", "")).strip()
    if not sym or len(sym) < 2 or sym.lower() in ("unknown", "nan", "none", ""):
        return False, "invalid_symbol"
    price = float(row.get("PriceBandUpper", 0))
    if price <= 0 or price > 200_000:
        return False, f"price_out_of_range:{price}"
    lot = int(row.get("LotSize", 0))
    if lot <= 0 or lot > 200_000:
        return False, f"lot_out_of_range:{lot}"
    # FIX #2: STRICT DATE GUARD — drop anything where close date is in the past
    days = int(row.get("DaysToClose", 0))
    if days < 0:
        return False, f"ipo_closed:{row.get('CloseDate','?')} ({days}d ago)"
    return True, ""

def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Fill missing columns, recompute derived fields, validate every row."""
    for col, val in REQUIRED_DEFAULTS.items():
        if col not in df.columns:
            df[col] = val

    # Coerce numerics
    for c in ("IssueSizeCr", "PriceBandLower", "PriceBandUpper", "LotSize",
              "GMP", "gmp_pct", "SubscriptionTimes"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(REQUIRED_DEFAULTS.get(c, 0))

    # Source case fix
    if "source" in df.columns and "Source" not in df.columns:
        df["Source"] = df["source"]

    # Recompute gmp_pct from GMP (never random)
    df["gmp_pct"] = df["GMP"].apply(lambda g: round(float(g) * 100, 2))

    # Recompute DaysToClose from CloseDate
    def _days(x):
        d = _parse_date(str(x))
        return (d - TODAY).days if d else -999
    df["DaysToClose"] = df["CloseDate"].apply(_days)

    # Validate every row
    valid_rows = []
    dropped = 0
    for _, row in df.iterrows():
        ok, reason = _validate_row(row)
        if ok:
            valid_rows.append(row)
        else:
            dropped += 1
            log.debug(f"  Dropped [{row.get('Symbol','?')}]: {reason}")

    if dropped:
        log.info(f"  🗑  Dropped {dropped} invalid/closed rows after validation")

    if not valid_rows:
        return pd.DataFrame()

    return pd.DataFrame(valid_rows).reset_index(drop=True)

# ═══════════════════════════════════════════════════════════
# MASTER FETCH ORCHESTRATOR
# ═══════════════════════════════════════════════════════════

def fetch_unified_calendar() -> pd.DataFrame:
    """
    Waterfall across all sources. Merges by Symbol, keeps highest sub record.
    Best GMP from any source is merged back in.
    FIX #2 applied in _enrich(): DaysToClose < 0 → dropped.
    """
    frames: List[pd.DataFrame] = []

    a = fetch_source_a_chittorgarh()
    if not a.empty:
        frames.append(a)

    b = fetch_source_b_investorgain()
    if not b.empty:
        frames.append(b)

    c = fetch_source_c_nse()
    if not c.empty:
        frames.append(c)

    if frames:
        raw      = pd.concat(frames, ignore_index=True)
        enriched = _enrich(raw)

        if enriched.empty:
            log.warning("All live rows were dropped by validation (all closed?)")
        else:
            # Merge best GMP per symbol back in
            best_gmp = (enriched[enriched["gmp_pct"] > 0]
                        .sort_values("gmp_pct", ascending=False)
                        .drop_duplicates(subset="Symbol", keep="first")[["Symbol", "GMP", "gmp_pct"]])

            deduped = (enriched.sort_values("SubscriptionTimes", ascending=False)
                               .drop_duplicates(subset="Symbol", keep="first")
                               .reset_index(drop=True))

            if not best_gmp.empty:
                deduped = (deduped.drop(columns=["GMP", "gmp_pct"], errors="ignore")
                                  .merge(best_gmp, on="Symbol", how="left"))
                deduped["GMP"]     = deduped["GMP"].fillna(0.0)
                deduped["gmp_pct"] = deduped["gmp_pct"].fillna(0.0)

            log.info(f"✅ LIVE DATA: {len(deduped)} unique open IPOs")
            return deduped

    log.warning("⚠️  ALL LIVE SOURCES FAILED — using placeholder fallback")
    return _enrich(_rebuild_fallback_csv())

# ═══════════════════════════════════════════════════════════
# BAYESIAN WEIGHT UPDATE
# ═══════════════════════════════════════════════════════════

def bayesian_weight_update(df: pd.DataFrame) -> Dict[str, float]:
    w = BASE_WEIGHTS.copy()
    if df.empty:
        return w
    avg_sub = df["SubscriptionTimes"].mean()
    if avg_sub > 80:
        w["sub"]  = min(0.38, w["sub"]  + 0.10)
        w["gmp"]  = max(0.12, w["gmp"]  - 0.05)
        w["halal"]= max(0.09, w["halal"]- 0.05)
        log.info(f"📈 Bayesian: HYPER-BULL (avg sub={avg_sub:.1f}×)")
    elif avg_sub < 15:
        w["gmp"]  = min(0.32, w["gmp"]  + 0.10)
        w["sub"]  = max(0.18, w["sub"]  - 0.10)
        w["halal"]= min(0.19, w["halal"]+ 0.05)
        log.info(f"📉 Bayesian: TEPID (avg sub={avg_sub:.1f}×)")
    else:
        log.info(f"➡️  Bayesian: NEUTRAL (avg sub={avg_sub:.1f}×)")
    total = sum(w.values())
    return {k: round(v / total, 6) for k, v in w.items()}

# ═══════════════════════════════════════════════════════════
# QUANT ENGINE
# ═══════════════════════════════════════════════════════════

@dataclass
class AllotmentProfile:
    symbol: str
    p_single_mc: float
    syndicate_matrix: Dict[int, float]
    optimal_syndicate: int
    kelly_pct: float
    ev_inr: float
    roi_pct: float
    ci_95: Tuple[float, float]

@dataclass
class ShariahVerdict:
    symbol: str
    tier: str
    barakah_index: float
    najash_alert: bool
    qabda_mandate: str
    deferred_issues: List[str]

def monte_carlo_allotment(sub, lot, size_cr, price, n=MONTE_CARLO_RUNS):
    if sub <= 0 or lot <= 0 or price <= 0 or size_cr <= 0:
        return 0.0, 0.0, 0.0
    retail = size_cr * 1e7 * 0.35
    avail  = max(1, int(retail / (lot * price)))
    total  = max(avail + 1, int(avail * sub))
    p_true = avail / total
    hits   = np.random.binomial(1, p_true, n)
    p_hat  = hits.mean()
    z      = 1.96
    denom  = 1 + z**2 / n
    center = (p_hat + z**2 / (2 * n)) / denom
    spread = (z * math.sqrt(p_hat * (1 - p_hat) / n + z**2 / (4 * n**2))) / denom
    return round(p_hat, 6), max(0.0, round(center - spread, 6)), min(1.0, round(center + spread, 6))

def compute_allotment(row: pd.Series) -> AllotmentProfile:
    sub   = max(0.1, float(row["SubscriptionTimes"]))
    price = float(row["PriceBandUpper"])
    lot   = int(row["LotSize"])
    size  = float(row["IssueSizeCr"])
    gmp   = float(row["GMP"])

    p_mc, ci_lo, ci_hi = monte_carlo_allotment(sub, lot, size, price)
    matrix  = {k: round(1 - (1 - p_mc) ** k, 6) for k in range(1, MAX_SYNDICATE + 1)}
    gain    = gmp * price * lot
    b_odds  = gain / max(1.0, 1500.0)
    cost    = lot * price

    best_k, best_ev = 1, -float("inf")
    for k, p_win in matrix.items():
        ev = p_win * gain - k * (cost + 500.0)
        if ev > best_ev:
            best_ev, best_k = ev, k

    p_opt     = matrix[best_k]
    f_star    = (b_odds * p_opt - (1 - p_opt)) / max(0.01, b_odds)
    kelly_pct = round(max(0.0, KELLY_FRACTION * f_star) * 100, 2)
    ev_inr    = round(p_opt * gain, 2)
    roi_pct   = round((ev_inr / max(1.0, cost * best_k)) * 100, 4)

    return AllotmentProfile(
        symbol=str(row["Symbol"]), p_single_mc=p_mc,
        syndicate_matrix=matrix, optimal_syndicate=best_k,
        kelly_pct=kelly_pct, ev_inr=ev_inr, roi_pct=roi_pct,
        ci_95=(ci_lo, ci_hi),
    )

def run_shariah(row: pd.Series) -> ShariahVerdict:
    gmp    = float(row["GMP"])
    sub    = float(row["SubscriptionTimes"])
    size   = float(row["IssueSizeCr"])
    sector = str(row["Sector"])
    sym    = str(row["Symbol"])

    barakah = 100.0
    issues: List[str] = []

    najash = gmp > 0.40 and sub > 80
    if najash:
        barakah -= 25
        issues.append("Najash: GMP>40% + Sub>80× (deceptive pump signal)")
    if size < 20:
        barakah -= 15
        issues.append("Microcap Liquidity Hazard (<₹20 Cr)")
    if sector == "SME" and sub > 200:
        barakah -= 10
        issues.append("SME Hyper-Pump Risk (Sub>200×)")

    tier   = "TIER_1_SHARIAH_COMPLIANT" if barakah >= 80 else "TIER_2_CONDITIONAL"
    qabda  = ("QABDA: Hold until T+2 Demat settlement before any resale. "
              "Listing-day flips = Gharar (OIC Fiqh Res. 3/3/86).")
    return ShariahVerdict(sym, tier, max(0.0, barakah), najash, qabda, issues)

def master_score(row, allot, shariah, w) -> Dict:
    days   = max(0, int(row["DaysToClose"]))
    tf     = 1.0 if days >= 7 else (0.5 + 0.5 * days / 7)
    gmp    = float(row["GMP"])
    sub    = float(row["SubscriptionTimes"])
    size   = float(row["IssueSizeCr"])

    s_gmp  = min(100.0, gmp * 200)
    s_sub  = min(100.0, (sub / 100.0) * 100) * tf
    s_sent = 40.0 + (20.0 if sub > 50 else 10.0 if sub > 25 else 0.0) + (20.0 if gmp > 0.40 else 10.0 if gmp > 0.20 else 0.0)
    s_trd  = 50.0
    s_size = 100 if size <= 20 else 80 if size <= 50 else 50 if size <= 100 else 20
    s_hal  = shariah.barakah_index

    raw    = (s_gmp * w["gmp"] + s_sub * w["sub"] + s_sent * w["sentiment"] +
              s_trd * w["trend"] + s_size * w["size"] + s_hal * w["halal"])
    final  = min(100.0, max(0.0, round(raw, 1)))
    verdict = ("🔥 PEARL" if final >= 80 else "✅ STRONG BUY" if final >= 70
               else "📈 MODERATE" if final >= 60 else "❌ SKIP")
    return {"FinalScore": final, "Verdict": verdict}

# ═══════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════

def init_db():
    IPO_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(IPO_DB_PATH)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ipo_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT, symbol TEXT, sector TEXT,
                final_score REAL, verdict TEXT,
                subscription_x REAL, gmp_pct REAL,
                issue_size_cr REAL, price_upper REAL, lot_size INTEGER,
                close_date TEXT, days_to_close INTEGER,
                p_single_mc REAL, ci_lo REAL, ci_hi REAL,
                optimal_syndicate INTEGER, kelly_pct REAL,
                ev_inr REAL, roi_pct REAL,
                barakah REAL, halal_tier TEXT, najash_alert INTEGER,
                source TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_date, symbol)
            )
        """)
    log.info("🗄  DB ready.")

def persist_db(df, allots, shariahs):
    date_label = TODAY.strftime("%Y-%m-%d")
    with sqlite3.connect(str(IPO_DB_PATH)) as con:
        for _, r in df.iterrows():
            sym = str(r["Symbol"])
            a   = allots[sym]
            sh  = shariahs[sym]
            con.execute("""
                INSERT OR REPLACE INTO ipo_scans (
                    run_date, symbol, sector, final_score, verdict,
                    subscription_x, gmp_pct, issue_size_cr, price_upper, lot_size,
                    close_date, days_to_close,
                    p_single_mc, ci_lo, ci_hi, optimal_syndicate,
                    kelly_pct, ev_inr, roi_pct,
                    barakah, halal_tier, najash_alert, source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                date_label, sym, r["Sector"], r["FinalScore"], r["Verdict"],
                r["SubscriptionTimes"], r["gmp_pct"], r["IssueSizeCr"],
                r["PriceBandUpper"], int(r["LotSize"]),
                r["CloseDate"], int(r["DaysToClose"]),
                a.p_single_mc, a.ci_95[0], a.ci_95[1], a.optimal_syndicate,
                a.kelly_pct, a.ev_inr, a.roi_pct,
                sh.barakah_index, sh.tier, int(sh.najash_alert),
                str(r.get("Source", "unknown")),
            ))
    log.info(f"🗄  Persisted {len(df)} records.")

# ═══════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════

def _tg(text: str, token: str, chat_id: str):
    text = text[:4096]
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=12,
        )
        if r.status_code != 200:
            log.warning(f"  Telegram {r.status_code}: {r.text[:120]}")
    except Exception as exc:
        log.error(f"  Telegram error: {exc}")

def send_telegram_alerts(df: pd.DataFrame, allots: dict, shariahs: dict):
    token   = os.getenv("TELEGRAM_TOKEN",   "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    console = not (token and chat_id)
    if console:
        log.warning("TELEGRAM_TOKEN/CHAT_ID not set — printing to console.")

    date_str = TODAY.strftime("%d %b %Y")
    has_fallback = any("FALLBACK" in str(r.get("Source","")) for _, r in df.iterrows())
    header = (
        f"⚔️ <b>{VERSION}</b>\n"
        f"📅 <b>{date_str}</b>  |  {len(df)} open IPOs\n"
    )
    if has_fallback:
        header += "⚠️ <i>Placeholder data shown — live fetch failed. Run again later.</i>\n"
    header += "━" * 38 + "\n"

    ranked = df.sort_values("FinalScore", ascending=False)
    for _, row in ranked.iterrows():
        header += (f"  {row['Verdict']} <b>{html_lib.escape(str(row['Symbol']))}</b>"
                   f" ({row['FinalScore']:.0f})  "
                   f"{row['SubscriptionTimes']:.1f}×  GMP {row['gmp_pct']:.1f}%\n")

    if console:
        print(f"\n[TELEGRAM]\n{header}")
    else:
        _tg(header, token, chat_id)
        _jitter(0.5, 1.0)

    for _, row in ranked.iterrows():
        sym = str(row["Symbol"])
        a   = allots[sym]
        sh  = shariahs[sym]
        score = row["FinalScore"]
        em  = "🔥" if score >= 80 else "✅" if score >= 70 else "📈" if score >= 60 else "❌"
        src = str(row.get("Source","live"))
        is_fallback = "FALLBACK" in src.upper()

        msg = (
            f"{em} <b>{html_lib.escape(sym)}</b>"
            + (" [⚠️PLACEHOLDER]" if is_fallback else f" [{row['Sector']}]") + "\n"
            f"   🏆 Score: <b>{score:.1f}/100</b>  {row['Verdict']}\n"
            f"\n"
            f"   📊 Sub: <b>{row['SubscriptionTimes']:.1f}×</b>"
            f"  |  GMP: <b>{row['gmp_pct']:.1f}%</b>"
            + ("  ⚠️<i>est.</i>" if row["gmp_pct"] == 0 else "") + "\n"
            f"   💹 ₹{row['PriceBandLower']:.0f}–₹{row['PriceBandUpper']:.0f}"
            f"  Lot: {row['LotSize']}"
            f"  Size: ₹{row['IssueSizeCr']:.0f}Cr\n"
            f"   📅 Closes: {row['CloseDate']}  ({row['DaysToClose']}d left)\n"
            f"\n"
            f"   🎲 P(Allot): <b>{a.p_single_mc * 100:.3f}%</b>"
            f"  [95% CI: {a.ci_95[0]*100:.2f}–{a.ci_95[1]*100:.2f}%]\n"
            f"   👥 Optimal Syndicate: <b>{a.optimal_syndicate} PANs</b>\n"
            f"   💰 Kelly: {a.kelly_pct:.1f}%"
            f"  EV: ₹{a.ev_inr:,.0f}"
            f"  ROI: {a.roi_pct:.2f}%\n"
            f"\n"
            f"   🕌 <b>{sh.tier}</b>  (Barakah: {sh.barakah_index:.0f}/100)\n"
        )
        if sh.deferred_issues:
            msg += "   🚨 " + " | ".join([html_lib.escape(i) for i in sh.deferred_issues]) + "\n"
        msg += f"   ⚖️ {html_lib.escape(sh.qabda_mandate)}\n"
        msg += f"   🔗 Source: {src}"

        if console:
            print(f"\n[TELEGRAM]\n{msg}\n{'─'*60}")
        else:
            _tg(msg, token, chat_id)
            _jitter(0.3, 0.8)

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def run():
    log.info(f"🚀  {VERSION}  [{TODAY}]")
    init_db()

    df = fetch_unified_calendar()
    if df.empty:
        log.error("❌ No IPO data after all sources and validation — aborting.")
        return None

    log.info(f"📦 Scoring {len(df)} open IPOs …")

    w        = bayesian_weight_update(df)
    allots:   Dict[str, AllotmentProfile] = {}
    shariahs: Dict[str, ShariahVerdict]   = {}
    scores:   List[dict]                  = []

    for _, row in df.iterrows():
        sym           = str(row["Symbol"])
        allots[sym]   = compute_allotment(row)
        shariahs[sym] = run_shariah(row)
        scores.append(master_score(row, allots[sym], shariahs[sym], w))

    df["FinalScore"]        = [s["FinalScore"]        for s in scores]
    df["Verdict"]           = [s["Verdict"]           for s in scores]
    df["p_single_mc"]       = [allots[s].p_single_mc  for s in df["Symbol"]]
    df["optimal_syndicate"] = [allots[s].optimal_syndicate for s in df["Symbol"]]
    df["kelly_pct"]         = [allots[s].kelly_pct    for s in df["Symbol"]]
    df["ev_inr"]            = [allots[s].ev_inr       for s in df["Symbol"]]
    df["roi_pct"]           = [allots[s].roi_pct      for s in df["Symbol"]]
    df["barakah"]           = [shariahs[s].barakah_index for s in df["Symbol"]]
    df["halal_tier"]        = [shariahs[s].tier        for s in df["Symbol"]]
    df["najash_alert"]      = [shariahs[s].najash_alert for s in df["Symbol"]]

    persist_db(df, allots, shariahs)

    JSON_EXPORT.parent.mkdir(parents=True, exist_ok=True)
    df.to_json(str(JSON_EXPORT), orient="records", indent=2)
    log.info(f"📄  JSON → {JSON_EXPORT}")

    # Console table
    ranked = df.sort_values("FinalScore", ascending=False)
    print(f"\n{'═'*100}")
    print(f"  {VERSION}  |  {TODAY}")
    print(f"{'═'*100}")
    print(f"  {'Symbol':<32} {'Score':>6}  {'Verdict':<14}  "
          f"{'Sub':>7}  {'GMP':>7}  {'Days':>4}  {'Synd':>4}  {'Halal':<26}  Source")
    print(f"  {'─'*32} {'─'*6}  {'─'*14}  "
          f"{'─'*7}  {'─'*7}  {'─'*4}  {'─'*4}  {'─'*26}  {'─'*20}")
    for _, row in ranked.iterrows():
        sym = str(row["Symbol"])
        a   = allots[sym]
        sh  = shariahs[sym]
        print(
            f"  {sym:<32} {row['FinalScore']:>6.1f}  {row['Verdict']:<14}  "
            f"{row['SubscriptionTimes']:>6.1f}×  {row['gmp_pct']:>6.1f}%  "
            f"{row['DaysToClose']:>4}  {a.optimal_syndicate:>4}  "
            f"{sh.tier:<26}  {str(row.get('Source',''))[:20]}"
        )
    print(f"{'═'*100}\n")

    send_telegram_alerts(df, allots, shariahs)
    log.info("🏁  Complete.")
    return df

if __name__ == "__main__":
    run()
