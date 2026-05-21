#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║        IPO SNIPER v5.7 — OPEN-ONLY TELEGRAM + UPCOMING FIX + NSE STEALTH  ║
║                                                                              ║
║  FIXES vs v5.6:                                                              ║
║  N. Telegram still sent listed/allotted IPOs → root cause was in            ║
║     _parse_ig_soup(): the raw symbol cell from Investorgain embeds status   ║
║     codes BEFORE _clean_symbol strips them:                                  ║
║       "L" suffix (e.g. "BSE SMEL") = Listed → now SKIPPED at parse time    ║
║       "C" suffix or "Allotted" text = Closed/Allotted → SKIPPED at source  ║
║       "U" suffix = Upcoming → IsUpcoming=True                               ║
║       "@price (-pct%)" = listed with return shown → SKIPPED                 ║
║     Listed/allotted rows are now dropped before they ever enter the df.     ║
║  O. Upcoming ₹97–100 / 2026-06-10 everywhere → Chittorgarh upcoming page   ║
║     is a DRHP filing list with no price/date columns. _parse_html_table     ║
║     fell through to hardcoded "100" default, producing ₹97–100 for all.    ║
║     Now: upcoming rows with default-value price (≤100) get lo=hi=0 (TBD).  ║
║     Telegram shows "Price TBD / Date TBD" instead of fake ₹97–100.         ║
║  P. Source C (NSE) 403 on all endpoints → NSE uses TLS fingerprinting +    ║
║     JS challenge cookies; plain HTTP requests always get 403/404 regardless ║
║     of headers. Fixed by switching to Playwright (same as Sources A & B)    ║
║     with full browser stealth. Intercepts XHR JSON on page load.           ║
║     Falls back to HTML scrape if no JSON intercepted. Non-fatal if blocked. ║
║                                                                              ║
║  RETAINED: A–M fixes from v5.4–v5.6.                                       ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, re, math, time, json, random, logging, sqlite3, html as html_lib
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
# CONFIG
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

# ── SOURCE URLS ────────────────────────────────────────────────────────────
# FIX A: only verified-working Chittorgarh URLs kept.
# upcoming-sme-ipo returned 404; removed entirely.
# upcoming mainboard URL corrected to /report/upcoming-ipo/6/
CHITT_LIVE_URLS = {
    "Mainboard": "https://www.chittorgarh.com/report/ipo-subscription-status/10/",
    "SME":       "https://www.chittorgarh.com/report/sme-ipo-subscription-status/10/",
}
CHITT_UPCOMING_URLS = {
    "Mainboard": "https://www.chittorgarh.com/report/upcoming-ipo/6/",
    # SME upcoming removed — 404 confirmed in run log
}

# FIX C (v2): Previous "fixed" endpoints were still the deprecated ones (all 404 in run log).
# Now using the current NSE API paths confirmed live as of 2026.
# /api/live-analysis-data?index=SECURITIES%20IN%20F%26O is the current live IPO data feed.
# /api/ipo-info is the mainboard upcoming feed. emerge-live is the SME live feed.
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
    """Parse a date in any common Indian format. Returns date or None."""
    text = str(text).strip()
    # Clean artifacts like "21 May 2026 (Thu)"
    text = re.sub(r"\s*\(.*?\)", "", text).strip()
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%m-%Y", "%Y-%m-%d",
                "%b %d, %Y", "%d/%m/%Y", "%B %d, %Y", "%d %B %Y"):
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

def _clean_symbol(raw: str) -> str:
    """Strip HTML tags, normalise whitespace, title-case."""
    s = BeautifulSoup(str(raw), "html.parser").get_text(strip=True)
    s = re.sub(r"\s+", " ", s).strip()
    return s

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
SKIP_SYMBOLS = {
    "company", "name", "issuer", "no records found",
    "compare", "click here", "", "open", "closed", "upcoming",
    "sno", "sr", "sr.", "#",
}

def _make_session(referer: str = "https://www.google.com/") -> requests.Session:
    s = requests.Session()
    s.headers.update({**BROWSER_HEADERS, "Referer": referer})
    return s

# ═══════════════════════════════════════════════════════════
# TABLE COLUMN SNIFFER  (shared by HTML + AJAX parsers)
# ═══════════════════════════════════════════════════════════
def _sniff_columns(headers: List[str]) -> Dict[str, int]:
    """
    Map semantic column names → column index from a list of header strings.
    FIX D: All parsers now use this central sniffer so column positions are
    never hard-coded as 0,1,2,3… which breaks whenever the table adds/reorders cols.
    """
    col: Dict[str, int] = {}
    for i, h in enumerate(headers):
        h = h.lower().strip()
        if any(k in h for k in ("company", "issuer", "name", "ipo")):
            col.setdefault("sym", i)
        elif any(k in h for k in ("issue size", "size", "amt", "cr")):
            col.setdefault("size", i)
        elif any(k in h for k in ("price band", "price", "band", "rate")):
            col.setdefault("price", i)
        elif any(k in h for k in ("close date", "closing date", "close", "end")):
            col.setdefault("close", i)
        elif any(k in h for k in ("open date", "opening", "open")):
            col.setdefault("open", i)
        elif any(k in h for k in ("lot size", "lot", "qty", "shares")):
            col.setdefault("lot", i)
        elif "gmp" in h or "premium" in h:
            col.setdefault("gmp", i)
        elif any(k in h for k in ("subscription", "subscribed", "sub", "times", "x")):
            col.setdefault("sub", i)
    col.setdefault("sym", 0)
    return col

# ═══════════════════════════════════════════════════════════
# HTML TABLE PARSER
# ═══════════════════════════════════════════════════════════
def _parse_html_table(table, ipo_type: str, source_tag: str,
                      is_upcoming: bool = False) -> pd.DataFrame:
    sector = "Mainboard" if "main" in ipo_type.lower() else "SME"
    rows   = table.find_all("tr")
    if len(rows) < 2:
        return pd.DataFrame()

    hdr = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
    col = _sniff_columns(hdr)

    records = []
    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        def _c(key, default=""):
            idx = col.get(key)
            return cells[idx].get_text(strip=True) if idx is not None and idx < len(cells) else default

        lnk    = cells[col["sym"]].find("a")
        symbol = _clean_symbol(lnk.get_text(strip=True) if lnk
                               else cells[col["sym"]].get_text(strip=True))
        if not symbol or symbol.lower() in SKIP_SYMBOLS or len(symbol) < 2:
            continue

        size = _flt(_c("size", "50"), 50.0)
        if size > 50_000:
            size /= 1e7

        lo, hi  = _parse_price_band(_c("price", ""))
        lot     = _int(_c("lot", "")) or (1000 if sector == "SME" else 50)

        # FIX O: Chittorgarh upcoming page (/report/upcoming-ipo/) is a DRHP filing list.
        # It has no price band or lot size columns (those aren't filed yet).
        # Previous code fell through to _parse_price_band("100") default, producing
        # fake ₹97–100 bands. Now: if price parse returned defaults (lo==95, hi==100)
        # AND is_upcoming is True, treat as TBD — emit 0/0 so Telegram shows "TBD".
        if is_upcoming and hi <= 100 and lo >= 95:
            lo, hi = 0.0, 0.0   # TBD — price not yet announced

        # FIX F: real close date, never hardcoded
        close_raw = _c("close", "")
        close_dt  = _parse_date(close_raw) if close_raw else None
        if close_dt is None:
            if is_upcoming:
                # Upcoming with unknown date: use a sentinel 20-day placeholder
                # but mark it clearly so downstream can show "TBD"
                close_dt = TODAY + timedelta(days=20)
            else:
                close_dt = TODAY + timedelta(days=10)

        gmp_raw = _c("gmp", "")
        if gmp_raw:
            gmp_v = _flt(gmp_raw, 0.0)
            gmp   = gmp_v / 100 if gmp_v > 1 else gmp_v
        else:
            gmp = 0.0

        sub = _flt(_c("sub", "0"), 0.0)

        days = (close_dt - TODAY).days
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
            "DaysToClose":      days,
            "IsUpcoming":       is_upcoming,
            "Source":           source_tag,
        })

    return pd.DataFrame(records)

# ═══════════════════════════════════════════════════════════
# AJAX ROW PARSER  (used by Playwright intercept)
# ═══════════════════════════════════════════════════════════
def _parse_ajax_rows(rows_raw: list, ipo_type: str,
                     source_tag: str, is_upcoming: bool = False) -> pd.DataFrame:
    """
    FIX D: AJAX rows from Chittorgarh DataTables are list-of-lists OR list-of-dicts.
    When they're dicts we can use key names. When they're lists we still need to sniff
    the column order from the first row's count — we do NOT assume position 0=name,1=size…
    We use a header-probe request to get actual column names when possible.
    """
    if not rows_raw:
        return pd.DataFrame()

    sector = "Mainboard" if "main" in ipo_type.lower() else "SME"

    # Detect dict vs list rows
    sample = rows_raw[0]
    is_dict_rows = isinstance(sample, dict)

    records = []
    for raw in rows_raw[:80]:
        try:
            if is_dict_rows:
                # Dict rows: keys are column names (sometimes HTML-wrapped values)
                cells_clean = {k: _clean_symbol(str(v)) for k, v in raw.items()}

                # Sniff which key maps to which field
                sym_key   = next((k for k in cells_clean if any(x in k.lower() for x in ("company","name","issuer","ipo"))), None)
                size_key  = next((k for k in cells_clean if any(x in k.lower() for x in ("size","cr","amt"))), None)
                price_key = next((k for k in cells_clean if any(x in k.lower() for x in ("price","band"))), None)
                close_key = next((k for k in cells_clean if any(x in k.lower() for x in ("close","end"))), None)
                lot_key   = next((k for k in cells_clean if any(x in k.lower() for x in ("lot","qty"))), None)
                gmp_key   = next((k for k in cells_clean if "gmp" in k.lower() or "premium" in k.lower()), None)
                sub_key   = next((k for k in cells_clean if any(x in k.lower() for x in ("sub","times","subscri"))), None)

                if sym_key is None:
                    # Fall back to first key
                    sym_key = list(cells_clean.keys())[0]

                symbol = _clean_symbol(cells_clean.get(sym_key, ""))
                size   = _flt(cells_clean.get(size_key, "50") if size_key else "50", 50.0)
                lo, hi = _parse_price_band(cells_clean.get(price_key, "100") if price_key else "100")
                lot    = _int(cells_clean.get(lot_key, "") if lot_key else "") or (1000 if sector == "SME" else 50)
                close_dt = _parse_date(cells_clean.get(close_key, "") if close_key else "") or \
                           (TODAY + timedelta(days=20 if is_upcoming else 10))
                sub    = _flt(cells_clean.get(sub_key, "0") if sub_key else "0", 0.0)
                gmp_raw = cells_clean.get(gmp_key, "") if gmp_key else ""
                gmp_v  = _flt(gmp_raw, 0.0) if gmp_raw else 0.0
                gmp    = gmp_v / 100 if gmp_v > 1 else gmp_v

            else:
                # List rows: positional — clean each cell then use sniffer heuristics
                # by trying to identify which positions contain what type of data
                clean = [_clean_symbol(str(c)) for c in raw]
                if not clean or len(clean) < 2:
                    continue

                # Position 0 is almost always the company name in Chittorgarh tables
                symbol = clean[0]

                # For remaining positions: probe by content type
                size, lo, hi, lot = 50.0, 95.0, 100.0, (1000 if sector=="SME" else 50)
                sub, gmp = 0.0, 0.0
                close_dt = TODAY + timedelta(days=20 if is_upcoming else 10)

                for i, cell in enumerate(clean[1:], start=1):
                    cell = cell.strip()
                    if not cell:
                        continue
                    # Date detection
                    d = _parse_date(cell)
                    if d and i >= 3:                          # dates appear later in row
                        close_dt = d
                        continue
                    # Price band "120 - 130" or "₹120-130"
                    if re.search(r"\d+\s*[-–]\s*\d+", cell):
                        lo, hi = _parse_price_band(cell)
                        continue
                    nums = re.findall(r"[\d.]+", cell.replace(",",""))
                    if not nums:
                        continue
                    v = float(nums[0])
                    # Subscription detection: usually >1 and has 'x' or is decimal ≥0.1
                    if "x" in cell.lower() or ("." in cell and 0.1 <= v <= 500 and i >= 4):
                        sub = v
                    # Issue size: large Cr numbers or explicit label
                    elif v > 10 and v < 10_000 and size == 50.0:
                        size = v
                    # Lot size: integers typically 25-2000
                    elif v == int(v) and 10 <= v <= 5000 and lot == (1000 if sector=="SME" else 50):
                        lot = int(v)
                    # GMP: small % value
                    elif v < 5 and gmp == 0.0 and "%" in cell:
                        gmp = v / 100

            if not symbol or symbol.lower() in SKIP_SYMBOLS or len(symbol) < 2:
                continue

            if size > 50_000:
                size /= 1e7

            days = (close_dt - TODAY).days
            records.append({
                "Symbol":            _clean_symbol(symbol),
                "Sector":            sector,
                "IssueSizeCr":       round(size, 2),
                "PriceBandLower":    lo,
                "PriceBandUpper":    hi,
                "LotSize":           lot,
                "GMP":               round(gmp, 4),
                "gmp_pct":           round(gmp * 100, 2),
                "SubscriptionTimes": round(sub, 2),
                "CloseDate":         close_dt.strftime("%Y-%m-%d"),
                "DaysToClose":       days,
                "IsUpcoming":        is_upcoming,
                "Source":            source_tag + "_ajax",
            })
        except Exception as exc:
            log.debug(f"  AJAX row parse error: {exc}")
            continue

    return pd.DataFrame(records)

# ═══════════════════════════════════════════════════════════
# SOURCE A — CHITTORGARH (Playwright primary, HTTP fallback)
# ═══════════════════════════════════════════════════════════
def _fetch_chitt_playwright(url: str, ipo_type: str, source_tag: str,
                            is_upcoming: bool = False) -> pd.DataFrame:
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
                                log.info(f"  PW AJAX: {len(rows)} rows")
                        except Exception:
                            pass
            page.on("response", _on_resp)

            page.goto(url, wait_until="networkidle", timeout=55_000)
            try:
                page.wait_for_selector("table tbody tr td:not(.dataTables_empty)",
                                       timeout=15_000)
            except PWTimeout:
                pass

            if intercepted:
                browser.close()
                return _parse_ajax_rows(intercepted, ipo_type, source_tag, is_upcoming)

            # Fallback: rendered HTML
            soup = BeautifulSoup(page.content(), "html.parser")
            browser.close()
            for tbl in soup.find_all("table"):
                if len(tbl.find_all("tr")) > 3:
                    df = _parse_html_table(tbl, ipo_type,
                                           source_tag + "_html", is_upcoming)
                    if not df.empty:
                        return df
    except Exception as exc:
        log.warning(f"  PW error [{ipo_type}]: {exc}")
    return pd.DataFrame()

def _fetch_chitt_http(url: str, ipo_type: str, source_tag: str,
                      is_upcoming: bool = False) -> pd.DataFrame:
    sess = _make_session("https://www.chittorgarh.com/")
    try:
        sess.get("https://www.chittorgarh.com/", timeout=12)
        _jitter(1.5, 3.0)
        resp = sess.get(url, timeout=25)
        log.info(f"  HTTP [{ipo_type}] → {resp.status_code}")
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
                    df = _parse_html_table(tbl, ipo_type,
                                           source_tag + "_http", is_upcoming)
                    if not df.empty:
                        return df
    except Exception as exc:
        log.warning(f"  HTTP error [{ipo_type}]: {exc}")
    return pd.DataFrame()

def fetch_source_a_chittorgarh() -> pd.DataFrame:
    log.info("━━ SOURCE A: Chittorgarh ━━")
    frames: List[pd.DataFrame] = []

    # Live subscription pages (priority — only open IPOs appear here)
    for itype, url in CHITT_LIVE_URLS.items():
        tag = f"chitt_live_{itype.lower()}"
        df  = _fetch_chitt_playwright(url, itype, tag, is_upcoming=False)
        if df.empty:
            df = _fetch_chitt_http(url, itype, tag, is_upcoming=False)
        if not df.empty:
            log.info(f"  ✅ Live [{itype}]: {len(df)} rows")
            frames.append(df)
        _jitter(2.0, 4.0)

    # Upcoming (secondary — FIX A: only mainboard, SME 404 removed)
    for itype, url in CHITT_UPCOMING_URLS.items():
        tag = f"chitt_upcoming_{itype.lower()}"
        df  = _fetch_chitt_playwright(url, itype, tag, is_upcoming=True)
        if df.empty:
            df = _fetch_chitt_http(url, itype, tag, is_upcoming=True)
        if not df.empty:
            log.info(f"  ✅ Upcoming [{itype}]: {len(df)} rows (pre-open)")
            frames.append(df)
        _jitter(1.5, 3.0)

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        log.info(f"  ✅ SOURCE A raw: {len(combined)} rows")
        return combined
    log.warning("  ⚠️  SOURCE A: no data")
    return pd.DataFrame()

# ═══════════════════════════════════════════════════════════
# SOURCE B — INVESTORGAIN GMP
# ═══════════════════════════════════════════════════════════
def fetch_source_b_investorgain() -> pd.DataFrame:
    """
    FIX B: Investorgain uses a <table id="mainTable"> inside a div.
    soup.find("table") may return a nav/layout table first.
    Now explicitly selects #mainTable or falls back to largest table.
    Also adds a Playwright fallback since the page may render via JS.
    """
    log.info("━━ SOURCE B: Investorgain GMP ━━")
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/"

    def _parse_ig_soup(soup: BeautifulSoup) -> pd.DataFrame:
        # Try named table first, then largest table
        table = (soup.find("table", {"id": "mainTable"}) or
                 soup.find("table", {"id": re.compile(r"ipo|gmp", re.I)}) or
                 max(soup.find_all("table"),
                     key=lambda t: len(t.find_all("tr")), default=None))
        if not table:
            return pd.DataFrame()

        rows = table.find_all("tr")
        if len(rows) < 2:
            return pd.DataFrame()

        hdr = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        col = _sniff_columns(hdr)

        records = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells or len(cells) < 2:
                continue

            def _c(key, default=""):
                idx = col.get(key)
                return cells[idx].get_text(strip=True) if idx is not None and idx < len(cells) else default

            # FIX N: Extract raw symbol cell text BEFORE _clean_symbol strips status markers.
            # Investorgain appends status codes into the symbol cell text:
            #   "L"  = Listed (closed, showing post-listing GMP)     → SKIP
            #   "C"  = Closed / Allotted                             → SKIP
            #   "O"  = Open for subscription                         → KEEP
            #   "U"  = Upcoming (not yet open)                       → mark IsUpcoming
            # The suffix also contains listing price like "@213.10 (34.87%)" for listed ones.
            # A negative pct in brackets, e.g. "(-14.75%)" = listed with loss = definitely closed.
            sym_raw = cells[col["sym"]].get_text(strip=True)

            # Detect status from suffix codes (BSE/NSE SMEL/SMEO/SMEU/SMECT/IPOL)
            # "L" anywhere after exchange code = Listed
            ig_listed  = bool(re.search(r"(BSE|NSE)\s*SME[A-Z]*L\b", sym_raw, re.I) or
                              re.search(r"IPOL\b", sym_raw, re.I) or
                              re.search(r"@[\d.]+\s*\([+-]?[\d.]+%\)", sym_raw))
            ig_allotted = bool(re.search(r"Allotted|Allot\b", sym_raw, re.I) or
                               re.search(r"(BSE|NSE)\s*SME[A-Z]*C\b", sym_raw, re.I))
            ig_upcoming = bool(re.search(r"(BSE|NSE)\s*SME[A-Z]*U\b", sym_raw, re.I))

            # Skip listed and allotted IPOs entirely — they are NOT open for subscription
            if ig_listed or ig_allotted:
                continue

            symbol = _clean_symbol(sym_raw)
            if not symbol or len(symbol) < 3 or symbol.lower() in SKIP_SYMBOLS:
                continue

            gmp_raw = _c("gmp", "")
            gmp_v   = _flt(gmp_raw, 0.0) if gmp_raw else 0.0
            gmp     = gmp_v / 100 if gmp_v > 1 else gmp_v

            lo, hi   = _parse_price_band(_c("price", "100"))
            sub      = _flt(_c("sub", "0"), 0.0)
            size     = _flt(_c("size", "50"), 50.0)
            lot      = _int(_c("lot", "")) or 1000

            # For upcoming rows, close date is often missing — use open-ended placeholder
            close_raw = _c("close", "")
            if close_raw:
                close_dt = _parse_date(close_raw) or (TODAY + timedelta(days=20 if ig_upcoming else 7))
            else:
                close_dt = TODAY + timedelta(days=20 if ig_upcoming else 7)

            days = (close_dt - TODAY).days
            # If sub > 0 the IPO is currently open regardless of status suffix
            is_open = sub > 0.0 or (not ig_upcoming and not ig_listed and not ig_allotted)

            records.append({
                "Symbol":            symbol,
                "Sector":            "Mainboard" if (hi > 250 or lot < 200) else "SME",
                "IssueSizeCr":       round(size, 2),
                "PriceBandLower":    lo,
                "PriceBandUpper":    hi,
                "LotSize":           lot,
                "GMP":               round(gmp, 4),
                "gmp_pct":           round(gmp * 100, 2),
                "SubscriptionTimes": round(sub, 2),
                "CloseDate":         close_dt.strftime("%Y-%m-%d"),
                "DaysToClose":       days,
                "IsUpcoming":        ig_upcoming,
                "Source":            "investorgain_gmp",
            })
        return pd.DataFrame(records)

    # Try plain HTTP first
    sess = _make_session("https://www.investorgain.com/")
    try:
        resp = sess.get(url, timeout=25)
        log.info(f"  Investorgain HTTP → {resp.status_code}")
        if resp.status_code == 200:
            deny = resp.headers.get("x-deny-reason", "")
            if not deny:
                df = _parse_ig_soup(BeautifulSoup(resp.text, "html.parser"))
                if not df.empty:
                    log.info(f"  ✅ SOURCE B: {len(df)} rows")
                    return df
                log.info("  No data from HTTP parse; trying Playwright …")
    except Exception as exc:
        log.warning(f"  Investorgain HTTP error: {exc}")

    # Playwright fallback for JS-rendered tables
    if PLAYWRIGHT_OK:
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = browser.new_context(
                    user_agent=BROWSER_HEADERS["User-Agent"]).new_page()
                page.goto(url, wait_until="networkidle", timeout=45_000)
                try:
                    page.wait_for_selector("table tr td", timeout=12_000)
                except PWTimeout:
                    pass
                df = _parse_ig_soup(BeautifulSoup(page.content(), "html.parser"))
                browser.close()
                if not df.empty:
                    log.info(f"  ✅ SOURCE B (PW): {len(df)} rows")
                    return df
        except Exception as exc:
            log.warning(f"  Investorgain PW error: {exc}")

    log.warning("  ⚠️  SOURCE B: no data")
    return pd.DataFrame()

# ═══════════════════════════════════════════════════════════
# SOURCE C — NSE INDIA API
# ═══════════════════════════════════════════════════════════
def fetch_source_c_nse() -> pd.DataFrame:
    """
    FIX C (v3): NSE returns 403/404 to all HTTP clients (including requests-with-cookies)
    because it uses TLS fingerprinting + JS challenge cookies that only a real browser sets.
    The only way to hit NSE API is via Playwright with stealth mode (same as Sources A & B).

    Strategy:
      1. Use Playwright to load the NSE IPO page and intercept the API JSON response.
      2. NSE fires /api/getAllIpo or /api/ipo-detail on page load — intercept it.
      3. If no JSON intercepted, scrape the rendered HTML table as fallback.
      4. If Playwright unavailable, log clearly and return empty (Sources A+B are sufficient).
    """
    log.info("━━ SOURCE C: NSE India API ━━")

    if not PLAYWRIGHT_OK:
        log.warning("  ⚠️  SOURCE C: Playwright not available — skipping NSE")
        return pd.DataFrame()

    NSE_IPO_PAGE = "https://www.nseindia.com/market-data/upcoming-issues-ipo"
    # NSE fires one of these XHR calls when the IPO page loads
    NSE_API_PATTERNS = [
        "/api/getAllIpo", "/api/ipo-detail", "/api/ipo",
        "/api/ipo-info", "/api/emerge-live", "/api/live-analysis-data",
    ]

    records: List[dict] = []
    intercepted_data: List[dict] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                ]
            )
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1366, "height": 768},
                extra_http_headers={
                    "Accept-Language": "en-IN,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
            )
            page = ctx.new_page()

            # Intercept JSON API responses
            def _on_nse_resp(resp):
                try:
                    if any(pat in resp.url for pat in NSE_API_PATTERNS):
                        if resp.status == 200:
                            ct = resp.headers.get("content-type", "")
                            if "json" in ct:
                                body = resp.json()
                                items = (body if isinstance(body, list) else
                                         body.get("data", body.get("ipoData",
                                         body.get("allIpo", body.get("ipo", [])))))
                                if isinstance(items, list) and items:
                                    intercepted_data.extend(items)
                                    log.info(f"  NSE intercept: {len(items)} rows from {resp.url.split('/')[-1][:40]}")
                except Exception:
                    pass

            page.on("response", _on_nse_resp)

            # Warmup: hit NSE homepage to get cookies, then the IPO page
            try:
                page.goto("https://www.nseindia.com/", wait_until="domcontentloaded", timeout=30_000)
                _jitter(1.5, 2.5)
                page.goto(NSE_IPO_PAGE, wait_until="networkidle", timeout=45_000)
                _jitter(2.0, 3.0)
            except Exception as exc:
                log.warning(f"  NSE page load error: {exc}")

            if intercepted_data:
                # Parse intercepted JSON items
                seen: set = set()
                for item in intercepted_data:
                    if not isinstance(item, dict):
                        continue
                    sym = str(item.get("symbol", item.get("companyName",
                              item.get("issuerName", item.get("name", ""))))).strip()
                    if not sym or len(sym) < 2 or sym in seen:
                        continue
                    lo, hi = _parse_price_band(str(item.get("priceBand",
                                               item.get("issuePrice", "100"))))
                    size_raw = item.get("issueSize", item.get("issueSizeCrores", 50.0))
                    size = _flt(size_raw, 50.0)
                    if size > 50_000:
                        size /= 1e7
                    lot  = _int(item.get("lotSize", item.get("minBidQuantity", 0))) or 50
                    sub_raw = str(item.get("subscriptionTimes", item.get("subscriptionStatus", "0")))
                    sub  = _flt(re.search(r"[\d.]+", sub_raw).group() if re.search(r"[\d.]+", sub_raw) else "0")
                    close_dt = _parse_date(str(item.get("closeDate",
                                              item.get("biddingEndDate",
                                              item.get("closingDate", ""))))) or \
                               (TODAY + timedelta(days=10))
                    is_up = sub == 0.0 or close_dt > TODAY + timedelta(days=2)
                    seen.add(sym)
                    records.append({
                        "Symbol": sym, "Sector": "Mainboard",
                        "IssueSizeCr": round(size, 2),
                        "PriceBandLower": lo, "PriceBandUpper": hi, "LotSize": lot,
                        "GMP": 0.0, "gmp_pct": 0.0,
                        "SubscriptionTimes": round(sub, 2),
                        "CloseDate": close_dt.strftime("%Y-%m-%d"),
                        "DaysToClose": (close_dt - TODAY).days,
                        "IsUpcoming": is_up, "Source": "nse_playwright",
                    })
            else:
                # HTML scrape fallback
                soup = BeautifulSoup(page.content(), "html.parser")
                for tbl in soup.find_all("table"):
                    if len(tbl.find_all("tr")) > 3:
                        df_tbl = _parse_html_table(tbl, "Mainboard", "nse_html", is_upcoming=False)
                        if not df_tbl.empty:
                            log.info(f"  NSE HTML fallback: {len(df_tbl)} rows")
                            browser.close()
                            return df_tbl

            browser.close()

    except Exception as exc:
        log.warning(f"  NSE Playwright error: {exc}")

    df = pd.DataFrame(records)
    if not df.empty:
        log.info(f"  ✅ SOURCE C: {len(df)} rows")
    else:
        log.warning("  ⚠️  SOURCE C: no data (NSE may be blocking headless — Sources A+B are sufficient)")
    return df

# ═══════════════════════════════════════════════════════════
# FALLBACK CSV
# ═══════════════════════════════════════════════════════════
def _rebuild_fallback_csv() -> pd.DataFrame:
    FALLBACK_CSV.parent.mkdir(parents=True, exist_ok=True)
    seed = [
        {"Symbol": "Placeholder IPO Alpha", "IssueSizeCr": 70.0, "PriceBandLower": 140,
         "PriceBandUpper": 148, "LotSize": 1000, "GMP": 0.0, "SubscriptionTimes": 0.0,
         "Sector": "SME", "CloseDate": (TODAY + timedelta(3)).strftime("%Y-%m-%d"),
         "IsUpcoming": True},
        {"Symbol": "Placeholder IPO Beta",  "IssueSizeCr": 200.0, "PriceBandLower": 300,
         "PriceBandUpper": 320, "LotSize": 50,   "GMP": 0.0, "SubscriptionTimes": 0.0,
         "Sector": "Mainboard", "CloseDate": (TODAY + timedelta(5)).strftime("%Y-%m-%d"),
         "IsUpcoming": True},
    ]
    df = pd.DataFrame(seed)
    df["Source"] = "FALLBACK_SEED_PLACEHOLDER"
    df.to_csv(FALLBACK_CSV, index=False)
    log.warning("⚠️  Fallback CSV rebuilt — live fetch failed entirely.")
    return df

# ═══════════════════════════════════════════════════════════
# VALIDATION + ENRICHMENT
# ═══════════════════════════════════════════════════════════
REQUIRED_DEFAULTS = {
    "Symbol": "UNKNOWN", "Sector": "SME", "IssueSizeCr": 50.0,
    "PriceBandLower": 95.0, "PriceBandUpper": 100.0, "LotSize": 1000,
    "GMP": 0.0, "gmp_pct": 0.0, "SubscriptionTimes": 0.0,
    "CloseDate": (TODAY + timedelta(days=7)).strftime("%Y-%m-%d"),
    "DaysToClose": 7, "IsUpcoming": False, "Source": "unknown",
}

def _validate_row(row: pd.Series) -> Tuple[bool, str]:
    sym = str(row.get("Symbol", "")).strip()
    if not sym or len(sym) < 2 or sym.lower() in ("unknown", "nan", "none", ""):
        return False, "invalid_symbol"
    price = float(row.get("PriceBandUpper", 0))
    if price <= 0 or price > 200_000:
        return False, f"price_out_of_range:{price}"
    lot = int(row.get("LotSize", 0))
    if lot <= 0 or lot > 200_000:
        return False, f"lot_out_of_range:{lot}"
    days = int(row.get("DaysToClose", 0))
    if days < 0:
        return False, f"ipo_closed:{row.get('CloseDate','?')} ({days}d ago)"
    # FIX G: upcoming IPOs with no subscription data are deprioritised but kept
    # only if close date is within 30 days (don't include year-out DRHP filings)
    is_upcoming = bool(row.get("IsUpcoming", False))
    if is_upcoming and days > 30:
        return False, f"upcoming_too_far:{days}d out"
    return True, ""

def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    for col, val in REQUIRED_DEFAULTS.items():
        if col not in df.columns:
            df[col] = val

    for c in ("IssueSizeCr", "PriceBandLower", "PriceBandUpper", "LotSize",
              "GMP", "gmp_pct", "SubscriptionTimes", "DaysToClose"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(REQUIRED_DEFAULTS.get(c, 0))

    if "source" in df.columns and "Source" not in df.columns:
        df["Source"] = df["source"]
    if "IsUpcoming" not in df.columns:
        df["IsUpcoming"] = False

    df["gmp_pct"] = df["GMP"].apply(lambda g: round(float(g) * 100, 2))

    def _days(x):
        d = _parse_date(str(x))
        return (d - TODAY).days if d else -999
    df["DaysToClose"] = df["CloseDate"].apply(_days)

    valid_rows, dropped = [], 0
    for _, row in df.iterrows():
        ok, reason = _validate_row(row)
        if ok:
            valid_rows.append(row)
        else:
            dropped += 1
            log.debug(f"  Drop [{row.get('Symbol','?')}]: {reason}")

    if dropped:
        log.info(f"  🗑  Dropped {dropped} rows (closed/invalid/too-far-out)")

    return pd.DataFrame(valid_rows).reset_index(drop=True) if valid_rows else pd.DataFrame()

# ═══════════════════════════════════════════════════════════
# MASTER FETCH ORCHESTRATOR
# ═══════════════════════════════════════════════════════════
def fetch_unified_calendar() -> pd.DataFrame:
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
            log.warning("All rows dropped by validation")
        else:
            # Merge best GMP back in (highest gmp_pct per symbol wins)
            best_gmp = (enriched[enriched["gmp_pct"] > 0]
                        .sort_values("gmp_pct", ascending=False)
                        .drop_duplicates("Symbol", keep="first")[["Symbol","GMP","gmp_pct"]])

            # Deduplicate: prefer live-subscribed over upcoming, then highest sub
            enriched["_prio"] = enriched["IsUpcoming"].apply(lambda x: 1 if x else 0)
            deduped = (enriched.sort_values(["_prio","SubscriptionTimes"],
                                            ascending=[True, False])
                               .drop_duplicates("Symbol", keep="first")
                               .drop(columns=["_prio"])
                               .reset_index(drop=True))

            if not best_gmp.empty:
                deduped = (deduped.drop(columns=["GMP","gmp_pct"], errors="ignore")
                                  .merge(best_gmp, on="Symbol", how="left"))
                deduped["GMP"]     = deduped["GMP"].fillna(0.0)
                deduped["gmp_pct"] = deduped["gmp_pct"].fillna(0.0)

            live_count     = int((~deduped["IsUpcoming"]).sum())
            upcoming_count = int(deduped["IsUpcoming"].sum())
            log.info(f"✅ {len(deduped)} IPOs total: {live_count} live, {upcoming_count} upcoming")
            return deduped

    log.warning("⚠️  ALL LIVE SOURCES FAILED — using placeholder fallback")
    return _enrich(_rebuild_fallback_csv())

# ═══════════════════════════════════════════════════════════
# BAYESIAN WEIGHTS
# ═══════════════════════════════════════════════════════════
def bayesian_weight_update(df: pd.DataFrame) -> Dict[str, float]:
    w = BASE_WEIGHTS.copy()
    if df.empty:
        return w
    # Only use live-subscribed rows for regime detection
    live = df[~df["IsUpcoming"]] if "IsUpcoming" in df.columns else df
    avg_sub = live["SubscriptionTimes"].mean() if not live.empty else 1.0
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
    return {k: round(v/total, 6) for k, v in w.items()}

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

def monte_carlo_allotment(sub, lot, size_cr, price, n=MC_RUNS):
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
    spread = (z * math.sqrt(p_hat*(1-p_hat)/n + z**2/(4*n**2))) / denom
    return round(p_hat,6), max(0.0,round(center-spread,6)), min(1.0,round(center+spread,6))

def compute_allotment(row: pd.Series) -> AllotmentProfile:
    sub   = max(0.1, float(row["SubscriptionTimes"]))
    price = float(row["PriceBandUpper"])
    lot   = int(row["LotSize"])
    size  = float(row["IssueSizeCr"])
    gmp   = float(row["GMP"])

    p_mc, ci_lo, ci_hi = monte_carlo_allotment(sub, lot, size, price)
    matrix  = {k: round(1-(1-p_mc)**k, 6) for k in range(1, MAX_SYNDICATE+1)}
    gain    = gmp * price * lot
    cost    = lot * price

    # FIX M: b_odds for Kelly must reflect actual capital at risk, not a static floor.
    # In an IPO application the principal is returned if unallotted — so the true
    # downside is NOT the full lot cost. It is:
    #   (1) Opportunity cost: ~5.5% annual STCG-free return foregone during lock-up
    #       = cost * 0.055 * (days_locked / 365), where lock-up ≈ 7 days typical
    #   (2) Listing-gap risk on allotment: empirical SME/mainboard gap ~2-4% adverse
    #       = price * lot * GAP_FLOOR_PCT
    # b_odds = net_gain / effective_downside  (units: INR / INR = dimensionless ratio)
    days_locked     = max(6, int(row.get("DaysToClose", 7))) + 2   # +2 for T+2 settlement
    opp_cost        = cost * 0.055 * (days_locked / 365)           # opportunity cost in INR
    GAP_FLOOR_PCT   = 0.025                                         # 2.5% adverse listing gap
    gap_risk        = price * lot * GAP_FLOOR_PCT                   # worst-case allotment loss
    effective_risk  = max(1.0, opp_cost + gap_risk)                 # total risk INR, always > 0
    b_odds          = gain / effective_risk                          # true net odds ratio

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
    gmp, sub, size, sector, sym = (
        float(row["GMP"]), float(row["SubscriptionTimes"]),
        float(row["IssueSizeCr"]), str(row["Sector"]), str(row["Symbol"])
    )
    barakah = 100.0
    issues: List[str] = []
    najash = gmp > 0.40 and sub > 80
    if najash:
        barakah -= 25; issues.append("Najash: GMP>40% + Sub>80× (pump signal)")
    if size < 20:
        barakah -= 15; issues.append("Microcap Hazard (<₹20 Cr)")
    if sector == "SME" and sub > 200:
        barakah -= 10; issues.append("SME Hyper-Pump (Sub>200×)")
    tier  = "TIER_1_SHARIAH_COMPLIANT" if barakah >= 80 else "TIER_2_CONDITIONAL"
    qabda = ("QABDA: Hold until T+2 Demat settlement before resale. "
             "Listing-day flips = Gharar (OIC Fiqh Res. 3/3/86).")
    return ShariahVerdict(sym, tier, max(0.0, barakah), najash, qabda, issues)

def master_score(row, allot, shariah, w) -> Dict:
    days   = max(0, int(row["DaysToClose"]))
    tf     = 1.0 if days >= 7 else (0.5 + 0.5 * days / 7)
    gmp, sub, size = float(row["GMP"]), float(row["SubscriptionTimes"]), float(row["IssueSizeCr"])
    is_upcoming = bool(row.get("IsUpcoming", False))

    s_gmp  = min(100.0, gmp * 200)
    s_sub  = min(100.0, sub) * tf
    s_sent = 40.0 + (20 if sub > 50 else 10 if sub > 25 else 0) + (20 if gmp > 0.40 else 10 if gmp > 0.20 else 0)
    s_trd  = 50.0
    s_size = 100 if size <= 20 else 80 if size <= 50 else 50 if size <= 100 else 20
    s_hal  = shariah.barakah_index

    raw   = (s_gmp*w["gmp"] + s_sub*w["sub"] + s_sent*w["sentiment"] +
             s_trd*w["trend"] + s_size*w["size"] + s_hal*w["halal"])
    final = min(100.0, max(0.0, round(raw, 1)))

    # FIX G: upcoming IPOs (no subscription data yet) capped to MODERATE
    if is_upcoming and final > 59:
        final = 59.0

    verdict = ("🔥 PEARL"      if final >= 80 else
               "✅ STRONG BUY" if final >= 70 else
               "📈 MODERATE"   if final >= 60 else
               "🕐 UPCOMING"   if is_upcoming else
               "❌ SKIP")
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
                final_score REAL, verdict TEXT, is_upcoming INTEGER,
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
        # ── SCHEMA MIGRATION: add any columns that older DB versions may be missing ──
        existing_cols = {row[1] for row in con.execute("PRAGMA table_info(ipo_scans)")}
        migrations = {
            "is_upcoming":       "ALTER TABLE ipo_scans ADD COLUMN is_upcoming INTEGER DEFAULT 0",
            "source":            "ALTER TABLE ipo_scans ADD COLUMN source TEXT DEFAULT 'unknown'",
            "days_to_close":     "ALTER TABLE ipo_scans ADD COLUMN days_to_close INTEGER DEFAULT 0",
            "barakah":           "ALTER TABLE ipo_scans ADD COLUMN barakah REAL DEFAULT 0",
            "halal_tier":        "ALTER TABLE ipo_scans ADD COLUMN halal_tier TEXT DEFAULT ''",
            "najash_alert":      "ALTER TABLE ipo_scans ADD COLUMN najash_alert INTEGER DEFAULT 0",
            "optimal_syndicate": "ALTER TABLE ipo_scans ADD COLUMN optimal_syndicate INTEGER DEFAULT 1",
            "kelly_pct":         "ALTER TABLE ipo_scans ADD COLUMN kelly_pct REAL DEFAULT 0",
            "ev_inr":            "ALTER TABLE ipo_scans ADD COLUMN ev_inr REAL DEFAULT 0",
            "roi_pct":           "ALTER TABLE ipo_scans ADD COLUMN roi_pct REAL DEFAULT 0",
            "ci_lo":             "ALTER TABLE ipo_scans ADD COLUMN ci_lo REAL DEFAULT 0",
            "ci_hi":             "ALTER TABLE ipo_scans ADD COLUMN ci_hi REAL DEFAULT 0",
            "p_single_mc":       "ALTER TABLE ipo_scans ADD COLUMN p_single_mc REAL DEFAULT 0",
        }
        for col_name, ddl in migrations.items():
            if col_name not in existing_cols:
                con.execute(ddl)
                log.info(f"🗄  Migration: added column '{col_name}'")
    log.info("🗄  DB ready.")

def persist_db(df, allots, shariahs):
    date_label = TODAY.strftime("%Y-%m-%d")
    with sqlite3.connect(str(IPO_DB_PATH)) as con:
        for _, r in df.iterrows():
            sym = str(r["Symbol"])
            a, sh = allots[sym], shariahs[sym]
            con.execute("""
                INSERT OR REPLACE INTO ipo_scans (
                    run_date, symbol, sector, final_score, verdict, is_upcoming,
                    subscription_x, gmp_pct, issue_size_cr, price_upper, lot_size,
                    close_date, days_to_close,
                    p_single_mc, ci_lo, ci_hi, optimal_syndicate,
                    kelly_pct, ev_inr, roi_pct,
                    barakah, halal_tier, najash_alert, source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                date_label, sym, r["Sector"], r["FinalScore"], r["Verdict"],
                int(r.get("IsUpcoming", False)),
                r["SubscriptionTimes"], r["gmp_pct"], r["IssueSizeCr"],
                r["PriceBandUpper"], int(r["LotSize"]),
                r["CloseDate"], int(r["DaysToClose"]),
                a.p_single_mc, a.ci_95[0], a.ci_95[1], a.optimal_syndicate,
                a.kelly_pct, a.ev_inr, a.roi_pct,
                sh.barakah_index, sh.tier, int(sh.najash_alert),
                str(r.get("Source","unknown")),
            ))
    log.info(f"🗄  Persisted {len(df)} records.")

# ═══════════════════════════════════════════════════════════
# TELEGRAM — FIX E: honour retry_after, batch into one msg per IPO
# ═══════════════════════════════════════════════════════════
def _tg_send_with_retry(text: str, token: str, chat_id: str,
                        max_retries: int = 3):
    """Send one Telegram message, honouring retry_after on 429."""
    text = text[:4096]
    for attempt in range(max_retries):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=15,
            )
            if r.status_code == 200:
                return
            if r.status_code == 429:
                # FIX E: read the retry_after the server tells us to wait
                retry_after = 35
                try:
                    retry_after = r.json()["parameters"]["retry_after"]
                except Exception:
                    pass
                log.info(f"  Telegram 429 — waiting {retry_after}s (attempt {attempt+1})")
                time.sleep(retry_after + 1)
            else:
                log.warning(f"  Telegram {r.status_code}: {r.text[:80]}")
                return
        except Exception as exc:
            log.error(f"  Telegram error: {exc}")
            return

def _tg_clean_symbol(sym: str) -> str:
    """
    Strip everything after the first occurrence of exchange/listing metadata
    that investorgain appends to the symbol: 'BSE SMEL@...', 'NSE SMEO', etc.
    These suffixes contain '@', '(', '%', ')' that break Telegram HTML parsing
    even after html_lib.escape() because they arrive pre-escaped from _clean_symbol.
    """
    # Remove trailing exchange/market suffixes like "BSE SMEL@213.10 (34.87%)"
    # or "NSE SMEO" or "IPOL@104.60 (4.6%)"
    sym = re.sub(r"\s*(BSE|NSE|IPO[A-Z]?|SME[A-Z]*)\s*.*$", "", sym, flags=re.IGNORECASE).strip()
    # Remove any stray @price or (pct%) fragments
    sym = re.sub(r"@[\d.,]+\s*\([\d.%+-]+\)", "", sym).strip()
    sym = re.sub(r"\s+", " ", sym).strip()
    return sym or "UNKNOWN"

def send_telegram_alerts(df: pd.DataFrame, allots: dict, shariahs: dict):
    token   = os.getenv("TELEGRAM_TOKEN",   "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    console = not (token and chat_id)
    if console:
        log.warning("TELEGRAM_TOKEN/CHAT_ID not set — printing to console.")

    date_str = TODAY.strftime("%d %b %Y")
    ranked   = df.copy()
    ranked["IsUpcoming"] = ranked["IsUpcoming"].fillna(False).astype(bool)
    ranked   = ranked.sort_values(["IsUpcoming","FinalScore"], ascending=[True,False])

    # FIX K (v2): Listed/Allotted IPOs are now dropped at source in _parse_ig_soup (FIX N).
    # The remaining filter here is a safety net: DaysToClose must be > 0 (not already closed).
    # We also require sub > 0 OR score >= 55 to exclude zero-data stragglers that
    # slipped through with a placeholder close date.
    live_df  = ranked[
        (~ranked["IsUpcoming"]) &
        (ranked["DaysToClose"] > 0) &
        ((ranked["SubscriptionTimes"] > 0) | (ranked["FinalScore"] >= 55))
    ]
    upco_df  = ranked[ranked["IsUpcoming"]]

    log.info(f"📨  Telegram: {len(live_df)} open IPOs, {len(upco_df)} upcoming (filtered from {len(ranked)} total)")

    # ── Summary header (single message) ──────────────────────────────────
    header = (f"⚔️ <b>IPO SNIPER</b>\n"
              f"📅 <b>{date_str}</b>  |  {len(live_df)} open · {len(upco_df)} upcoming\n"
              f"{'━'*38}\n")
    for _, row in live_df.iterrows():
        clean_sym = html_lib.escape(_tg_clean_symbol(str(row['Symbol'])))
        # Verdict contains emoji + text — strip emoji for inline header to avoid parse issues
        verdict_text = re.sub(r"[^\x00-\x7F\s\w]", "", str(row['Verdict'])).strip()
        header += (f"  <b>{clean_sym}</b>"
                   f" ({row['FinalScore']:.0f}) "
                   f"{row['SubscriptionTimes']:.1f}× "
                   f"GMP {row['gmp_pct']:.1f}%\n")
    if not upco_df.empty:
        header += f"\n🕐 <b>Upcoming ({len(upco_df)})</b>\n"
        for _, row in upco_df.iterrows():
            clean_sym = html_lib.escape(_tg_clean_symbol(str(row['Symbol'])))
            lo_p, hi_p = float(row['PriceBandLower']), float(row['PriceBandUpper'])
            price_str = f"₹{lo_p:.0f}–{hi_p:.0f}" if hi_p > 0 else "Price TBD"
            close_str = str(row['CloseDate']) if row['CloseDate'] != (TODAY + timedelta(days=20)).strftime("%Y-%m-%d") else "Date TBD"
            header += (f"  <b>{clean_sym}</b>  {price_str}"
                       f"  opens {html_lib.escape(close_str)}\n")

    if console:
        print(f"\n[TELEGRAM HEADER]\n{header}")
    else:
        _tg_send_with_retry(header, token, chat_id)
        time.sleep(2.0)

    # ── One combined detail message per open live IPO ─────────────────────
    for _, row in live_df.iterrows():
        sym       = str(row["Symbol"])
        a, sh     = allots[sym], shariahs[sym]
        score     = row["FinalScore"]
        clean_sym = html_lib.escape(_tg_clean_symbol(sym))
        em        = "🔥" if score >= 80 else "✅" if score >= 70 else "📈" if score >= 60 else "⚠️"

        # FIX L: all dynamic strings passed into HTML tags must be escaped.
        # sh.tier and row['Sector'] were previously inserted raw.
        sector_safe = html_lib.escape(str(row['Sector']))
        tier_safe   = html_lib.escape(str(sh.tier))
        qabda_safe  = html_lib.escape(str(sh.qabda_mandate))
        score_label = html_lib.escape(f"{score:.1f}/100")

        msg = (
            f"{em} <b>{clean_sym}</b> [{sector_safe}]\n"
            f"   🏆 <b>{score_label}</b>\n"
            f"   📊 Sub: <b>{row['SubscriptionTimes']:.1f}×</b>"
            f"  GMP: <b>{row['gmp_pct']:.1f}%</b>"
            + ("  <i>(not yet available)</i>" if row["gmp_pct"] == 0 else "") + "\n"
            f"   💹 ₹{row['PriceBandLower']:.0f}–₹{row['PriceBandUpper']:.0f}"
            f"  Lot {row['LotSize']}  Size ₹{row['IssueSizeCr']:.0f}Cr\n"
            f"   📅 Closes: {html_lib.escape(str(row['CloseDate']))} ({row['DaysToClose']}d left)\n"
            f"   🎲 P(Allot): <b>{a.p_single_mc*100:.3f}%</b>"
            f"  [CI: {a.ci_95[0]*100:.2f}–{a.ci_95[1]*100:.2f}%]\n"
            f"   👥 Syndicate: <b>{a.optimal_syndicate} PANs</b>"
            f"  Kelly: {a.kelly_pct:.1f}%"
            f"  EV: ₹{a.ev_inr:,.0f}\n"
            f"   🕌 {tier_safe}  (Barakah {sh.barakah_index:.0f}/100)\n"
        )
        if sh.deferred_issues:
            msg += "   🚨 " + " | ".join(html_lib.escape(i) for i in sh.deferred_issues) + "\n"
        msg += f"   ⚖️ {qabda_safe}"

        if console:
            print(f"\n[TELEGRAM IPO]\n{msg}\n{'─'*55}")
        else:
            _tg_send_with_retry(msg, token, chat_id)
            time.sleep(2.0)

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def run():
    log.info(f"🚀  {VERSION}  [{TODAY}]")
    init_db()

    df = fetch_unified_calendar()
    if df.empty:
        log.error("❌ No IPO data — aborting.")
        return None

    df["IsUpcoming"] = df["IsUpcoming"].fillna(False).astype(bool)
    live_count = int((~df["IsUpcoming"]).sum())
    log.info(f"📦 Scoring {len(df)} IPOs ({live_count} live, {len(df)-live_count} upcoming) …")

    w        = bayesian_weight_update(df)
    allots:   Dict[str, AllotmentProfile] = {}
    shariahs: Dict[str, ShariahVerdict]   = {}
    scores:   List[dict]                  = []

    for _, row in df.iterrows():
        sym           = str(row["Symbol"])
        allots[sym]   = compute_allotment(row)
        shariahs[sym] = run_shariah(row)
        scores.append(master_score(row, allots[sym], shariahs[sym], w))

    df["FinalScore"]        = [s["FinalScore"]             for s in scores]
    df["Verdict"]           = [s["Verdict"]                for s in scores]
    df["p_single_mc"]       = [allots[s].p_single_mc       for s in df["Symbol"]]
    df["optimal_syndicate"] = [allots[s].optimal_syndicate for s in df["Symbol"]]
    df["kelly_pct"]         = [allots[s].kelly_pct         for s in df["Symbol"]]
    df["ev_inr"]            = [allots[s].ev_inr            for s in df["Symbol"]]
    df["roi_pct"]           = [allots[s].roi_pct           for s in df["Symbol"]]
    df["barakah"]           = [shariahs[s].barakah_index   for s in df["Symbol"]]
    df["halal_tier"]        = [shariahs[s].tier            for s in df["Symbol"]]
    df["najash_alert"]      = [shariahs[s].najash_alert    for s in df["Symbol"]]

    persist_db(df, allots, shariahs)
    JSON_EXPORT.parent.mkdir(parents=True, exist_ok=True)
    df.to_json(str(JSON_EXPORT), orient="records", indent=2)
    log.info(f"📄  JSON → {JSON_EXPORT}")

    # Console table — live IPOs first, then upcoming
    ranked = df.sort_values(["IsUpcoming","FinalScore"], ascending=[True,False])
    W = 102
    print(f"\n{'═'*W}")
    print(f"  {VERSION}  |  {TODAY}")
    print(f"{'═'*W}")
    print(f"  {'Symbol':<32} {'Score':>5}  {'Verdict':<14}  "
          f"{'Sub':>6}  {'GMP':>6}  {'Days':>4}  {'Synd':>4}  "
          f"{'Status':<10}  Source")
    print(f"  {'─'*32} {'─'*5}  {'─'*14}  "
          f"{'─'*6}  {'─'*6}  {'─'*4}  {'─'*4}  "
          f"{'─'*10}  {'─'*18}")
    for _, row in ranked.iterrows():
        sym    = str(row["Symbol"])
        a      = allots[sym]
        status = "UPCOMING" if row.get("IsUpcoming") else "LIVE"
        print(
            f"  {sym:<32} {row['FinalScore']:>5.1f}  {row['Verdict']:<14}  "
            f"{row['SubscriptionTimes']:>5.1f}×  {row['gmp_pct']:>5.1f}%  "
            f"{row['DaysToClose']:>4}  {a.optimal_syndicate:>4}  "
            f"{status:<10}  {str(row.get('Source',''))[:18]}"
        )
    print(f"{'═'*W}\n")

    send_telegram_alerts(df, allots, shariahs)
    log.info("🏁  Complete.")
    return df

if __name__ == "__main__":
    run()
