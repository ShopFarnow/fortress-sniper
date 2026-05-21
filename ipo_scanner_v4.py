#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          IPO SNIPER v5.4 — PRODUCTION BULLETPROOF EDITION                    ║
║  Data Engineering · Bayesian Engine · Shariah Matrix · Telegram Integration  ║
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
# CONFIGURATION MANAGEMENT
# ═══════════════════════════════════════════════════════════
IPO_DB_PATH      = Path("data/ipo_sniper_v5.db")
FALLBACK_CSV     = Path("data/ipo_fallback_v5.csv")   
JSON_EXPORT      = Path("data/ipo_latest_run.json")
VERSION          = "IPO-SNIPER-v5.4-BULLETPROOF"
MONTE_CARLO_RUNS = 50_000
KELLY_FRACTION   = 0.25
MAX_SYNDICATE    = 10
SEED             = 42
np.random.seed(SEED)

CHITT_LIVE_URLS = {
    "Mainboard": "https://www.chittorgarh.com/report/ipo-subscription-status/10/",
    "SME":       "https://www.chittorgarh.com/report/sme-ipo-subscription-status/10/",
}
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-8s │ %(message)s")
log = logging.getLogger("IPO-SNIPER-v5")

TODAY = datetime.today().date()

# ═══════════════════════════════════════════════════════════
# TYPE-SAFE SCANNERS AND PARSERS
# ═══════════════════════════════════════════════════════════
def _flt(v, default: float = 0.0) -> float:
    """Type-safe numeric transformation prevents expression runtime casting errors."""
    if v is None: return default
    try:
        m = re.search(r"[\d.]+", str(v).replace(",", ""))
        return float(m.group()) if m else default
    except Exception:
        return default

def _int(v, default: int = 0) -> int:
    if v is None: return default
    try:
        m = re.search(r"\d+", str(v).replace(",", ""))
        return int(m.group()) if m else default
    except Exception:
        return default

def _jitter(lo: float = 1.5, hi: float = 3.5):
    time.sleep(random.uniform(lo, hi))

def _parse_date(text: str) -> Optional[object]:
    text = str(text).strip()
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%m-%Y", "%Y-%m-%d", "%b %d, %Y", "%d/%m/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError: pass
    return None

def _parse_price_band(text: str) -> Tuple[float, float]:
    nums = re.findall(r"[\d.]+", str(text).replace(",", ""))
    if len(nums) >= 2: return float(nums[0]), float(nums[-1])
    if len(nums) == 1:
        v = float(nums[0])
        return round(v * 0.97, 2), v
    return 95.0, 100.0

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8",
    "Cache-Control": "no-cache",
}

def _make_session(referer: str = "https://www.google.com/") -> requests.Session:
    s = requests.Session()
    s.headers.update({**BROWSER_HEADERS, "Referer": referer})
    return s

# ═══════════════════════════════════════════════════════════
# SECURE STRUCTURAL PARSING ROUTINES
# ═══════════════════════════════════════════════════════════
SKIP_SYMBOLS = {"company", "name", "issuer", "no records found", "compare", "click here", "", "open", "closed", "upcoming"}

def _parse_html_table(table, ipo_type: str, source_tag: str) -> pd.DataFrame:
    sector = "Mainboard" if "main" in ipo_type.lower() else "SME"
    rows = table.find_all("tr")
    if len(rows) < 2: return pd.DataFrame()

    hdr = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
    col: Dict[str, int] = {}
    for i, h in enumerate(hdr):
        if any(k in h for k in ("company", "issuer", "name", "ipo")): col.setdefault("sym", i)
        elif any(k in h for k in ("size", "cr", "amt")): col.setdefault("size", i)
        elif any(k in h for k in ("price", "band", "rate")): col.setdefault("price", i)
        elif any(k in h for k in ("close", "end date", "closing")): col.setdefault("close", i)
        elif any(k in h for k in ("open", "start", "opening")): col.setdefault("open", i)
        elif any(k in h for k in ("lot", "qty", "shares")): col.setdefault("lot", i)
        elif "gmp" in h or "premium" in h: col.setdefault("gmp", i)
        elif any(k in h for k in ("sub", "times", "overall", "x")): col.setdefault("sub", i)

    col.setdefault("sym", 0)
    records = []
    
    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) <= max(col.values(), default=0): continue

        def _c(key, default=""):
            idx = col.get(key)
            return cells[idx].get_text(strip=True) if idx is not None and idx < len(cells) else default

        lnk = cells[col["sym"]].find("a")
        symbol = (lnk.get_text(strip=True) if lnk else cells[col["sym"]].get_text(strip=True)).strip()
        symbol = re.sub(r"\s+", " ", symbol)
        if not symbol or symbol.lower() in SKIP_SYMBOLS or len(symbol) < 2: continue

        size = _flt(_c("size", "50"), 50.0)
        if size > 50_000: size /= 1e7
        lo, hi = _parse_price_band(_c("price", "100"))
        lot = _int(_c("lot", "")) or (1000 if sector == "SME" else 50)

        close_raw = _c("close", "")
        close_dt = _parse_date(close_raw) if close_raw else None
        if close_dt is None: close_dt = TODAY + timedelta(days=10)

        # Patched explicit fallback condition prevents calculation drops
        gmp_raw = _c("gmp", "")
        if gmp_raw:
            gmp_v = _flt(gmp_raw, 0.0)
            gmp = gmp_v / 100 if gmp_v > 1 else gmp_v
        else:
            gmp = 0.0

        sub = _flt(_c("sub", "0"), 0.0)

        records.append({
            "Symbol": symbol, "Sector": sector, "IssueSizeCr": round(size, 2),
            "PriceBandLower": lo, "PriceBandUpper": hi, "LotSize": lot,
            "GMP": round(gmp, 4), "gmp_pct": round(gmp * 100, 2), "SubscriptionTimes": round(sub, 2),
            "CloseDate": close_dt.strftime("%Y-%m-%d"), "DaysToClose": (close_dt - TODAY).days, "Source": source_tag,
        })
    return pd.DataFrame(records)

# ═══════════════════════════════════════════════════════════
# DATA INGESTION ENGINE FEEDS
# ═══════════════════════════════════════════════════════════
def _fetch_chitt_playwright(url: str, ipo_type: str, source_tag: str) -> pd.DataFrame:
    if not PLAYWRIGHT_OK: return pd.DataFrame()
    log.info(f"  PW [{ipo_type}] → {url}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"])
            page = ctx.new_page()

            intercepted: List[dict] = []
            def _on_resp(resp):
                if resp.status == 200 and "chittorgarh" in resp.url:
                    if "json" in resp.headers.get("content-type", ""):
                        try:
                            rows = resp.json().get("data", resp.json().get("aaData", []))
                            if rows: intercepted.extend(rows)
                        except Exception: pass
            page.on("response", _on_resp)

            page.goto(url, wait_until="networkidle", timeout=55_000)
            try: page.wait_for_selector("table tbody tr td:not(.dataTables_empty)", timeout=15_000)
            except PWTimeout: pass

            if intercepted:
                sector = "Mainboard" if "main" in ipo_type.lower() else "SME"
                records = []
                for row_data in intercepted[:60]:
                    cells = row_data if isinstance(row_data, list) else list(row_data.values())
                    clean = [BeautifulSoup(str(c), "html.parser").get_text(strip=True) for c in cells]
                    if not clean or len(clean) < 4: continue  # Patched strict length index guard
                    
                    symbol = clean[0]
                    if symbol.lower() in SKIP_SYMBOLS: continue
                    size = _flt(clean[1], 50.0)
                    lo, hi = _parse_price_band(clean[2])
                    lot = _int(clean[3]) or (1000 if sector == "SME" else 50)
                    
                    close_dt = _parse_date(clean[4]) if len(clean) > 4 else None
                    if not close_dt: close_dt = TODAY + timedelta(days=10)
                    
                    sub = _flt(clean[5], 0.0) if len(clean) > 5 else 0.0
                    gmp_raw = clean[6] if len(clean) > 6 else ""
                    gmp_v = _flt(gmp_raw, 0.0) if gmp_raw else 0.0
                    gmp = gmp_v / 100 if gmp_v > 1 else gmp_v

                    records.append({
                        "Symbol": symbol, "Sector": sector, "IssueSizeCr": round(size, 2),
                        "PriceBandLower": lo, "PriceBandUpper": hi, "LotSize": lot,
                        "GMP": round(gmp, 4), "gmp_pct": round(gmp * 100, 2), "SubscriptionTimes": round(sub, 2),
                        "CloseDate": close_dt.strftime("%Y-%m-%d"), "DaysToClose": (close_dt - TODAY).days, "Source": source_tag + "_ajax",
                    })
                browser.close()
                return pd.DataFrame(records)

            soup = BeautifulSoup(page.content(), "html.parser")
            browser.close()
            for tbl in soup.find_all("table"):
                if len(tbl.find_all("tr")) > 3:
                    df = _parse_html_table(tbl, ipo_type, source_tag + "_html")
                    if not df.empty: return df
    except Exception as e:
        log.warning(f"  PW execution crash fallback route triggered [{ipo_type}]: {e}")
    return pd.DataFrame()

def _fetch_chitt_http(url: str, ipo_type: str, source_tag: str) -> pd.DataFrame:
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
                    df = _parse_html_table(tbl, ipo_type, source_tag + "_http")
                    if not df.empty: return df
    except Exception as e:
        log.warning(f"  HTTP connection vector timeout [{ipo_type}]: {e}")
    return pd.DataFrame()

def fetch_source_a_chittorgarh() -> pd.DataFrame:
    log.info("━━ SOURCE A: Chittorgarh live subscription pages ━━")
    all_frames: List[pd.DataFrame] = []
    for itype, url in CHITT_LIVE_URLS.items():
        tag = f"chitt_live_{itype.lower()}"
        df = _fetch_chitt_playwright(url, itype, tag)
        if df.empty: df = _fetch_chitt_http(url, itype, tag)
        if not df.empty: all_frames.append(df)
        _jitter(2.0, 4.0)

    for itype, url in CHITT_UPCOMING_URLS.items():
        tag = f"chitt_upcoming_{itype.lower()}"
        df = _fetch_chitt_playwright(url, itype, tag)
        if df.empty: df = _fetch_chitt_http(url, itype, tag)
        if not df.empty: all_frames.append(df)
        _jitter(1.5, 3.0)

    return pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()

def fetch_source_b_investorgain() -> pd.DataFrame:
    log.info("━━ SOURCE B: Investorgain GMP ━━")
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/"
    sess = _make_session("https://www.investorgain.com/")
    try:
        resp = sess.get(url, timeout=25)
        if resp.status_code != 200: return pd.DataFrame()
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table")
        if not table: return pd.DataFrame()

        rows = table.find_all("tr")
        hdr = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        col: Dict[str, int] = {}
        for i, h in enumerate(hdr):
            if any(k in h for k in ("ipo", "company", "name")): col.setdefault("sym", i)
            elif "gmp" in h: col.setdefault("gmp", i)
            elif any(k in h for k in ("sub", "times")): col.setdefault("sub", i)
            elif "price" in h: col.setdefault("price", i)
            elif any(k in h for k in ("close", "date", "end")): col.setdefault("close", i)
            elif any(k in h for k in ("size", "cr")): col.setdefault("size", i)
            elif "lot" in h: col.setdefault("lot", i)

        col.setdefault("sym", 0)
        records = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells or len(cells) <= max(col.values(), default=0): continue

            def _c(key, default=""):
                idx = col.get(key)
                return cells[idx].get_text(strip=True) if idx is not None and idx < len(cells) else default

            symbol = re.sub(r"<[^>]+>", "", cells[col["sym"]].get_text(strip=True)).strip()
            if not symbol or len(symbol) < 3 or symbol.lower() in SKIP_SYMBOLS: continue

            gmp_raw = _c("gmp", "")
            gmp = (_flt(gmp_raw, 0.0) / 100 if _flt(gmp_raw, 0.0) > 1 else _flt(gmp_raw, 0.0)) if gmp_raw else 0.0

            lo, hi = _parse_price_band(_c("price", "100"))
            sub = _flt(_c("sub", "0"), 0.0)
            size = _flt(_c("size", "50"), 50.0)
            lot = _int(_c("lot", "")) or 1000
            close_dt = _parse_date(_c("close", "")) or (TODAY + timedelta(days=7))

            records.append({
                "Symbol": symbol, "Sector": "Mainboard" if hi > 250 or lot < 200 else "SME", "IssueSizeCr": round(size, 2),
                "PriceBandLower": lo, "PriceBandUpper": hi, "LotSize": lot, "GMP": round(gmp, 4), "gmp_pct": round(gmp * 100, 2),
                "SubscriptionTimes": round(sub, 2), "CloseDate": close_dt.strftime("%Y-%m-%d"), "DaysToClose": (close_dt - TODAY).days, "Source": "investorgain_gmp",
            })
        return pd.DataFrame(records)
    except Exception as e:
        log.warning(f"  Investorgain pipeline tracking fault: {e}")
    return pd.DataFrame()

def fetch_source_c_nse() -> pd.DataFrame:
    log.info("━━ SOURCE C: NSE India API ━━")
    sess = _make_session("https://www.nseindia.com/")
    sess.headers.update({"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*", "Referer": "https://www.nseindia.com/market-data/upcoming-issues-ipo"})
    for url in NSE_WARMUP:
        try: sess.get(url, timeout=12)
        except Exception: pass
        _jitter(1.5, 2.5)

    records: List[dict] = []
    seen: set = set()
    for endpoint, sector in NSE_ENDPOINTS:
        try:
            resp = sess.get(endpoint, timeout=20)
            if resp.status_code != 200 or len(resp.content) < 30: continue
            data = resp.json()
            items = data if isinstance(data, list) else data.get("data", [])
            for item in items:
                if not isinstance(item, dict): continue
                sym = str(item.get("symbol", item.get("companyName", item.get("issuerName", "")))).strip()
                if not sym or len(sym) < 2 or sym in seen: continue

                lo, hi = _parse_price_band(str(item.get("priceBand", item.get("issuePrice", "100"))))
                size = _flt(item.get("issueSize", item.get("totalIssueSizeCr", 50.0)), 50.0)
                if size > 50_000: size /= 1e7
                lot = _int(item.get("lotSize", item.get("minBidQuantity", 0))) or (1000 if sector == "SME" else 50)
                sub_raw = str(item.get("subscriptionTimes", item.get("subscriptionStatus", "0")))
                sub = _flt(re.search(r"[\d.]+", sub_raw).group() if re.search(r"[\d.]+", sub_raw) else "0")
                close_dt = _parse_date(str(item.get("closeDate", item.get("biddingEndDate", "")))) or (TODAY + timedelta(days=10))
                
                seen.add(sym)
                records.append({
                    "Symbol": sym, "Sector": sector, "IssueSizeCr": round(size, 2), "PriceBandLower": lo, "PriceBandUpper": hi,
                    "LotSize": lot, "GMP": 0.0, "gmp_pct": 0.0, "SubscriptionTimes": round(sub, 2),
                    "CloseDate": close_dt.strftime("%Y-%m-%d"), "DaysToClose": (close_dt - TODAY).days, "Source": "nse_api",
                })
            _jitter(1.5, 3.0)
        except Exception as e: log.warning(f"  NSE API network exception: {e}")
    return pd.DataFrame(records)

def _rebuild_fallback_csv() -> pd.DataFrame:
    """FIX #5 Rebuilds todays standalone fallback dataset dynamically if servers go black."""
    FALLBACK_CSV.parent.mkdir(parents=True, exist_ok=True)
    seed = [
        {"Symbol": "Placeholder IPO Alpha Ltd",  "IssueSizeCr": 75.0,  "PriceBandLower": 140, "PriceBandUpper": 148, "LotSize": 1000, "GMP": 0.15, "SubscriptionTimes": 14.5, "Sector": "SME",       "CloseDate": (TODAY + timedelta(3)).strftime("%Y-%m-%d")},
        {"Symbol": "Placeholder IPO Beta Corp",   "IssueSizeCr": 250.0, "PriceBandLower": 300, "PriceBandUpper": 320, "LotSize": 50,   "GMP": 0.35, "SubscriptionTimes": 48.2, "Sector": "Mainboard", "CloseDate": (TODAY + timedelta(5)).strftime("%Y-%m-%d")},
    ]
    df = pd.DataFrame(seed)
    df["Source"] = "FALLBACK_SEED_EMERGENCY"
    df.to_csv(FALLBACK_CSV, index=False)
    return df

# ═══════════════════════════════════════════════════════════
# STRICT VALIDATION AND DEDUPLICATION PIPELINE
# ═══════════════════════════════════════════════════════════
REQUIRED_DEFAULTS = {
    "Symbol": "UNKNOWN", "Sector": "SME", "IssueSizeCr": 50.0, "PriceBandLower": 95.0, "PriceBandUpper": 100.0,
    "LotSize": 1000, "GMP": 0.0, "gmp_pct": 0.0, "SubscriptionTimes": 0.0,
    "CloseDate": (TODAY + timedelta(days=7)).strftime("%Y-%m-%d"), "DaysToClose": 7, "Source": "unknown",
}

def _validate_row(row: pd.Series) -> Tuple[bool, str]:
    sym = str(row.get("Symbol", "")).strip()
    if not sym or len(sym) < 2 or sym.lower() in ("unknown", "nan", "none"): return False, "invalid_symbol"
    if float(row.get("PriceBandUpper", 0)) <= 0: return False, "price_out_of_bounds"
    # FIX #2 STRICT DATE GUARD: Instantly drops issues whose lifecycles ended in the past
    if int(row.get("DaysToClose", 0)) < 0: return False, "deal_closed_historical"
    return True, ""

def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    for col, val in REQUIRED_DEFAULTS.items():
        if col not in df.columns: df[col] = val

    for c in ("IssueSizeCr", "PriceBandLower", "PriceBandUpper", "LotSize", "GMP", "gmp_pct", "SubscriptionTimes"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(REQUIRED_DEFAULTS.get(c, 0))

    if "source" in df.columns and "Source" not in df.columns: df["Source"] = df["source"]
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
        raw_pool = pd.concat(frames, ignore_index=True)
        enriched = _enrich(raw_pool)
        if not enriched.empty:
            # Cross-Source Aggregation: Extracts highest subscription records and maps best premiums back cleanly
            best_gmp = enriched[enriched["gmp_pct"] > 0].sort_values("gmp_pct", ascending=False).drop_duplicates(subset="Symbol", keep="first")[["Symbol", "GMP", "gmp_pct"]]
            deduped = enriched.sort_values("SubscriptionTimes", ascending=False).drop_duplicates(subset="Symbol", keep="first").reset_index(drop=True)
            
            if not best_gmp.empty:
                deduped = deduped.drop(columns=["GMP", "gmp_pct"], errors="ignore").merge(best_gmp, on="Symbol", how="left")
                deduped["GMP"] = deduped["GMP"].fillna(0.0)
                deduped["gmp_pct"] = deduped["gmp_pct"].fillna(0.0)
                
            return deduped

    log.warning("⚠️ Live feeds offline. Pulling adaptive fallback placeholder variables...")
    return _enrich(_rebuild_fallback_csv())

# ═══════════════════════════════════════════════════════════
# PORTFOLIO CALCULATORS & ADAPTIVE MODEL DRIVERS
# ═══════════════════════════════════════════════════════════
def bayesian_weight_update(df: pd.DataFrame) -> Dict[str, float]:
    w = BASE_WEIGHTS.copy()
    if df.empty: return w
    avg_sub = df["SubscriptionTimes"].mean()
    if avg_sub > 80:
        w["sub"] += 0.10; w["gmp"] -= 0.05; w["halal"] -= 0.05
        log.info(f"📈 Bayesian Regime Matrix: HYPER-BULL Detected (avg_sub={avg_sub:.1f}x)")
    elif avg_sub < 15:
        w["gmp"] += 0.10; w["sub"] -= 0.10; w["halal"] += 0.05
        log.info(f"📉 Bayesian Regime Matrix: TEPID Market Detected (avg_sub={avg_sub:.1f}x)")
    total = sum(w.values())
    return {k: round(v / total, 6) for k, v in w.items()}

@dataclass
class AllotmentProfile:
    symbol: str; p_single_mc: float; syndicate_matrix: Dict[int, float]; optimal_syndicate: int
    kelly_pct: float; ev_inr: float; roi_pct: float; ci_95: Tuple[float, float]

@dataclass
class ShariahVerdict:
    symbol: str; tier: str; barakah_index: float; najash_alert: bool; qabda_mandate: str; deferred_issues: List[str]

def compute_allotment(row: pd.Series) -> AllotmentProfile:
    sub = max(0.1, float(row["SubscriptionTimes"]))
    price = float(row["PriceBandUpper"])
    lot = int(row["LotSize"])
    size = float(row["IssueSizeCr"])
    gmp = float(row["GMP"])

    retail = size * 1e7 * 0.35
    avail = max(1, int(retail / (lot * price)))
    total = max(avail + 1, int(avail * sub))
    p_true = avail / total
    
    hits = np.random.binomial(1, p_true, MONTE_CARLO_RUNS)
    p_mc = hits.mean()
    
    z = 1.96
    denom = 1 + z**2 / MONTE_CARLO_RUNS
    center = (p_mc + z**2 / (2 * MONTE_CARLO_RUNS)) / denom
    spread = (z * math.sqrt(p_mc * (1 - p_mc) / MONTE_CARLO_RUNS + z**2 / (4 * MONTE_CARLO_RUNS**2))) / denom
    
    matrix = {k: round(1 - (1 - p_mc) ** k, 6) for k in range(1, MAX_SYNDICATE + 1)}
    gain = gmp * price * lot
    b_odds = gain / 1500.0
    cost = lot * price

    best_k, best_ev = 1, -float("inf")
    for k, p_win in matrix.items():
        ev = p_win * gain - k * (cost + 500.0)
        if ev > best_ev: best_ev = ev; best_k = k

    p_opt = matrix[best_k]
    f_star = (b_odds * p_opt - (1 - p_opt)) / max(0.01, b_odds)
    
    return AllotmentProfile(
        symbol=str(row["Symbol"]), p_single_mc=p_mc, syndicate_matrix=matrix, optimal_syndicate=best_k,
        kelly_pct=round(max(0.0, KELLY_FRACTION * f_star) * 100, 2), ev_inr=round(p_opt * gain, 2),
        roi_pct=round((round(p_opt * gain, 2) / max(1.0, cost * best_k)) * 100, 4), ci_95=(max(0.0, round(center - spread, 6)), min(1.0, round(center + spread, 6)))
    )

def run_shariah(row: pd.Series) -> ShariahVerdict:
    gmp, sub, size = float(row["GMP"]), float(row["SubscriptionTimes"]), float(row["IssueSizeCr"])
    barakah = 100.0
    issues = []

    # Frame 1: Najash Speculative Bubble Containment
    najash = gmp > 0.40 and sub > 80
    if najash: barakah -= 25; issues.append("Speculative Bidding Bubble (Najash Active)")
    if size < 20: barakah -= 15; issues.append("Microcap Liquidity Hazard (<₹20 Cr)")
    if str(row["Sector"]) == "SME" and sub > 200: barakah -= 10; issues.append("Hyper-Pump Ingestion Danger (Sub>200×)")

    return ShariahVerdict(
        str(row["Symbol"]), "TIER_1_SHARIAH_COMPLIANT" if barakah >= 80 else "TIER_2_CONDITIONAL", max(0.0, barakah), najash,
        "QABDA MANDATE: Transaction exits are locked until physical ledger delivery credits the Demat account completely.", issues
    )

def master_score(row, allot, shariah, w) -> Dict:
    days = max(0, int(row["DaysToClose"]))
    tf = 1.0 if days >= 7 else (0.5 + 0.5 * days / 7)
    gmp, sub, size = float(row["GMP"]), float(row["SubscriptionTimes"]), float(row["IssueSizeCr"])

    s_gmp = min(100.0, gmp * 200)
    s_sub = min(100.0, (sub / 100.0) * 100) * tf
    s_sent = min(100.0, 40.0 + (20.0 if sub > 50 else 10.0 if sub > 25 else 0.0) + (20.0 if gmp > 0.40 else 10.0 if gmp > 0.20 else 0.0))
    s_size = 100 if size <= 20 else 80 if size <= 50 else 50 if size <= 100 else 20
    
    raw = (s_gmp * w["gmp"] + s_sub * w["sub"] + s_sent * w["sentiment"] + 50.0 * w["trend"] + s_size * w["size"] + shariah.barakah_index * w["halal"])
    final = min(100.0, max(0.0, round(raw, 1)))
    return {"FinalScore": final, "Verdict": "🔥 PEARL" if final >= 80 else "✅ STRONG BUY" if final >= 70 else "📈 MODERATE" if final >= 60 else "❌ SKIP"}

# ═══════════════════════════════════════════════════════════
# DATA DISTRIBUTION AND NOTIFICATIONS LAYER
# ═══════════════════════════════════════════════════════════
def init_db():
    IPO_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(IPO_DB_PATH)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ipo_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT, symbol TEXT, sector TEXT, final_score REAL, verdict TEXT,
                subscription_x REAL, gmp_pct REAL, issue_size_cr REAL, price_upper REAL, lot_size INTEGER, close_date TEXT, days_to_close INTEGER,
                p_single_mc REAL, ci_lo REAL, ci_hi REAL, optimal_syndicate INTEGER, kelly_pct REAL, ev_inr REAL, roi_pct REAL,
                barakah REAL, halal_tier TEXT, najash_alert INTEGER, source TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(run_date, symbol)
            )
        """)

def send_telegram_alerts(df: pd.DataFrame, allots: dict, shariahs: dict):
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id: return

    # Index Summary Sheet Notification Construction
    header = f"⚔️ <b>{VERSION}</b>\n📅 <b>{TODAY.strftime('%d %b %Y')}</b> │ Active Pipeline: {len(df)} Open IPOs\n"
    if any("FALLBACK" in str(r.get("Source","")) for _, r in df.iterrows()):
        header += "⚠️ <i>Standby backup matrix initialized. Real listings offline.</i>\n"
    header += "━" * 30 + "\n"

    for _, row in df.sort_values("FinalScore", ascending=False).iterrows():
        header += f"  {row['Verdict']} <b>{html_lib.escape(str(row['Symbol']))}</b> ({row['FinalScore']:.0f}) │ Sub: {row['SubscriptionTimes']:.1f}x │ GMP: {row['gmp_pct']:.1f}%\n"
    
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": header[:4090], "parse_mode": "HTML"}, timeout=12)
    _jitter(0.5, 1.2)

    # Detailed Audit Notification Channels
    for _, row in df.sort_values("FinalScore", ascending=False).iterrows():
        sym = str(row["Symbol"]); a = allots[sym]; sh = shariahs[sym]
        esc_sym = html_lib.escape(sym)
        
        msg = (
            f"{'🔥' if row['FinalScore'] >= 80 else '✅'} <b>{esc_sym}</b> [{row['Sector']}]\n"
            f"  🏆 Score Vector: <b>{row['FinalScore']:.1f}/100</b> ➔ {row['Verdict']}\n\n"
            f"  📊 Subscription: <b>{row['SubscriptionTimes']:.1f}x</b> │ Premium GMP: <b>{row['gmp_pct']:.1f}%</b>\n"
            f"  💹 Band: ₹{row['PriceBandLower']:.0f}–₹{row['PriceBandUpper']:.0f} │ Lot Size: {row['LotSize']} │ Size: ₹{row['IssueSizeCr']:.0f}Cr\n"
            f"  📅 Timeline: {row['CloseDate']} ({row['DaysToClose']}d remaining)\n\n"
            f"  🎲 P(Allotment): <b>{a.p_single_mc * 100:.3f}%</b> [95% CI: {a.ci_95[0]*100:.2f}%–{a.ci_95[1]*100:.2f}%]\n"
            f"  👥 Syndicate Consortia: <b>{a.optimal_syndicate} Independent PANs</b>\n"
            f"  💰 Kelly Allocation: {a.kelly_pct}% │ Expected EV Value: ₹{a.ev_inr:,.0f}\n\n"
            f"  🕌 <b>{sh.tier}</b> (Barakah: {sh.barakah_index:.0f}/100)\n"
            f"  ⚖️ <i>{html_lib.escape(sh.qabda_mandate)}</i>\n"
            f"  🔗 Origin: <code>{html_lib.escape(str(row.get('Source','')))}</code>"
        )
        if sh.deferred_issues: msg += f"\n  🚨 Warnings: " + " │ ".join([html_lib.escape(i) for i in sh.deferred_issues])
        
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg[:4090], "parse_mode": "HTML"}, timeout=12)
        _jitter(0.4, 0.9)

# ═══════════════════════════════════════════════════════════
# SYSTEM ENTRY ORCHESTRATION PIPELINE
# ═══════════════════════════════════════════════════════════
def run():
    log.info(f"🚀 Initializing System Orchestration Workspace {VERSION} [{TODAY}]")
    init_db()

    df = fetch_unified_calendar()
    if df.empty:
        log.error("❌ Structural failure inside tracking arrays. Calendar initialization aborted.")
        return None

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

    # Persist and write execution traces locally
    with sqlite3.connect(str(IPO_DB_PATH)) as con:
        for _, r in df.iterrows():
            sym = str(r["Symbol"])
            con.execute("""
                INSERT OR REPLACE INTO ipo_scans (
                    run_date, symbol, sector, final_score, verdict, subscription_x, gmp_pct, issue_size_cr, price_upper, lot_size, close_date, days_to_close,
                    p_single_mc, ci_lo, ci_hi, optimal_syndicate, kelly_pct, ev_inr, roi_pct, barakah, halal_tier, najash_alert, source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (TODAY.strftime("%Y-%m-%d"), sym, r["Sector"], r["FinalScore"], r["Verdict"], r["SubscriptionTimes"], r["gmp_pct"], r["IssueSizeCr"],
                  r["PriceBandUpper"], int(r["LotSize"]), r["CloseDate"], int(r["DaysToClose"]), allots[sym].p_single_mc, allots[sym].ci_95[0], allots[sym].ci_95[1],
                  allots[sym].optimal_syndicate, allots[sym].kelly_pct, allots[sym].ev_inr, allots[sym].roi_pct, shariahs[sym].barakah_index, shariahs[sym].tier, int(shariahs[sym].najash_alert), str(r.get("Source", "unknown"))))

    JSON_EXPORT.parent.mkdir(parents=True, exist_ok=True)
    df.to_json(str(JSON_EXPORT), orient="records", indent=2)

    # Print clean summary console tracking matrix layout
    ranked = df.sort_values("FinalScore", ascending=False)
    print(f"\n{'═'*100}\n  {VERSION} EVALUATION INSIGHTS SHEET │ {TODAY}\n{'═'*100}")
    print(f"  {'Symbol':<32} {'Score':>6}  {'Verdict':<14}  {'Sub':>7}  {'GMP':>7}  {'Days':>4}  {'Synd':>4}  {'Halal':<26}  Source")
    print(f"  {'─'*32} {'─'*6}  {'─'*14}  {'─'*7}  {'─'*7}  {'─'*4}  {'─'*4}  {'─'*26}  {'─'*20}")
    for _, row in ranked.iterrows():
        sym = str(row["Symbol"]); a = allots[sym]; sh = shariahs[sym]
        print(f"  {sym:<32} {row['FinalScore']:>6.1f}  {row['Verdict']:<14}  {row['SubscriptionTimes']:>6.1f}×  {row['gmp_pct']:>6.1f}%  {row['DaysToClose']:>4}  {a.optimal_syndicate:>4}  {sh.tier:<26}  {str(row.get('Source',''))[:20]}")
    print(f"{'═'*100}\n")

    send_telegram_alerts(df, allots, shariahs)
    log.info("🏁 Strategy pipeline execution operations successfully finalized.")
    return df

if __name__ == "__main__":
    run()
