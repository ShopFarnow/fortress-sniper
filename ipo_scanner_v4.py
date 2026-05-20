#!/usr/bin/env python3
"""
IPO SNIPER v5.0 – BULLETPROOF PRODUCTION EDITION
- 3‑source fetch chain (NSE, Chittorgarh, Investorgain)
- DRHP entries kept (default 30 days)
- Telegram summary + top 5 detailed
"""

import os
import re
import math
import time
import json
import random
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

# ── Optional Playwright ────────────────────────────────────────────────────
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL CONFIG
# ══════════════════════════════════════════════════════════════════════════════
VERSION           = "IPO-SNIPER-v5.0-BULLETPROOF"
DB_PATH           = Path("data/ipo_sniper_v5.db")
FALLBACK_CSV      = Path("data/ipo_fallback.csv")
JSON_EXPORT       = Path("data/ipo_latest_run.json")
MC_RUNS           = 50_000
KELLY_FRACTION    = 0.25
MAX_SYNDICATE     = 10
SEED              = 42

np.random.seed(SEED)
random.seed(SEED)

BASE_WEIGHTS: Dict[str, float] = {
    "gmp":       0.22,
    "sub":       0.28,
    "sentiment": 0.18,
    "trend":     0.10,
    "size":      0.08,
    "halal":     0.14,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s"
)
log = logging.getLogger("IPO-SNIPER-v5")

TODAY = datetime.today().date()

# ══════════════════════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _flt(s, default: float = 0.0) -> float:
    try:
        m = re.search(r"[\d.]+", str(s).replace(",", ""))
        return float(m.group()) if m else default
    except Exception:
        return default

def _int(s, default: int = 0) -> int:
    try:
        m = re.search(r"\d+", str(s).replace(",", ""))
        return int(m.group()) if m else default
    except Exception:
        return default

def _parse_price_band(text: str) -> Tuple[float, float]:
    nums = re.findall(r"[\d.]+", str(text).replace(",", ""))
    if len(nums) >= 2:
        return float(nums[0]), float(nums[-1])
    if len(nums) == 1:
        v = float(nums[0])
        return round(v * 0.97, 2), v
    return 95.0, 100.0

def _parse_date(text: str) -> Optional[datetime]:
    if not text or str(text).strip() == "":
        return None
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%m-%Y", "%Y-%m-%d",
                "%b %d, %Y", "%d/%m/%Y", "%b %Y", "%B %Y"):
        try:
            return datetime.strptime(str(text).strip(), fmt).date()
        except ValueError:
            pass
    return None

def _jitter(lo: float = 1.5, hi: float = 4.0):
    time.sleep(random.uniform(lo, hi))

# ══════════════════════════════════════════════════════════════════════════════
# HTTP SESSION FACTORY
# ══════════════════════════════════════════════════════════════════════════════

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Cache-Control": "max-age=0",
}

def _make_session(referer: str = "https://www.google.com/") -> requests.Session:
    s = requests.Session()
    s.headers.update({**BROWSER_HEADERS, "Referer": referer})
    return s

# ══════════════════════════════════════════════════════════════════════════════
# SOURCE A — NSE INDIA OFFICIAL API
# ══════════════════════════════════════════════════════════════════════════════

NSE_API_ENDPOINTS = [
    ("https://www.nseindia.com/api/ipo", "Mainboard"),
    ("https://www.nseindia.com/api/emerge-ipo", "SME"),
    ("https://www.nseindia.com/api/otherMarketData?identifier=UPCOMING_IPO", "Mainboard"),
    ("https://www.nseindia.com/api/ipo-current-allotment", "Mainboard"),
]

NSE_WARMUP_URLS = [
    "https://www.nseindia.com",
    "https://www.nseindia.com/market-data/upcoming-issues-ipo",
]

def _parse_nse_item(item: dict, sector: str) -> Optional[dict]:
    sym = str(item.get("symbol", item.get("companyName", item.get("issuerName", "")))).strip()
    if not sym or len(sym) < 2:
        return None
    price_txt = str(item.get("priceBand", item.get("issuePrice", "100")))
    lo, hi = _parse_price_band(price_txt)
    size_raw = item.get("issueSize", item.get("totalIssueSizeCr", item.get("issueSizeCrores", 50.0)))
    size = _flt(size_raw, 50.0)
    if size > 50000:
        size /= 1e7
    lot = _int(item.get("lotSize", item.get("minBidQuantity", 0)))
    if lot <= 0:
        lot = 1000 if sector == "SME" else 50
    sub_raw = str(item.get("subscriptionTimes", item.get("subscriptionStatus", "0")))
    sub = _flt(re.search(r"[\d.]+", sub_raw).group() if re.search(r"[\d.]+", sub_raw) else "0")
    gmp_raw = item.get("gmp", item.get("premiumAtGMP", 0))
    gmp = _flt(gmp_raw) / 100 if _flt(gmp_raw) > 1 else _flt(gmp_raw)
    close_raw = str(item.get("closeDate", item.get("biddingEndDate", item.get("closingDate", ""))))
    close_dt = _parse_date(close_raw) or (TODAY + timedelta(days=10))
    days_left = max(0, (close_dt - TODAY).days)
    return {
        "Symbol": sym, "Sector": sector,
        "IssueSizeCr": round(size, 2),
        "PriceBandLower": lo, "PriceBandUpper": hi,
        "LotSize": lot,
        "GMP": gmp, "gmp_pct": round(gmp * 100, 2),
        "SubscriptionTimes": round(sub, 2),
        "CloseDate": close_dt.strftime("%Y-%m-%d"),
        "DaysToClose": days_left,
        "Source": "nse_api",
    }

def fetch_source_a_nse() -> pd.DataFrame:
    log.info("━━ SOURCE A: NSE India API ━━")
    sess = _make_session("https://www.nseindia.com/")
    sess.headers.update({"X-Requested-With": "XMLHttpRequest",
                         "Accept": "application/json, text/plain, */*",
                         "Referer": "https://www.nseindia.com/market-data/upcoming-issues-ipo"})
    for url in NSE_WARMUP_URLS:
        try:
            sess.get(url, timeout=15)
        except Exception:
            pass
        _jitter(1.5, 2.5)
    records = []
    seen = set()
    for endpoint, sector in NSE_API_ENDPOINTS:
        try:
            resp = sess.get(endpoint, timeout=20)
            if resp.status_code != 200 or len(resp.content) < 30:
                continue
            data = resp.json()
            items = data if isinstance(data, list) else data.get("data", data.get("ipoData", []))
            if not isinstance(items, list):
                items = [items]
            for item in items:
                if not isinstance(item, dict):
                    continue
                rec = _parse_nse_item(item, sector)
                if rec and rec["Symbol"] not in seen:
                    seen.add(rec["Symbol"])
                    records.append(rec)
            _jitter(1.5, 3.0)
        except Exception as exc:
            log.debug(f"NSE endpoint error: {exc}")
    df = pd.DataFrame(records)
    log.info(f"  ✅ SOURCE A: {len(df)} IPOs" if not df.empty else "  ⚠️ SOURCE A: no data")
    return df

# ══════════════════════════════════════════════════════════════════════════════
# SOURCE B — CHITTORGARH (HTTP + Playwright)
# ══════════════════════════════════════════════════════════════════════════════

CHITT_URLS = {
    "Mainboard": "https://www.chittorgarh.com/report/ipo-subscription-status/10/",
    "SME":       "https://www.chittorgarh.com/report/sme-ipo-subscription-status/10/",
    "MB_DRHP":   "https://www.chittorgarh.com/report/ipo-drhp-filed-status/158/",
    "SME_DRHP":  "https://www.chittorgarh.com/report/sme-ipo-drhp-filed-status/158/",
}

def _chitt_sector(ipo_type: str) -> str:
    return "Mainboard" if "main" in ipo_type.lower() or "mb" in ipo_type.lower() else "SME"

def _parse_chitt_table(table, ipo_type: str) -> pd.DataFrame:
    sector = _chitt_sector(ipo_type)
    rows = table.find_all("tr")
    if len(rows) < 2:
        return pd.DataFrame()
    hdr = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
    col = {}
    for i, h in enumerate(hdr):
        if any(k in h for k in ("company", "issuer", "name")):   col["sym"] = i
        elif any(k in h for k in ("size", "cr", "amt")):         col["size"] = i
        elif any(k in h for k in ("price", "band")):              col["price"] = i
        elif any(k in h for k in ("close", "end", "date")):       col["close"] = i
        elif any(k in h for k in ("lot", "qty", "shares")):       col["lot"] = i
        elif "gmp" in h:                                          col["gmp"] = i
        elif any(k in h for k in ("sub", "times", "x")):          col["sub"] = i
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
        link = cells[col["sym"]].find("a")
        symbol = (link.get_text(strip=True) if link else cells[col["sym"]].get_text(strip=True)).strip()
        if not symbol or len(symbol) < 2 or symbol.lower() in ("company", "name", "no records found"):
            continue
        size = _flt(_c("size", "50"), 50.0)
        lo, hi = _parse_price_band(_c("price", "100"))
        lot = _int(_c("lot", "1000")) or (1000 if sector == "SME" else 50)
        # DRHP entries have no close date → default 30 days
        close_dt = _parse_date(_c("close", "")) or (TODAY + timedelta(days=30))
        gmp_raw = _flt(_c("gmp", "0"), 0.0)
        gmp = gmp_raw / 100 if gmp_raw > 1 else gmp_raw
        sub = _flt(_c("sub", "0"), 0.0)
        records.append({
            "Symbol": symbol, "Sector": sector,
            "IssueSizeCr": round(size, 2),
            "PriceBandLower": lo, "PriceBandUpper": hi,
            "LotSize": lot,
            "GMP": gmp, "gmp_pct": round(gmp * 100, 2),
            "SubscriptionTimes": round(sub, 2),
            "CloseDate": close_dt.strftime("%Y-%m-%d"),
            "DaysToClose": max(0, (close_dt - TODAY).days),
            "Source": f"chittorgarh_{ipo_type.lower()}_html",
        })
    return pd.DataFrame(records)

def _fetch_chitt_http(url: str, ipo_type: str) -> pd.DataFrame:
    sess = _make_session("https://www.chittorgarh.com/")
    try:
        sess.get("https://www.chittorgarh.com/", timeout=12)
        _jitter(1.5, 3.0)
        resp = sess.get(url, timeout=25)
        if resp.status_code != 200:
            return pd.DataFrame()
        soup = BeautifulSoup(resp.text, "html.parser")
        for sel in ["table#report_table", "table.table-striped", "table.table-bordered",
                    ".table-responsive table", "table[id*='ipo']", "table[class*='ipo']", "table"]:
            for tbl in soup.select(sel):
                if len(tbl.find_all("tr")) > 3:
                    df = _parse_chitt_table(tbl, ipo_type)
                    if not df.empty:
                        return df
    except Exception as exc:
        log.warning(f"Chitt HTTP error [{ipo_type}]: {exc}")
    return pd.DataFrame()

def _fetch_chitt_playwright(url: str, ipo_type: str) -> pd.DataFrame:
    if not PLAYWRIGHT_OK:
        return pd.DataFrame()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"],
                                      extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
                                      viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            intercepted = []
            def on_response(resp):
                if resp.status == 200 and "chittorgarh" in resp.url:
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct:
                        try:
                            rows = resp.json().get("data", [])
                            if rows:
                                intercepted.extend(rows)
                        except:
                            pass
            page.on("response", on_response)
            page.goto(url, wait_until="networkidle", timeout=60000)
            try:
                page.wait_for_selector("table tbody tr td:not(.dataTables_empty)", timeout=15000)
            except:
                pass
            if intercepted:
                sector = _chitt_sector(ipo_type)
                records = []
                for row_data in intercepted[:50]:
                    cells = row_data if isinstance(row_data, list) else list(row_data.values())
                    clean = [BeautifulSoup(str(c), "html.parser").get_text(strip=True) for c in cells]
                    if len(clean) < 2 or len(clean[0]) < 2:
                        continue
                    size = _flt(clean[1] if len(clean) > 1 else "50", 50.0)
                    lo, hi = _parse_price_band(clean[2] if len(clean) > 2 else "100")
                    lot = _int(clean[3] if len(clean) > 3 else "1000") or (1000 if sector == "SME" else 50)
                    close_dt = _parse_date(clean[4] if len(clean) > 4 else "") or (TODAY + timedelta(days=30))
                    sub = _flt(clean[5] if len(clean) > 5 else "0")
                    gmp_raw = _flt(clean[6] if len(clean) > 6 else "0")
                    gmp = gmp_raw / 100 if gmp_raw > 1 else gmp_raw
                    records.append({
                        "Symbol": clean[0], "Sector": sector,
                        "IssueSizeCr": round(size, 2),
                        "PriceBandLower": lo, "PriceBandUpper": hi,
                        "LotSize": lot,
                        "GMP": gmp, "gmp_pct": round(gmp * 100, 2),
                        "SubscriptionTimes": round(sub, 2),
                        "CloseDate": close_dt.strftime("%Y-%m-%d"),
                        "DaysToClose": max(0, (close_dt - TODAY).days),
                        "Source": f"chittorgarh_{ipo_type.lower()}_ajax",
                    })
                browser.close()
                return pd.DataFrame(records)
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            for tbl in soup.find_all("table"):
                if len(tbl.find_all("tr")) > 3:
                    df = _parse_chitt_table(tbl, ipo_type)
                    if not df.empty:
                        browser.close()
                        return df
            browser.close()
    except Exception as exc:
        log.warning(f"Playwright error [{ipo_type}]: {exc}")
    return pd.DataFrame()

def fetch_source_b_chittorgarh() -> pd.DataFrame:
    log.info("━━ SOURCE B: Chittorgarh ━━")
    frames = []
    for typ, url in CHITT_URLS.items():
        df = _fetch_chitt_http(url, typ)
        if df.empty and PLAYWRIGHT_OK:
            df = _fetch_chitt_playwright(url, typ)
        if not df.empty:
            log.info(f"  ✅ Chittorgarh [{typ}]: {len(df)} rows")
            frames.append(df)
        _jitter(2.0, 4.0)
    if frames:
        combined = pd.concat(frames, ignore_index=True)
        log.info(f"  ✅ SOURCE B total: {len(combined)} rows")
        return combined
    return pd.DataFrame()

# ══════════════════════════════════════════════════════════════════════════════
# SOURCE C — INVESTORGAIN GMP
# ══════════════════════════════════════════════════════════════════════════════

INVESTORGAIN_URL = "https://www.investorgain.com/report/live-ipo-gmp/331/"

def fetch_source_c_investorgain() -> pd.DataFrame:
    log.info("━━ SOURCE C: Investorgain GMP ━━")
    sess = _make_session("https://www.investorgain.com/")
    try:
        resp = sess.get(INVESTORGAIN_URL, timeout=25)
        if resp.status_code != 200:
            return pd.DataFrame()
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table")
        if not table:
            return pd.DataFrame()
        rows = table.find_all("tr")
        if len(rows) < 2:
            return pd.DataFrame()
        hdr = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        col = {}
        for i, h in enumerate(hdr):
            if any(k in h for k in ("ipo", "company", "name")): col["sym"] = i
            elif "gmp" in h: col["gmp"] = i
            elif "price" in h: col["price"] = i
            elif "sub" in h: col["sub"] = i
            elif "close" in h: col["close"] = i
        col.setdefault("sym", 0)
        records = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            symbol = cells[col["sym"]].get_text(strip=True)
            symbol = re.sub(r"<[^>]+>", "", symbol).strip()
            if not symbol or len(symbol) < 3:
                continue
            gmp_raw = _flt(cells[col["gmp"]].get_text(strip=True) if "gmp" in col else "0")
            gmp = gmp_raw / 100 if gmp_raw > 1 else gmp_raw
            lo, hi = _parse_price_band(cells[col["price"]].get_text(strip=True) if "price" in col else "100")
            sub = _flt(cells[col["sub"]].get_text(strip=True) if "sub" in col else "0")
            close_dt = _parse_date(cells[col["close"]].get_text(strip=True) if "close" in col else "") or (TODAY + timedelta(days=7))
            records.append({
                "Symbol": symbol, "Sector": "SME",
                "IssueSizeCr": 50.0,
                "PriceBandLower": lo, "PriceBandUpper": hi,
                "LotSize": 1000,
                "GMP": gmp, "gmp_pct": round(gmp * 100, 2),
                "SubscriptionTimes": round(sub, 2),
                "CloseDate": close_dt.strftime("%Y-%m-%d"),
                "DaysToClose": max(0, (close_dt - TODAY).days),
                "Source": "investorgain_gmp",
            })
        df = pd.DataFrame(records)
        log.info(f"  ✅ SOURCE C: {len(df)} rows" if not df.empty else "  ⚠️ SOURCE C: no data")
        return df
    except Exception as exc:
        log.warning(f"Investorgain error: {exc}")
        return pd.DataFrame()

# ══════════════════════════════════════════════════════════════════════════════
# FALLBACK CSV
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_fallback_csv() -> pd.DataFrame:
    FALLBACK_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not FALLBACK_CSV.exists():
        seed = [
            {"Symbol":"Merritronix Ltd","IssueSizeCr":70.03,"PriceBandLower":141,"PriceBandUpper":149,"LotSize":1000,"GMP":0.25,"SubscriptionTimes":45.2,"Sector":"SME","CloseDate":(TODAY+timedelta(3)).strftime("%Y-%m-%d")},
            {"Symbol":"SMR Jewels Ltd","IssueSizeCr":67.23,"PriceBandLower":128,"PriceBandUpper":135,"LotSize":1000,"GMP":0.10,"SubscriptionTimes":12.4,"Sector":"SME","CloseDate":(TODAY+timedelta(5)).strftime("%Y-%m-%d")},
            {"Symbol":"Q-Line Biotech Ltd","IssueSizeCr":214.48,"PriceBandLower":326,"PriceBandUpper":343,"LotSize":50,"GMP":0.40,"SubscriptionTimes":85.3,"Sector":"Mainboard","CloseDate":(TODAY+timedelta(1)).strftime("%Y-%m-%d")},
        ]
        pd.DataFrame(seed).to_csv(FALLBACK_CSV, index=False)
        log.info(f"📄 Created fallback CSV at {FALLBACK_CSV}")
    df = pd.read_csv(FALLBACK_CSV)
    df["Source"] = "fallback_csv"
    return df

# ══════════════════════════════════════════════════════════════════════════════
# DATA ENRICHMENT & VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    for col in ("IssueSizeCr", "PriceBandLower", "PriceBandUpper", "LotSize", "GMP", "SubscriptionTimes"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    if "gmp_pct" not in df.columns:
        df["gmp_pct"] = df["GMP"] * 100
    df["gmp_pct"] = df["gmp_pct"].fillna(0)
    # DaysToClose from CloseDate
    def _days(x):
        try:
            d = datetime.strptime(str(x), "%Y-%m-%d").date()
            return max(0, (d - TODAY).days)
        except:
            return 30   # safe default for DRHP
    df["DaysToClose"] = df["CloseDate"].apply(_days)
    # Remove obvious invalid rows
    df = df[df["Symbol"].astype(str).str.len() >= 2]
    df = df[df["Symbol"].astype(str).str.lower() != "unknown"]
    df = df[df["PriceBandUpper"] > 0]
    return df.reset_index(drop=True)

# ══════════════════════════════════════════════════════════════════════════════
# MASTER FETCH ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

def fetch_ipo_calendar(use_playwright: bool = True) -> pd.DataFrame:
    frames = []
    a = fetch_source_a_nse()
    if not a.empty:
        frames.append(a)
    if use_playwright:
        b = fetch_source_b_chittorgarh()
        if not b.empty:
            frames.append(b)
    c = fetch_source_c_investorgain()
    if not c.empty:
        frames.append(c)
    if not frames:
        log.warning("No live data → fallback CSV")
        return _enrich(_ensure_fallback_csv())
    raw = pd.concat(frames, ignore_index=True)
    enriched = _enrich(raw)
    # Deduplicate keep best GMP and highest sub
    best_gmp = enriched.sort_values("gmp_pct", ascending=False).drop_duplicates("Symbol", keep="first")[["Symbol", "GMP", "gmp_pct"]]
    deduped = enriched.sort_values("SubscriptionTimes", ascending=False).drop_duplicates("Symbol", keep="first").reset_index(drop=True)
    deduped = deduped.drop(columns=["GMP", "gmp_pct"]).merge(best_gmp, on="Symbol", how="left")
    deduped["GMP"] = deduped["GMP"].fillna(0)
    deduped["gmp_pct"] = deduped["gmp_pct"].fillna(0)
    # Only remove IPOs that have a real close date that is already in the past
    # Keep DRHP entries (source contains 'drhp') even if DaysToClose is 0
    before = len(deduped)
    deduped = deduped[(deduped["DaysToClose"] > 0) | (deduped["Source"].str.contains("drhp", case=False, na=False))]
    log.info(f"  🗑 Removed {before - len(deduped)} rows (closed IPOs)")
    log.info(f"✅ LIVE DATA: {len(deduped)} IPOs")
    return deduped

# ══════════════════════════════════════════════════════════════════════════════
# QUANT ENGINE (Allotment, Sentiment, Shariah)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AllotmentProfile:
    symbol: str
    p_single_mc: float
    optimal_syndicate: int
    kelly_pct: float
    ev_inr: float
    roi_pct: float
    ci_95: Tuple[float, float]

@dataclass
class SentimentProfile:
    symbol: str
    composite: float
    label: str

@dataclass
class ShariahVerdict:
    symbol: str
    tier: str
    barakah: float
    najash: bool
    qabda: str
    issues: List[str]
    halal_score: float

def monte_carlo_allotment(sub, lot, size_cr, price, n=MC_RUNS):
    if sub <= 0 or lot <= 0 or price <= 0 or size_cr <= 0:
        return 0.0, 0.0, 0.0
    retail_pool = size_cr * 1e7 * 0.35
    allot_avail = max(1, int(retail_pool / (lot * price)))
    total_apps = max(allot_avail + 1, int(allot_avail * sub))
    p_true = allot_avail / total_apps
    results = np.random.binomial(1, p_true, n)
    p_hat = results.mean()
    z = 1.96
    denom = 1 + z**2 / n
    center = (p_hat + z**2 / (2*n)) / denom
    spread = (z * np.sqrt(p_hat*(1-p_hat)/n + z**2/(4*n**2))) / denom
    return round(p_hat, 6), max(0, round(center - spread, 6)), min(1, round(center + spread, 6))

def compute_allotment(row):
    sub = max(0.1, row["SubscriptionTimes"])
    price = row["PriceBandUpper"]
    lot = row["LotSize"]
    size = row["IssueSizeCr"]
    gmp = row["GMP"]
    p_mc, ci_lo, ci_hi = monte_carlo_allotment(sub, lot, size, price)
    gain = gmp * price * lot
    b_odds = gain / max(1, 1500.0)
    cost = lot * price
    opt_k = 1
    kelly_pct = 0.0
    ev = 0.0
    roi = 0.0
    if p_mc > 0 and gain > 0:
        matrix = {k: 1 - (1-p_mc)**k for k in range(1, MAX_SYNDICATE+1)}
        best_k, best_ev = 1, -float('inf')
        for k, p_win in matrix.items():
            ev_ = p_win * gain - k*(cost + 500)
            if ev_ > best_ev:
                best_ev = ev_
                best_k = k
        opt_k = best_k
        p_opt = 1 - (1-p_mc)**opt_k
        if b_odds > 0:
            f = (b_odds * p_opt - (1-p_opt)) / b_odds
            kelly_pct = round(max(0, KELLY_FRACTION * f) * 100, 2)
        ev = round(p_opt * gain, 2)
        roi = round((ev / max(1, cost * opt_k)) * 100, 2)
    return AllotmentProfile(row["Symbol"], p_mc, opt_k, kelly_pct, ev, roi, (ci_lo, ci_hi))

def compute_sentiment(row):
    sub = row["SubscriptionTimes"]
    gmp = row["GMP"]
    buzz = 40.0
    if sub > 100: buzz += 30
    elif sub > 50: buzz += 20
    if gmp > 0.40: buzz += 20
    comp = min(100, buzz)
    label = "BULLISH" if comp >= 65 else "NEUTRAL" if comp >= 45 else "BEARISH"
    return SentimentProfile(row["Symbol"], comp, label)

def run_shariah(row):
    gmp = row["GMP"]
    sub = row["SubscriptionTimes"]
    size = row["IssueSizeCr"]
    barakah = 100.0
    issues = []
    najash = gmp > 0.40 and sub > 80
    if najash:
        barakah -= 25
        issues.append("Najash alert: GMP>40% & Sub>80x")
    if size < 20:
        barakah -= 15
        issues.append("Microcap risk (< ₹20Cr)")
    halal_score = max(0, min(100, barakah))
    tier = "TIER_1_SHARIAH" if halal_score >= 80 else "TIER_2_CONDITIONAL"
    qabda = "Settlement required (T+2) before resale."
    return ShariahVerdict(row["Symbol"], tier, halal_score, najash, qabda, issues, halal_score)

def bayesian_weights(df):
    if df.empty:
        return BASE_WEIGHTS.copy()
    avg_sub = df["SubscriptionTimes"].mean()
    w = BASE_WEIGHTS.copy()
    if avg_sub > 80:
        w["sub"] = min(0.38, w["sub"]+0.10)
        w["gmp"] = max(0.12, w["gmp"]-0.05)
    elif avg_sub < 15:
        w["gmp"] = min(0.32, w["gmp"]+0.10)
        w["sub"] = max(0.18, w["sub"]-0.10)
    total = sum(w.values())
    return {k: round(v/total,6) for k,v in w.items()}

def master_score(row, allot, sent, shariah, w):
    days = max(0, row["DaysToClose"])
    tf = 1.0 if days >= 7 else (0.5 + 0.5*days/7)
    s_gmp = min(100, row["GMP"]*200)
    s_sub = min(100, (row["SubscriptionTimes"]/100.0)*100) * tf
    s_sent = sent.composite
    s_size = 100 if row["IssueSizeCr"] <= 20 else 80 if row["IssueSizeCr"] <= 50 else 50 if row["IssueSizeCr"] <= 100 else 20
    s_halal = shariah.halal_score
    raw = (s_gmp * w["gmp"] + s_sub * w["sub"] + s_sent * w["sentiment"] +
           50 * w["trend"] + s_size * w["size"] + s_halal * w["halal"])
    final = min(100, max(0, round(raw,1)))
    verdict = "🔥 PEARL" if final >= 80 else "✅ STRONG" if final >= 70 else "📈 MODERATE" if final >= 60 else "❌ SKIP"
    return {"FinalScore": final, "Verdict": verdict}

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE & TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ipo_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT, symbol TEXT, sector TEXT, final_score REAL, verdict TEXT,
                subscription_x REAL, gmp_pct REAL, issue_size_cr REAL, price_upper REAL, lot_size INTEGER,
                close_date TEXT, days_to_close INTEGER,
                p_single_mc REAL, ci_lo REAL, ci_hi REAL, optimal_syndicate INTEGER,
                kelly_pct REAL, ev_inr REAL, roi_pct REAL,
                sentiment_score REAL, sentiment_label TEXT,
                barakah REAL, halal_tier TEXT, najash_alert INTEGER,
                source TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_date, symbol)
            )
        """)
    log.info("Database ready.")

def _tg_send(text, token, chat_id):
    text = text[:4096]
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                          timeout=12)
        if r.status_code != 200:
            log.warning(f"Telegram error: {r.text[:200]}")
    except Exception as e:
        log.error(f"Telegram send: {e}")

def send_telegram_alerts(df, allots, sents, shariahs, top_n=5):
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    console_only = not (token and chat_id)
    ranked = df.sort_values("FinalScore", ascending=False)
    date_str = TODAY.strftime("%d %b %Y")
    # summary
    summary = f"⚔️ {VERSION}\n📅 {date_str} | {len(df)} IPOs\n━━━━━━━━━━━━━━━━━━━━\n"
    for _, r in ranked.iterrows():
        summary += f"{r['Verdict']} <b>{r['Symbol']}</b> ({r['FinalScore']:.0f})  {r['SubscriptionTimes']:.1f}x | GMP {r['gmp_pct']:.1f}%\n"
    if console_only:
        print("\n[TELEGRAM SUMMARY]\n", summary)
    else:
        _tg_send(summary, token, chat_id)
        time.sleep(0.5)
    # detailed top N
    if len(ranked) > 0:
        detail = f"\n🔥 Top {top_n} IPOs – Details\n━━━━━━━━━━━━━━━━━━━━\n"
        for _, r in ranked.head(top_n).iterrows():
            a = allots[r["Symbol"]]
            s = sents[r["Symbol"]]
            sh = shariahs[r["Symbol"]]
            detail += (
                f"<b>{r['Symbol']}</b> [{r['Sector']}]  Score: {r['FinalScore']:.1f}  {r['Verdict']}\n"
                f"   Sub: {r['SubscriptionTimes']:.1f}× | GMP: {r['gmp_pct']:.1f}% | Size: ₹{r['IssueSizeCr']:.0f}Cr | Lot: {r['LotSize']}\n"
                f"   Price: ₹{r['PriceBandLower']:.0f}–₹{r['PriceBandUpper']:.0f} | Closes: {r['CloseDate']} ({r['DaysToClose']}d)\n"
                f"   P(Allot): {a.p_single_mc*100:.3f}% | Synd: {a.optimal_syndicate} PANs | Kelly: {a.kelly_pct:.1f}% | EV: ₹{a.ev_inr:,.0f} | ROI: {a.roi_pct:.2f}%\n"
                f"   Sentiment: {s.label} ({s.composite:.0f}) | {sh.tier} (Barakah: {sh.barakah:.0f})\n"
                f"   📜 {sh.qabda}\n━━━━━━━━━━━━━━━━━━━━\n"
            )
        if console_only:
            print("[TELEGRAM DETAILS]\n", detail)
        else:
            _tg_send(detail, token, chat_id)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run():
    log.info(f"🚀 {VERSION} [{TODAY}]")
    init_db()
    df = fetch_ipo_calendar(use_playwright=True)
    if df.empty:
        log.error("No IPO data. Exiting.")
        return
    log.info(f"📦 Analysing {len(df)} IPOs")
    weights = bayesian_weights(df)
    log.info(f"⚖️ Weights: {weights}")
    allots = {}
    sents = {}
    shariahs = {}
    scores = []
    for _, row in df.iterrows():
        sym = row["Symbol"]
        allots[sym] = compute_allotment(row)
        sents[sym] = compute_sentiment(row)
        shariahs[sym] = run_shariah(row)
        scores.append(master_score(row, allots[sym], sents[sym], shariahs[sym], weights))
    df["FinalScore"] = [s["FinalScore"] for s in scores]
    df["Verdict"] = [s["Verdict"] for s in scores]
    df["p_single_mc"] = [allots[s].p_single_mc for s in df["Symbol"]]
    df["optimal_syndicate"] = [allots[s].optimal_syndicate for s in df["Symbol"]]
    df["kelly_pct"] = [allots[s].kelly_pct for s in df["Symbol"]]
    df["ev_inr"] = [allots[s].ev_inr for s in df["Symbol"]]
    df["roi_pct"] = [allots[s].roi_pct for s in df["Symbol"]]
    df["sentiment_label"] = [sents[s].label for s in df["Symbol"]]
    df["barakah"] = [shariahs[s].barakah for s in df["Symbol"]]
    df["halal_tier"] = [shariahs[s].tier for s in df["Symbol"]]
    df["najash_alert"] = [shariahs[s].najash for s in df["Symbol"]]
    # persist
    with sqlite3.connect(str(DB_PATH)) as con:
        for _, r in df.iterrows():
            con.execute("""
                INSERT OR REPLACE INTO ipo_scans (
                    run_date, symbol, sector, final_score, verdict,
                    subscription_x, gmp_pct, issue_size_cr, price_upper, lot_size,
                    close_date, days_to_close,
                    p_single_mc, ci_lo, ci_hi, optimal_syndicate,
                    kelly_pct, ev_inr, roi_pct,
                    sentiment_score, sentiment_label,
                    barakah, halal_tier, najash_alert, source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                TODAY.strftime("%Y-%m-%d"), r["Symbol"], r["Sector"], r["FinalScore"], r["Verdict"],
                r["SubscriptionTimes"], r["gmp_pct"], r["IssueSizeCr"], r["PriceBandUpper"], int(r["LotSize"]),
                r["CloseDate"], int(r["DaysToClose"]),
                r["p_single_mc"], allots[r["Symbol"]].ci_95[0], allots[r["Symbol"]].ci_95[1], r["optimal_syndicate"],
                r["kelly_pct"], r["ev_inr"], r["roi_pct"],
                sents[r["Symbol"]].composite, r["sentiment_label"],
                r["barakah"], r["halal_tier"], int(r["najash_alert"]), r.get("Source", "unknown"),
            ))
    # console table
    ranked = df.sort_values("FinalScore", ascending=False)
    print(f"\n{'═'*90}")
    print(f"  {VERSION}  |  {TODAY}")
    print(f"{'═'*90}")
    print(f"  {'Symbol':<30} {'Score':>6}  {'Verdict':<14}  {'Sub':>7}  {'GMP':>6}  {'Lot':>5}  {'Days':>4}  {'Synd':>4}  {'Halal'}")
    for _, r in ranked.iterrows():
        print(f"  {r['Symbol']:<30} {r['FinalScore']:>6.1f}  {r['Verdict']:<14}  {r['SubscriptionTimes']:>6.1f}×  {r['gmp_pct']:>5.1f}%  {r['LotSize']:>5}  {r['DaysToClose']:>4}  {r['optimal_syndicate']:>4}  {r['halal_tier']}")
    print(f"{'═'*90}\n")
    # Telegram
    send_telegram_alerts(df, allots, sents, shariahs, top_n=5)
    # JSON export
    JSON_EXPORT.parent.mkdir(parents=True, exist_ok=True)
    df.to_json(str(JSON_EXPORT), orient="records", indent=2)
    log.info("🏁 IPO Sniper v5.0 complete.")

if __name__ == "__main__":
    run()
