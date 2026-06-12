#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║   PROJECT FORTRESS — INCUBATOR v1.0 STONE HUNTER                           ║
║   Bismillah — In the name of Allah, the Most Gracious, the Most Merciful   ║
║                                                                              ║
║   MISSION: Find stocks at ₹40 before they become ₹150 (3-6 month horizon) ║
║                                                                              ║
║   ARCHITECTURE: Stan Weinstein Stage 1 + CANSLIM EPS + LLM Concall         ║
║   RUNS: Friday 16:00 IST (11:30 UTC) via GitHub Actions — zero VPS cost    ║
║   COMPLEMENTS: sniper_v7_eod.py (short-term swing)                         ║
║                                                                              ║
║   THREE GATES (all must pass):                                              ║
║   GATE-1  WEINSTEIN STAGE 1 BASE                                           ║
║            200MA slope flat (±3% over 13 weeks)                            ║
║            Price in tight sideways box ≥ 12 weeks                          ║
║            Box width < 35% (not a falling knife, not wild)                 ║
║            Price within 20% of 200MA (not too extended)                    ║
║                                                                              ║
║   GATE-2  EPS ACCELERATION (CANSLIM 'E')                                   ║
║            Latest QTR EPS > prior QTR EPS by ≥ +25%                       ║
║            OR revenue acceleration as proxy when EPS unavailable           ║
║            Pulls from NSE quarterly results API                            ║
║                                                                              ║
║   GATE-3  SPONGE VOLUME PROFILE                                            ║
║            Red weeks: avg volume < 60% of 20-week average (Dry Up)        ║
║            Green weeks: ≥1 week volume > 150% average (Sponge)            ║
║            Proves institutions quietly accumulating                        ║
║                                                                              ║
║   LLM CONCALL ANALYSIS (bonus gate — lifts score)                         ║
║            Scrapes NSE/BSE for latest earnings call transcript PDF         ║
║            Prompts gpt-4o-mini for CAPEX expansion + margin expansion      ║
║            +20 pts per confirmed signal                                    ║
║                                                                              ║
║   OUTPUT: Google Sheets INCUBATOR tab + Telegram weekly briefing           ║
║   HALAL: Full 4-layer screen inherited from sniper_v7 architecture         ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, io, re, json, math, time, random, logging, hashlib
import threading, warnings, subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import requests
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("incubator_v1")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONFIG
# ══════════════════════════════════════════════════════════════════════════════

VERSION = "INCUBATOR v3.0 STONE HUNTER (price-before-slice + yf-eps + 6pct-slope)"

OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
OPENAI_MINI_MODEL  = os.getenv("OPENAI_MINI_MODEL", "gpt-4o-mini")
_OPENAI_OK         = bool(OPENAI_API_KEY)

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

GOOGLE_SHEET_ID    = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_CREDS_JSON  = os.getenv("GOOGLE_CREDS_JSON", "")

SCRAPERAPI_KEY     = os.getenv("SCRAPERAPI_KEY", "")

# Stage 1 thresholds
STAGE1_MA200_FLAT_PCT   = float(os.getenv("STAGE1_MA200_FLAT_PCT",   "0.06"))   # ±6% — allows natural rounding bottoms
STAGE1_BOX_WIDTH_MAX    = float(os.getenv("STAGE1_BOX_WIDTH_MAX",    "0.35"))   # <35% box
STAGE1_BOX_WEEKS_MIN    = int(os.getenv("STAGE1_BOX_WEEKS_MIN",      "12"))     # ≥12 weeks
STAGE1_PRICE_FROM_MA200 = float(os.getenv("STAGE1_PRICE_FROM_MA200", "0.20"))   # within 20%

# EPS gate
EPS_ACCEL_PCT_MIN  = float(os.getenv("EPS_ACCEL_PCT_MIN", "0.25"))   # ≥25% QoQ EPS growth

# Sponge volume
SPONGE_DRY_VOL_PCT = float(os.getenv("SPONGE_DRY_VOL_PCT", "0.60"))  # red weeks < 60% avg
SPONGE_WET_VOL_PCT = float(os.getenv("SPONGE_WET_VOL_PCT", "1.50"))  # ≥1 green week >150% avg

# Screening
MIN_PRICE          = float(os.getenv("MIN_PRICE",          "15"))
MAX_PRICE          = float(os.getenv("MAX_PRICE",          "500"))    # Stones are cheap
MIN_TURNOVER_LAKHS = float(os.getenv("MIN_TURNOVER_LAKHS", "20"))     # lower than sniper
MAX_CANDIDATES     = int(os.getenv("MAX_CANDIDATES",       "400"))
STONE_SCORE_MIN    = int(os.getenv("STONE_SCORE_MIN",      "60"))     # /120 total
TOP_N_STONES       = int(os.getenv("TOP_N_STONES",         "5"))

OUTPUTS_DIR = Path(os.getenv("CACHE_PATH", "outputs/incubator_cache.db")).parent

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — NSE SESSION (shared pattern from sniper_v7)
# ══════════════════════════════════════════════════════════════════════════════

_NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

_NSE_SESSION_CACHE: Optional[requests.Session] = None
_NSE_SESSION_TS    = 0.0
_NSE_SESSION_LOCK  = threading.Lock()

def _get_nse_session() -> requests.Session:
    global _NSE_SESSION_CACHE, _NSE_SESSION_TS
    with _NSE_SESSION_LOCK:
        now = time.time()
        if _NSE_SESSION_CACHE and (now - _NSE_SESSION_TS) < 300:
            return _NSE_SESSION_CACHE
        ua   = random.choice(_UA_POOL)
        hdrs = {**_NSE_HEADERS, "User-Agent": ua}
        sess = requests.Session()
        try:
            r1 = sess.get("https://www.nseindia.com", headers=hdrs, timeout=12)
            log.info(f"NSE session step1: HTTP {r1.status_code}")
            time.sleep(1.2)
            r2 = sess.get("https://www.nseindia.com/api/allIndices",
                          headers={**hdrs, "Accept": "application/json",
                                   "X-Requested-With": "XMLHttpRequest"},
                          timeout=12)
            log.info(f"NSE session step2 (allIndices): HTTP {r2.status_code}")
            time.sleep(0.8)
        except Exception as e:
            log.warning(f"NSE session handshake: {e}")
        _NSE_SESSION_CACHE = sess
        _NSE_SESSION_TS    = now
        return sess

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — GOOGLE SHEETS
# ══════════════════════════════════════════════════════════════════════════════

_GS_WB: Any = None
_GS_LOCK = threading.Lock()

def _gs_ok() -> bool:
    return bool(GOOGLE_SHEET_ID and GOOGLE_CREDS_JSON)

def _get_workbook():
    global _GS_WB
    if _GS_WB:
        return _GS_WB
    with _GS_LOCK:
        if _GS_WB:
            return _GS_WB
        if not _gs_ok():
            return None
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            creds_dict = json.loads(GOOGLE_CREDS_JSON)
            scopes = ["https://www.googleapis.com/auth/spreadsheets",
                      "https://www.googleapis.com/auth/drive"]
            creds  = Credentials.from_service_account_info(creds_dict, scopes=scopes)
            gc     = gspread.authorize(creds)
            _GS_WB = gc.open_by_key(GOOGLE_SHEET_ID)
            log.info("Google Sheets connected ✅")
        except Exception as e:
            log.warning(f"Sheets connect: {e}")
    return _GS_WB

def _get_ws(tab: str):
    wb = _get_workbook()
    if not wb:
        return None
    try:
        return wb.worksheet(tab)
    except Exception:
        try:
            return wb.add_worksheet(title=tab, rows=500, cols=30)
        except Exception as e:
            log.warning(f"_get_ws {tab}: {e}")
            return None

def _push_sheet(tab: str, rows: list):
    ws = _get_ws(tab)
    if not ws or not rows:
        return
    try:
        ws.clear()
        ws.update("A1", rows, value_input_option="USER_ENTERED")
        log.info(f"Sheets {tab}: {len(rows)-1} rows ✅")
    except Exception as e:
        log.warning(f"_push_sheet {tab}: {e}")

def _read_sheet(tab: str) -> list:
    ws = _get_ws(tab)
    if not ws:
        return []
    try:
        return ws.get_all_values()
    except Exception:
        return []

def _append_row(tab: str, row: list):
    ws = _get_ws(tab)
    if not ws:
        return
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.debug(f"_append_row {tab}: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — SENTINEL + OPENAI
# ══════════════════════════════════════════════════════════════════════════════

def _write_sentinel(stage: str, extra: dict = None):
    try:
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        lines = [f"VERSION : {VERSION}",
                 f"STAGE   : {stage}",
                 f"UTCTIME : {datetime.utcnow().isoformat()}"]
        if extra:
            for k, v in extra.items():
                lines.append(f"{k:8s}: {v}")
        (OUTPUTS_DIR / "last_incubator_run.txt").write_text("\n".join(lines) + "\n")
    except Exception:
        pass

def _call_openai(prompt: str, max_tokens: int = 400) -> Optional[str]:
    if not _OPENAI_OK:
        return None
    h = hashlib.md5(prompt.encode()).hexdigest()
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": OPENAI_MINI_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": max_tokens, "temperature": 0.2},
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            elif resp.status_code == 429:
                time.sleep(2 ** attempt)
        except Exception as e:
            log.debug(f"_call_openai attempt {attempt}: {e}")
    return None

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — HALAL SCREEN (L1 keyword only — L2-L4 from sector map)
# ══════════════════════════════════════════════════════════════════════════════

_HARAM_KW = {"bank","banking","finance","financial","insurance","nbfc","mortgage",
             "alcohol","brewery","beer","liquor","tobacco","cigarette","casino",
             "gambling","lottery","weapons","defence prod","arms","ammunition"}

_SECTOR_MAP: Dict[str, str] = {
    "TCS":"IT","INFY":"IT","WIPRO":"IT","HCLTECH":"IT","TECHM":"IT",
    "SUNPHARMA":"PHARMA","DRREDDY":"PHARMA","CIPLA":"PHARMA","DIVISLAB":"PHARMA",
    "HINDUNILVR":"FMCG","ITC":"FMCG","NESTLEIND":"FMCG","BRITANNIA":"FMCG",
    "RELIANCE":"ENERGY","ONGC":"ENERGY","BPCL":"ENERGY","NTPC":"ENERGY",
    "HDFCBANK":"BANK","ICICIBANK":"BANK","SBIN":"BANK","KOTAKBANK":"BANK",
    "AXISBANK":"BANK","BAJFINANCE":"FINANCE","BAJAJFINSV":"FINANCE",
    "JSWSTEEL":"METAL","TATASTEEL":"METAL","HINDZINC":"METAL","VEDL":"METAL",
    "MARUTI":"AUTO","TATAMOTORS":"AUTO","M&M":"AUTO","MOTHERSON":"AUTO",
    "LT":"INFRA","NBCC":"INFRA","NCC":"INFRA","CONCOR":"INFRA",
    "DEEPAKNTR":"CHEMICALS","PIIND":"CHEMICALS","CHAMBLFERT":"CHEMICALS",
    "COROMANDEL":"CHEMICALS","GNFC":"CHEMICALS","TATACHEM":"CHEMICALS",
}
_HARAM_SECTORS = {"BANK","FINANCE"}

def halal_ok(symbol: str) -> bool:
    sym = symbol.upper()
    sl  = sym.lower()
    for kw in _HARAM_KW:
        if kw in sl:
            return False
    sector = _SECTOR_MAP.get(sym, "DIVERSIFIED")
    if sector in _HARAM_SECTORS:
        return False
    return True

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — BHAVCOPY (weekly — reads from Sheets BHAVCOPY tab first)
# ══════════════════════════════════════════════════════════════════════════════

def load_universe() -> pd.DataFrame:
    """
    Load full NSE EQ universe for Stone screening.
    Priority: Sheets BHAVCOPY tab → NSE bhavcopy → fallback symbol list.
    For weekly incubator, Sheets tab is always most reliable.
    """
    # Try Sheets BHAVCOPY first (populated by sniper_v7 runs)
    if _gs_ok():
        raw = _read_sheet("BHAVCOPY")
        if raw and len(raw) > 100:
            df = pd.DataFrame(raw[1:], columns=[str(h).strip().upper() for h in raw[0]])
            col_map = {}
            for internal, cands in {
                "symbol": ["SYMBOL"], "close": ["CLOSE","LTP","LAST"],
                "volume": ["VOLUME","TOTTRDQTY"], "high": ["HIGH"], "low": ["LOW"],
                "turnover_lakhs": ["TURNOVER_LAKHS","TOTTRDVAL"],
            }.items():
                for c in cands:
                    if c in df.columns:
                        col_map[c] = internal; break
            df = df.rename(columns=col_map)
            for col in ["close","volume","high","low","turnover_lakhs"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
            if "turnover_lakhs" not in df.columns:
                df["turnover_lakhs"] = df.get("volume", 0) * df.get("close", 0) / 100_000
            df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
            df = df.dropna(subset=["close"]).query("close > 0").reset_index(drop=True)

            # PATCH 1a: BAN ETFs, INDEX FUNDS, BONDS
            etf_keywords = ['ETF', 'BEES', 'QLITY', 'NIFTY', 'GSEC', 'BOND', 'LIQUIDCASE',
                            'LIQUID', 'GILT', 'CPSE', 'BHARAT', 'MAFSETF', 'JUNIORBEES']
            etf_pattern = '|'.join(etf_keywords)
            before = len(df)
            df = df[~df['symbol'].str.contains(etf_pattern, na=False)]
            log.info(f"ETF/Index filter removed {before - len(df)} symbols, {len(df)} remain")

            # PATCH 1b: PRICE FILTER BEFORE head(400) — ensures 400 affordable stocks, not large-caps
            df = df[(df["close"] >= MIN_PRICE) & (df["close"] <= MAX_PRICE)]
            log.info(f"Price filter ₹{MIN_PRICE:.0f}-{MAX_PRICE:.0f}: {len(df)} remain")

            # PATCH 1c: SORT BY LIQUIDITY (turnover), NOT ALPHABET
            if "turnover_lakhs" in df.columns:
                df = df.sort_values("turnover_lakhs", ascending=False)
                log.info("Universe sorted by turnover_lakhs (liquidity) ✅")

            df = df.head(400).reset_index(drop=True)
            log.info(f"Universe: {len(df)} rows from Sheets BHAVCOPY ✅")
            return df

    # NSE bhavcopy fallback
    try:
        today = datetime.today()
        d = today - timedelta(days=1)
        for _ in range(5):
            if d.weekday() < 5: break
            d -= timedelta(days=1)
        dd = d.strftime("%d"); mm = d.strftime("%m"); yyyy = d.strftime("%Y")
        mmm = d.strftime("%b").upper()
        url = (f"https://archives.nseindia.com/content/historical/EQUITIES/"
               f"{yyyy}/{mmm}/cm{dd}{mmm}{yyyy}bhav.csv.zip")
        sess = _get_nse_session()
        resp = sess.get(url, headers=_NSE_HEADERS, timeout=25)
        if resp.status_code == 200 and len(resp.content) > 5000:
            from zipfile import ZipFile
            zf   = ZipFile(io.BytesIO(resp.content))
            name = [n for n in zf.namelist() if n.endswith(".csv")][0]
            df   = pd.read_csv(io.BytesIO(zf.read(name)))
            df.columns = [c.strip().upper() for c in df.columns]
            if "SERIES" in df.columns:
                df = df[df["SERIES"] == "EQ"]
            df = df.rename(columns={"SYMBOL":"symbol","CLOSE":"close",
                                    "HIGH":"high","LOW":"low",
                                    "TOTTRDQTY":"volume","TOTTRDVAL":"turnover_lakhs"})
            for col in ["close","high","low","volume","turnover_lakhs"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
            df["turnover_lakhs"] = df.get("turnover_lakhs", 0) / 100_000
            df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
            df = df.dropna(subset=["close"]).query("close > 0").reset_index(drop=True)

            # PATCH 1: BAN ETFs, INDEX FUNDS, BONDS
            etf_keywords = ['ETF', 'BEES', 'QLITY', 'NIFTY', 'GSEC', 'BOND', 'LIQUIDCASE',
                            'LIQUID', 'GILT', 'CPSE', 'BHARAT', 'MAFSETF', 'JUNIORBEES']
            etf_pattern = '|'.join(etf_keywords)
            before = len(df)
            df = df[~df['symbol'].str.contains(etf_pattern, na=False)]
            log.info(f"ETF/Index filter removed {before - len(df)} symbols, {len(df)} remain")

            # PATCH 1b: PRICE FILTER BEFORE head(400) — guarantees 400 affordable candidates
            df = df[(df["close"] >= MIN_PRICE) & (df["close"] <= MAX_PRICE)]
            log.info(f"Price filter ₹{MIN_PRICE:.0f}-{MAX_PRICE:.0f}: {len(df)} remain")

            # PATCH 1: SORT BY LIQUIDITY, NOT ALPHABET
            if "turnover_lakhs" in df.columns:
                df = df.sort_values("turnover_lakhs", ascending=False)
                log.info("Universe sorted by turnover_lakhs (liquidity) ✅")

            log.info(f"Universe: {len(df)} rows from NSE bhavcopy ✅")
            return df.head(400).reset_index(drop=True)
    except Exception as e:
        log.warning(f"NSE bhavcopy: {e}")

    # Hardcoded fallback
    log.warning("Universe: using hardcoded symbol list")
    syms = [
        "RELIANCE","TCS","INFY","WIPRO","HCLTECH","TECHM","SUNPHARMA","DRREDDY",
        "CIPLA","DIVISLAB","HINDUNILVR","ITC","NESTLEIND","BRITANNIA","MARICO",
        "JSWSTEEL","TATASTEEL","HINDZINC","VEDL","MARUTI","TATAMOTORS","M&M",
        "LT","NCC","NBCC","CONCOR","DEEPAKNTR","PIIND","CHAMBLFERT","COROMANDEL",
        "GNFC","TATACHEM","NAVINFLUOR","FINEORG","ATUL","PIDILITIND","BERGEPAINT",
        "PAGEIND","RELAXO","TITAN","APOLLOHOSP","DMART","IRCTC","ADANIPORTS",
        "POLYCAB","DIXON","KAYNES","ABB","SIEMENS","CUMMINSIND","THERMAX",
        "SYNGENE","KALYANKJIL","MANINFRA","PRICOLLTD","APLLTD","SPARC","JAINREC",
        "PACEDIGITK","PINELABS","ZEEL","MOTHERSON","TMCV","WIPRO","CONCOR",
    ]
    return pd.DataFrame({"symbol": syms, "close": [100.0]*len(syms),
                         "volume": [100000]*len(syms), "high": [105.0]*len(syms),
                         "low": [95.0]*len(syms), "turnover_lakhs": [100.0]*len(syms)})

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — WEEKLY HISTORY (52 weeks)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_weekly_history(symbol: str, weeks: int = 52) -> pd.DataFrame:
    """
    Fetch weekly OHLCV from NSE historical API.
    Falls back to yfinance weekly resampling.
    Returns DataFrame with columns: date, open, high, low, close, volume
    Indexed as weekly bars.
    """
    end_dt   = datetime.today()
    start_dt = end_dt - timedelta(days=(weeks + 8) * 7)
    end_str   = end_dt.strftime("%d-%m-%Y")
    start_str = start_dt.strftime("%d-%m-%Y")

    # NSE historical API (daily) → resample to weekly
    try:
        sess = _get_nse_session()
        url  = (f"https://www.nseindia.com/api/historical/cm/equity"
                f"?symbol={symbol}&series=[%22EQ%22]"
                f"&from={start_str}&to={end_str}&csv=true")
        resp = sess.get(url, headers={**_NSE_HEADERS,
                                      "Accept": "application/json",
                                      "X-Requested-With": "XMLHttpRequest",
                                      "Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}"},
                        timeout=20)
        if resp.status_code == 200 and len(resp.content) > 200:
            data = resp.json()
            rows = data.get("data", data) if isinstance(data, dict) else data
            if rows and isinstance(rows, list):
                df = pd.DataFrame(rows)
                col_map = {}
                for c in df.columns:
                    cu = c.upper()
                    if "TIMESTAMP" in cu or "DATE" in cu: col_map[c] = "date"
                    elif "OPENING" in cu: col_map[c] = "open"
                    elif "HIGH"    in cu: col_map[c] = "high"
                    elif "LOW"     in cu: col_map[c] = "low"
                    elif "CLOSING" in cu or "CLOSE" in cu: col_map[c] = "close"
                    elif "QTY"     in cu or "VOLUME" in cu: col_map[c] = "volume"
                df = df.rename(columns=col_map)
                if all(c in df.columns for c in ["date","open","high","low","close","volume"]):
                    df["date"] = pd.to_datetime(df["date"], errors="coerce")
                    for col in ["open","high","low","close","volume"]:
                        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
                    df = df.dropna(subset=["date","close"]).sort_values("date")
                    df = df.set_index("date")
                    weekly = df[["open","high","low","close","volume"]].resample("W").agg({
                        "open":   "first",
                        "high":   "max",
                        "low":    "min",
                        "close":  "last",
                        "volume": "sum",
                    }).dropna().tail(weeks)
                    weekly = weekly.reset_index()
                    if len(weekly) >= 13:
                        log.debug(f"Weekly {symbol}: NSE_API {len(weekly)} bars")
                        return weekly
    except Exception as e:
        log.debug(f"fetch_weekly_history NSE {symbol}: {e}")

    # yfinance fallback
    try:
        import yfinance as yf
        raw = yf.download(f"{symbol}.NS", start=start_dt, end=end_dt,
                          interval="1wk", progress=False, auto_adjust=True, timeout=20)
        if not raw.empty:
            df = raw.reset_index()
            df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                          for c in df.columns]
            df["date"] = pd.to_datetime(df.get("date", df.get("datetime")))
            df = df[["date","open","high","low","close","volume"]].dropna()
            result = df.tail(weeks).reset_index(drop=True)
            log.debug(f"Weekly {symbol}: YFINANCE {len(result)} bars")
            return result
    except Exception as e:
        log.debug(f"fetch_weekly_history yfinance {symbol}: {e}")

    return pd.DataFrame()

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — GATE 1: WEINSTEIN STAGE 1 BASE
# ══════════════════════════════════════════════════════════════════════════════

def check_stage1_base(symbol: str, weekly: pd.DataFrame,
                      close: float) -> Tuple[bool, dict]:
    """
    Stan Weinstein Stage 1 = stock in flat, boring consolidation base.
    NOT an uptrend (that's Stage 2 — sniper_v7 handles those).
    NOT a downtrend (Stage 4 — avoid).
    Stage 1: 200MA flat, price in tight sideways box for ≥ 12 weeks.

    Returns (passed: bool, details: dict)
    """
    details = {"stage": "UNKNOWN", "ma200_slope_pct": 0.0, "box_width_pct": 0.0,
               "box_weeks": 0, "price_from_ma200": 0.0, "reason": ""}

    if weekly.empty or len(weekly) < 20:
        details["reason"] = f"insufficient data: {len(weekly)} weeks"
        return False, details

    close_w = weekly["close"].values.astype(float)
    high_w  = weekly["high"].values.astype(float)
    low_w   = weekly["low"].values.astype(float)

    # ── 200MA (weekly = 200-day proxy via 40-week MA) ─────────────────────────
    ma_period = min(40, len(close_w))
    ma200 = float(pd.Series(close_w).rolling(ma_period).mean().iloc[-1])
    if ma200 <= 0:
        details["reason"] = "ma200=0"
        return False, details

    # Slope: compare 13-week-ago MA to now
    if len(close_w) >= 13:
        ma200_13w_ago = float(pd.Series(close_w[:-13]).rolling(
            min(ma_period, len(close_w)-13)).mean().iloc[-1])
        slope_pct = (ma200 - ma200_13w_ago) / ma200_13w_ago if ma200_13w_ago > 0 else 1.0
    else:
        slope_pct = 1.0
    details["ma200_slope_pct"] = round(slope_pct * 100, 2)

    # Gate: slope must be flat (±STAGE1_MA200_FLAT_PCT)
    if abs(slope_pct) > STAGE1_MA200_FLAT_PCT:
        details["reason"] = (f"200MA slope={slope_pct*100:+.1f}% not flat "
                             f"(limit ±{STAGE1_MA200_FLAT_PCT*100:.0f}%)")
        details["stage"] = "STAGE2_OR_4"
        return False, details

    # ── Sideways box detection ────────────────────────────────────────────────
    # Walk back from today finding the longest contiguous window where
    # price stayed in a defined box (max_high / min_low < 1 + BOX_WIDTH_MAX)
    box_weeks = 0
    for lookback in range(min(40, len(close_w)), 0, -1):
        window_h = high_w[-lookback:]
        window_l = low_w[-lookback:]
        box_hi   = float(window_h.max())
        box_lo   = float(window_l.min())
        if box_lo > 0 and (box_hi / box_lo - 1) <= STAGE1_BOX_WIDTH_MAX:
            box_weeks = lookback
            break
    details["box_weeks"]     = box_weeks
    details["box_width_pct"] = round(
        (high_w[-box_weeks:].max() / low_w[-box_weeks:].min() - 1) * 100
        if box_weeks > 0 else 99, 1
    )

    if box_weeks < STAGE1_BOX_WEEKS_MIN:
        details["reason"] = (f"box only {box_weeks}w < min {STAGE1_BOX_WEEKS_MIN}w "
                             f"(width={details['box_width_pct']:.0f}%)")
        return False, details

    # ── Price proximity to 200MA ──────────────────────────────────────────────
    price_from_ma200 = (close - ma200) / ma200 if ma200 > 0 else 1.0
    details["price_from_ma200"] = round(price_from_ma200 * 100, 1)

    if abs(price_from_ma200) > STAGE1_PRICE_FROM_MA200:
        details["reason"] = (f"price {price_from_ma200*100:+.1f}% from 200MA "
                             f"(limit ±{STAGE1_PRICE_FROM_MA200*100:.0f}%)")
        return False, details

    # ── Score ─────────────────────────────────────────────────────────────────
    score = 0
    # Box quality: longer and tighter = better base
    score += min(25, int(box_weeks / STAGE1_BOX_WEEKS_MIN * 15))
    if details["box_width_pct"] < 15: score += 10
    elif details["box_width_pct"] < 25: score += 5
    # MA flatness: closer to 0% slope = better
    if abs(slope_pct) < 0.01: score += 10
    elif abs(slope_pct) < 0.02: score += 5
    # Price hugging 200MA (the base is forming right at institutional cost basis)
    if abs(price_from_ma200) < 0.05: score += 5

    details["stage"]  = "STAGE1"
    details["score"]  = score
    details["ma200"]  = round(ma200, 2)
    details["reason"] = (f"Stage1 ✅ box={box_weeks}w/{details['box_width_pct']:.0f}% "
                         f"slope={slope_pct*100:+.1f}% price_from_ma200={price_from_ma200*100:+.1f}%")
    return True, details

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — GATE 2: EPS ACCELERATION
# ══════════════════════════════════════════════════════════════════════════════

def fetch_quarterly_results(symbol: str) -> List[dict]:
    """
    Fetch last 4 quarters of NSE financial results.
    Returns list of dicts: [{period, eps, revenue, net_profit}]
    """
    results = []
    try:
        sess = _get_nse_session()
        # NSE corporate results API
        resp = sess.get(
            f"https://www.nseindia.com/api/quote-equity?symbol={symbol}&section=financials",
            headers={**_NSE_HEADERS, "Accept": "application/json",
                     "X-Requested-With": "XMLHttpRequest",
                     "Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}"},
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            # NSE returns financials under different keys depending on company type
            fin_data = (data.get("financials", {}) or
                        data.get("data", {}).get("financials", {}))
            quarterly = (fin_data.get("quarterly", []) or
                         fin_data.get("quarterlyResults", []))
            for q in quarterly[:4]:
                eps  = float(q.get("eps", q.get("basicEps", 0)) or 0)
                rev  = float(q.get("revenue", q.get("totalIncome", 0)) or 0)
                np_  = float(q.get("netProfit", q.get("pat", 0)) or 0)
                per  = str(q.get("period", q.get("quarter","")) or "")
                results.append({"period": per, "eps": eps,
                                 "revenue": rev, "net_profit": np_})
    except Exception as e:
        log.debug(f"fetch_quarterly_results {symbol}: {e}")

    # Screener.in fallback (public JSON endpoint, no auth needed)
    if not results:
        try:
            resp = requests.get(
                f"https://www.screener.in/api/company/{symbol}/",
                headers={"User-Agent": random.choice(_UA_POOL),
                         "Accept": "application/json"},
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                for q in (data.get("quarterly_results", []) or [])[:4]:
                    results.append({
                        "period":     str(q.get("period","")),
                        "eps":        float(q.get("eps", 0) or 0),
                        "revenue":    float(q.get("revenue", q.get("sales",0)) or 0),
                        "net_profit": float(q.get("net_profit", q.get("pat",0)) or 0),
                    })
        except Exception as e:
            log.debug(f"screener.in fallback {symbol}: {e}")

    # PATCH 2: yfinance fallback — free, unblocked, works for NSE stocks
    if not results:
        try:
            import yfinance as yf
            ticker = yf.Ticker(f"{symbol}.NS")
            q_fin = ticker.quarterly_income_stmt
            if q_fin is not None and not q_fin.empty:
                for dt in q_fin.columns[:4]:
                    net_inc = float(q_fin.loc["Net Income", dt]) if "Net Income" in q_fin.index else 0.0
                    rev     = float(q_fin.loc["Total Revenue", dt]) if "Total Revenue" in q_fin.index else 0.0
                    eps     = float(q_fin.loc["Basic EPS", dt]) if "Basic EPS" in q_fin.index else 0.0
                    results.append({
                        "period":     dt.strftime("%Y-%m-%d"),
                        "eps":        eps,
                        "revenue":    rev,
                        "net_profit": net_inc,
                    })
                log.debug(f"yfinance quarterly fallback {symbol}: {len(results)} quarters ✅")
        except Exception as e:
            log.debug(f"yfinance quarterly fallback {symbol}: {e}")

    return results

def check_eps_acceleration(symbol: str) -> Tuple[bool, dict]:
    """
    EPS acceleration gate: latest QTR EPS must be ≥ +25% above prior QTR.
    Falls back to net_profit growth if EPS unavailable.
    Returns (passed: bool, details: dict)
    """
    details = {"eps_latest": 0, "eps_prior": 0, "eps_growth_pct": 0,
               "reason": "", "score": 0}

    qtrs = fetch_quarterly_results(symbol)
    if len(qtrs) < 2:
        details["reason"] = f"insufficient quarterly data: {len(qtrs)} quarters — REJECTED"
        # PATCH 2: Hard reject — blind gamble without EPS data
        details["score"] = 0
        return False, details

    latest = qtrs[0]
    prior  = qtrs[1]

    # Use EPS if available; fall back to net_profit
    if latest["eps"] != 0 and prior["eps"] != 0:
        metric     = "EPS"
        val_latest = latest["eps"]
        val_prior  = prior["eps"]
    elif latest["net_profit"] != 0 and prior["net_profit"] != 0:
        metric     = "NET_PROFIT"
        val_latest = latest["net_profit"]
        val_prior  = prior["net_profit"]
    elif latest["revenue"] != 0 and prior["revenue"] != 0:
        metric     = "REVENUE"
        val_latest = latest["revenue"]
        val_prior  = prior["revenue"]
    else:
        details["reason"] = "no financial data available — REJECTED"
        details["score"]  = 0
        return False, details   # PATCH 2: Hard reject — no data = no trade

    # Both must be positive (no loss-making turnarounds — separate strategy)
    if val_prior <= 0:
        details["reason"] = f"{metric} prior={val_prior:.2f} ≤ 0 (loss-making)"
        return False, details

    growth_pct = (val_latest - val_prior) / abs(val_prior)
    details["eps_latest"]    = round(val_latest, 2)
    details["eps_prior"]     = round(val_prior,  2)
    details["eps_growth_pct"] = round(growth_pct * 100, 1)
    details["metric"]        = metric

    if growth_pct < EPS_ACCEL_PCT_MIN:
        details["reason"] = (f"{metric} growth {growth_pct*100:+.1f}% "
                             f"< min +{EPS_ACCEL_PCT_MIN*100:.0f}%")
        return False, details

    # Score: higher growth = more points (max 30)
    score = min(30, int(growth_pct * 100))
    details["score"]  = score
    details["reason"] = (f"EPS ✅ {metric} {growth_pct*100:+.1f}% "
                         f"latest={val_latest:.2f} prior={val_prior:.2f}")
    return True, details

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — GATE 3: SPONGE VOLUME PROFILE
# ══════════════════════════════════════════════════════════════════════════════

def check_sponge_volume(weekly: pd.DataFrame) -> Tuple[bool, dict]:
    """
    Sponge volume = institutional quiet accumulation.
    Pattern: red weeks have dry volume (< 60% avg) = nobody selling.
             green weeks have sponge volume (≥1 week > 150% avg) = someone buying.
    Proves institutions absorbing supply without moving price (Stage 1 characteristic).
    """
    details = {"dry_up_weeks": 0, "sponge_weeks": 0,
               "dry_vol_avg_ratio": 0.0, "sponge_vol_max_ratio": 0.0,
               "reason": "", "score": 0}

    if weekly.empty or len(weekly) < 10:
        details["reason"] = f"insufficient weekly data: {len(weekly)} bars"
        details["score"]  = 5
        return True, details   # soft pass

    close_w = weekly["close"].values.astype(float)
    vol_w   = weekly["volume"].values.astype(float)
    lookback = min(20, len(weekly))

    close_r = close_w[-lookback:]
    vol_r   = vol_w[-lookback:]
    avg_vol = float(vol_r.mean())
    if avg_vol <= 0:
        details["reason"] = "avg volume = 0"
        details["score"]  = 5
        return True, details

    # Red weeks = close < prior close
    red_mask   = close_r[1:] < close_r[:-1]
    green_mask = close_r[1:] >= close_r[:-1]
    red_vols   = vol_r[1:][red_mask]
    green_vols = vol_r[1:][green_mask]

    dry_vol_ratio   = float(red_vols.mean()   / avg_vol) if len(red_vols)   > 0 else 1.0
    sponge_vol_max  = float(green_vols.max()  / avg_vol) if len(green_vols) > 0 else 0.0
    dry_up_weeks    = int((vol_r[1:][red_mask] < avg_vol * SPONGE_DRY_VOL_PCT).sum())
    sponge_weeks    = int((vol_r[1:][green_mask] > avg_vol * SPONGE_WET_VOL_PCT).sum())

    details["dry_up_weeks"]      = dry_up_weeks
    details["sponge_weeks"]      = sponge_weeks
    details["dry_vol_avg_ratio"] = round(dry_vol_ratio, 3)
    details["sponge_vol_max_ratio"] = round(sponge_vol_max, 3)

    # Gate: must have meaningful dry-up AND at least one sponge week
    if dry_vol_ratio > SPONGE_DRY_VOL_PCT and sponge_weeks == 0:
        details["reason"] = (f"no sponge pattern: dry={dry_vol_ratio:.2f} "
                             f"sponge_weeks={sponge_weeks}")
        return False, details

    score = 0
    if dry_up_weeks >= 3:   score += 10
    elif dry_up_weeks >= 1: score += 5
    if sponge_weeks >= 2:   score += 20
    elif sponge_weeks >= 1: score += 12
    if dry_vol_ratio < 0.50: score += 5    # extra quiet on red days

    details["score"]  = score
    details["reason"] = (f"Sponge ✅ dry={dry_up_weeks}w({dry_vol_ratio:.2f}x) "
                         f"sponge={sponge_weeks}w(max {sponge_vol_max:.2f}x)")
    return True, details

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — CONCALL ANALYSIS (LLM bonus gate)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_concall_text(symbol: str) -> str:
    """
    Fetch latest earnings call transcript text.
    Sources: NSE/BSE filing search → PDF text extraction.
    Returns raw text string (truncated to 8000 chars for LLM).
    """
    text = ""
    # Source 1: NSE investor presentations / concall filings
    try:
        sess = _get_nse_session()
        resp = sess.get(
            f"https://www.nseindia.com/api/annual-reports?index=equities&symbol={symbol}",
            headers={**_NSE_HEADERS, "Accept": "application/json",
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=12
        )
        if resp.status_code == 200:
            filings = resp.json() if isinstance(resp.json(), list) else resp.json().get("data",[])
            for f in (filings or [])[:5]:
                subject = str(f.get("subject","") or f.get("desc","")).lower()
                if any(kw in subject for kw in ["concall","earnings call","investor call",
                                                 "con call","q1","q2","q3","q4","results"]):
                    pdf_url = f.get("fileName","") or f.get("fileLink","")
                    if pdf_url and pdf_url.endswith(".pdf"):
                        text = _extract_pdf_text(pdf_url)
                        if len(text) > 500:
                            break
    except Exception as e:
        log.debug(f"concall NSE {symbol}: {e}")

    # Source 2: BSE filings search
    if not text and SCRAPERAPI_KEY:
        try:
            target = f"https://www.bseindia.com/corporates/ann.html#{symbol}"
            resp = requests.get(
                "https://api.scraperapi.com/",
                params={"api_key": SCRAPERAPI_KEY, "url": target, "render": "false"},
                timeout=25,
            )
            if resp.status_code == 200:
                raw = resp.text[:3000]
                # Extract first PDF link containing concall keywords
                pdf_matches = re.findall(r'https?://[^\s"\']+\.pdf', raw, re.IGNORECASE)
                for url in pdf_matches[:3]:
                    t = _extract_pdf_text(url)
                    if len(t) > 500:
                        text = t
                        break
        except Exception as e:
            log.debug(f"concall BSE {symbol}: {e}")

    # PATCH 3: Source 3 — Screener.in concall page (most reliable for Indian mid-caps)
    if not text and SCRAPERAPI_KEY:
        try:
            # Screener.in concall page for this symbol
            screener_url = f"https://www.screener.in/company/{symbol}/concalls/"
            resp = requests.get(
                "https://api.scraperapi.com/",
                params={"api_key": SCRAPERAPI_KEY, "url": screener_url, "render": "false"},
                timeout=30,
            )
            if resp.status_code == 200 and len(resp.text) > 500:
                raw_html = resp.text
                # Extract transcript text — Screener wraps it in <div class="con-call">
                # or just grab all visible text between script/style tags
                clean = re.sub(r'<script[^>]*>.*?</script>', '', raw_html, flags=re.DOTALL)
                clean = re.sub(r'<style[^>]*>.*?</style>',  '', clean, flags=re.DOTALL)
                clean = re.sub(r'<[^>]+>', ' ', clean)
                clean = re.sub(r'\s+', ' ', clean).strip()
                if len(clean) > 500:
                    text = clean
                    log.info(f"Concall {symbol}: scraped Screener.in ({len(text)} chars) ✅")
        except Exception as e:
            log.debug(f"concall Screener.in {symbol}: {e}")

    return text[:8000]

def _extract_pdf_text(url: str) -> str:
    """Download PDF and extract text via pdfminer or subprocess pdftotext."""
    try:
        r = requests.get(url, headers={"User-Agent": random.choice(_UA_POOL)},
                         timeout=20)
        if r.status_code != 200 or len(r.content) < 1000:
            return ""
        # Try pdfminer
        try:
            from pdfminer.high_level import extract_text as pdf_extract
            return pdf_extract(io.BytesIO(r.content))[:8000]
        except ImportError:
            pass
        # Fallback: write to tmp and pdftotext
        tmp = Path("/tmp/concall_tmp.pdf")
        tmp.write_bytes(r.content)
        result = subprocess.run(["pdftotext", str(tmp), "-"],
                                capture_output=True, timeout=15)
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="ignore")[:8000]
    except Exception as e:
        log.debug(f"_extract_pdf_text: {e}")
    return ""

def analyze_concall(symbol: str) -> dict:
    """
    LLM analysis of earnings call transcript.
    Hunts for CAPEX expansion + margin expansion signals.
    Returns {capex_signal: bool, margin_signal: bool, summary: str, score: int}
    """
    result = {"capex_signal": False, "margin_signal": False,
              "summary": "", "score": 0}

    if not _OPENAI_OK:
        result["summary"] = "LLM disabled (no OPENAI_API_KEY)"
        return result

    text = _fetch_concall_text(symbol)
    if not text or len(text) < 300:
        result["summary"] = "No concall transcript found"
        return result

    prompt = f"""You are a quantitative analyst reading an Indian company earnings call transcript.
Company: {symbol}

Transcript (may be partial):
{text[:6000]}

Respond ONLY as JSON (no markdown):
{{
  "capex_expansion": true/false,
  "capex_detail": "one sentence or empty string",
  "margin_expansion": true/false,
  "margin_detail": "one sentence or empty string",
  "confidence": 0.0-1.0,
  "summary": "2-3 sentences max"
}}

Rules:
- capex_expansion: true ONLY if management explicitly mentions new factory, new plant, capacity expansion, greenfield, brownfield, or major capex plan with ₹ amount
- margin_expansion: true ONLY if management explicitly mentions raw material cost reduction, operating leverage improvement, or margin guidance upgrade
- Do NOT infer. Only mark true if explicitly stated."""

    raw = _call_openai(prompt, max_tokens=300)
    if raw:
        try:
            parsed = json.loads(re.sub(r"```json|```", "", raw).strip())
            result["capex_signal"]   = bool(parsed.get("capex_expansion", False))
            result["margin_signal"]  = bool(parsed.get("margin_expansion", False))
            result["summary"]        = str(parsed.get("summary",""))[:200]
            result["confidence"]     = float(parsed.get("confidence", 0.5))
            result["capex_detail"]   = str(parsed.get("capex_detail",""))[:100]
            result["margin_detail"]  = str(parsed.get("margin_detail",""))[:100]
            score = 0
            if result["capex_signal"]:  score += 20
            if result["margin_signal"]: score += 20
            result["score"] = score
            log.info(f"Concall {symbol}: capex={result['capex_signal']} "
                     f"margin={result['margin_signal']} score={score}")
        except Exception as e:
            log.debug(f"concall parse {symbol}: {e}")
    return result

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — STONE SCORER (composite 120 pts max)
# ══════════════════════════════════════════════════════════════════════════════
#
#  Stage1 base score  : 0-50  (box quality + MA flatness)
#  EPS acceleration   : 0-30  (growth magnitude)
#  Sponge volume      : 0-35  (dry-up + sponge weeks)  [note: 35 not 30]
#  Concall LLM        : 0-40  (capex +20, margin +20)
#  Total max          : 155   (pick threshold: STONE_SCORE_MIN default 60)

def score_stone(symbol: str, bhav_row: dict) -> Optional[dict]:
    """
    Full Stone pipeline. Returns result dict or None if fails any hard gate.
    """
    sym   = symbol.upper()
    close = float(bhav_row.get("close", 0))

    if close <= 0:
        return None

    # Price filter for Stones (cheap pre-discovery stocks)
    if close < MIN_PRICE or close > MAX_PRICE:
        return None

    # Halal
    if not halal_ok(sym):
        log.debug(f"Stone {sym}: halal veto")
        return None

    # Weekly history
    weekly = fetch_weekly_history(sym, weeks=52)
    if weekly.empty or len(weekly) < 13:
        log.info(f"  STONE_REJECT {sym:14s} | NO_WEEKLY_DATA bars={len(weekly)}")
        return None

    total_score = 0

    # GATE 1: Stage 1 base
    g1_ok, g1 = check_stage1_base(sym, weekly, close)
    if not g1_ok:
        log.info(f"  STONE_REJECT {sym:14s} | STAGE1_FAIL | {g1['reason']}")
        return None
    total_score += g1.get("score", 0)

    # GATE 2: EPS acceleration
    g2_ok, g2 = check_eps_acceleration(sym)
    if not g2_ok:
        log.info(f"  STONE_REJECT {sym:14s} | EPS_FAIL | {g2['reason']}")
        return None
    total_score += g2.get("score", 0)

    # GATE 3: Sponge volume
    g3_ok, g3 = check_sponge_volume(weekly)
    if not g3_ok:
        log.info(f"  STONE_REJECT {sym:14s} | SPONGE_FAIL | {g3['reason']}")
        return None
    total_score += g3.get("score", 0)

    # Minimum score check before expensive LLM call
    if total_score < STONE_SCORE_MIN * 0.5:
        log.info(f"  STONE_REJECT {sym:14s} | PRE_LLM_LOW score={total_score}")
        return None

    # LLM Concall (bonus — expensive, runs only on survivors)
    concall = {"capex_signal": False, "margin_signal": False,
               "summary": "", "score": 0}
    if _OPENAI_OK:
        try:
            concall = analyze_concall(sym)
            total_score += concall.get("score", 0)
        except Exception as e:
            log.debug(f"concall {sym}: {e}")

    if total_score < STONE_SCORE_MIN:
        log.info(f"  STONE_REJECT {sym:14s} | TOTAL_LOW score={total_score} < {STONE_SCORE_MIN}")
        return None

    # Build target: Stage 1 → Stage 2 breakout = breakout above box high
    box_high    = float(weekly["high"].tail(g1.get("box_weeks", 12)).max())
    target_6m   = round(box_high * 1.25, 2)   # 25% above box breakout
    target_12m  = round(box_high * 1.60, 2)   # 60% above
    stop_loss   = round(weekly["low"].tail(4).min() * 0.97, 2)   # 3% below 4-week low

    log.info(f"  ✅ STONE {sym:14s} | score={total_score} | "
             f"stage1={g1['box_weeks']}w | eps={g2.get('eps_growth_pct',0):+.0f}% | "
             f"sponge={g3['sponge_weeks']}w | "
             f"capex={concall['capex_signal']} margin={concall['margin_signal']}")

    return {
        "symbol":          sym,
        "close":           close,
        "total_score":     total_score,
        # Stage 1
        "stage":           g1.get("stage","STAGE1"),
        "box_weeks":       g1.get("box_weeks", 0),
        "box_width_pct":   g1.get("box_width_pct", 0),
        "ma200_slope_pct": g1.get("ma200_slope_pct", 0),
        "ma200":           g1.get("ma200", 0),
        "stage1_score":    g1.get("score", 0),
        # EPS
        "eps_growth_pct":  g2.get("eps_growth_pct", 0),
        "eps_latest":      g2.get("eps_latest", 0),
        "eps_prior":       g2.get("eps_prior", 0),
        "eps_metric":      g2.get("metric","EPS"),
        "eps_score":       g2.get("score", 0),
        # Sponge
        "dry_up_weeks":    g3.get("dry_up_weeks", 0),
        "sponge_weeks":    g3.get("sponge_weeks", 0),
        "sponge_score":    g3.get("score", 0),
        # Concall
        "capex_signal":    concall.get("capex_signal", False),
        "margin_signal":   concall.get("margin_signal", False),
        "concall_summary": concall.get("summary","")[:120],
        "concall_score":   concall.get("score", 0),
        # Targets
        "box_high":        round(box_high, 2),
        "stop_loss":       stop_loss,
        "target_25pct":    target_6m,
        "target_60pct":    target_12m,
        "upside_6m_pct":   round((target_6m  / close - 1) * 100, 1),
        "upside_12m_pct":  round((target_12m / close - 1) * 100, 1),
        "run_date":        datetime.today().strftime("%Y-%m-%d"),
    }

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 13 — TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def _send_tg(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    for attempt in range(3):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=15,
            )
            if resp.status_code == 200:
                return
            elif resp.status_code == 429:
                time.sleep(2 ** attempt)
        except Exception as e:
            log.debug(f"Telegram attempt {attempt}: {e}")

def send_telegram_stones(stones: List[dict], date_label: str, total_scanned: int):
    lines = [
        f"🪨 <b>FORTRESS INCUBATOR v1.0 — {date_label}</b>",
        f"Scanned: {total_scanned} | Stones found: {len(stones)}",
        "",
    ]
    for s in stones[:TOP_N_STONES]:
        lines += [
            f"🏆 <b>{s['symbol']}</b> — Score {s['total_score']}/120",
            f"   Close ₹{s['close']:.0f} | Box {s['box_weeks']}w/{s['box_width_pct']:.0f}%",
            f"   EPS {s['eps_growth_pct']:+.0f}% QoQ | Sponge {s['sponge_weeks']}w",
            f"   Target ₹{s['target_25pct']:.0f} (+{s['upside_6m_pct']:.0f}% in 6m)",
            f"   Stop ₹{s['stop_loss']:.0f} | "
            f"{'🏗 CAPEX' if s['capex_signal'] else ''}"
            f"{'📈 MARGIN' if s['margin_signal'] else ''}",
        ]
        if s.get("concall_summary"):
            lines.append(f"   💬 {s['concall_summary'][:80]}")
        lines.append("")
    if not stones:
        lines.append("No Stones found this week. Market base formations need more time.")
        lines.append("Incubator will re-scan next Friday. 🕐")
    _send_tg("\n".join(lines))

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 14 — GOOGLE SHEETS OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

_INCUBATOR_HEADER = [
    "Date","Symbol","Score/120","Stage1Score","EPSScore","SpongeScore","ConcallScore",
    "Close","MA200","BoxWeeks","BoxWidth%","MA200Slope%","PriceFromMA200%",
    "EPS_Growth%","EPS_Latest","EPS_Prior","EPS_Metric",
    "DryUpWeeks","SpongeWeeks",
    "CapexSignal","MarginSignal","ConcallSummary",
    "BoxHigh","StopLoss","Target25%","Target60%","Upside6m%","Upside12m%",
]

def _stone_to_row(s: dict) -> list:
    return [
        s.get("run_date",""), s.get("symbol",""),
        s.get("total_score",0), s.get("stage1_score",0),
        s.get("eps_score",0), s.get("sponge_score",0), s.get("concall_score",0),
        s.get("close",0), s.get("ma200",0),
        s.get("box_weeks",0), s.get("box_width_pct",0), s.get("ma200_slope_pct",0),
        round((s.get("close",0)/s.get("ma200",1)-1)*100,1) if s.get("ma200") else 0,
        s.get("eps_growth_pct",0), s.get("eps_latest",0), s.get("eps_prior",0),
        s.get("eps_metric","EPS"),
        s.get("dry_up_weeks",0), s.get("sponge_weeks",0),
        "✅" if s.get("capex_signal") else "", "✅" if s.get("margin_signal") else "",
        s.get("concall_summary","")[:80],
        s.get("box_high",0), s.get("stop_loss",0),
        s.get("target_25pct",0), s.get("target_60pct",0),
        s.get("upside_6m_pct",0), s.get("upside_12m_pct",0),
    ]

def push_stones_to_sheets(stones: List[dict], date_label: str):
    existing = _read_sheet("INCUBATOR")
    rows = existing if existing else [_INCUBATOR_HEADER]
    # Remove today's entries if rerun
    rows = [r for r in rows if not (len(r) > 0 and str(r[0]) == date_label)]
    if not rows:
        rows = [_INCUBATOR_HEADER]
    for s in stones:
        rows.append(_stone_to_row(s))
    _push_sheet("INCUBATOR", rows)

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 15 — MAIN RUN
# ══════════════════════════════════════════════════════════════════════════════

def run():
    log.info("=" * 70)
    log.info(f"  {VERSION}")
    log.info(f"  Weinstein Stage1 | EPS ≥+{EPS_ACCEL_PCT_MIN*100:.0f}% | Sponge Vol | LLM Concall")
    log.info(f"  Score gate: {STONE_SCORE_MIN}/120 | Top N: {TOP_N_STONES}")
    log.info("=" * 70)

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    _write_sentinel("STARTED")

    date_label = datetime.today().strftime("%Y-%m-%d")
    log.info(f"Date: {date_label}")

    # Load universe
    bhav = load_universe()
    if bhav.empty:
        log.error("Universe empty — abort")
        _send_tg(f"❌ <b>INCUBATOR v1.0 — {date_label}</b>\nUniverse unavailable.")
        return []
    _write_sentinel("UNIVERSE_LOADED", {"ROWS": len(bhav)})

    # Filter to Stone price range
    cands = bhav[
        (bhav["close"] >= MIN_PRICE) &
        (bhav["close"] <= MAX_PRICE) &
        (bhav["turnover_lakhs"] >= MIN_TURNOVER_LAKHS)
    ].head(MAX_CANDIDATES).copy()
    log.info(f"Candidates (price ₹{MIN_PRICE:.0f}-{MAX_PRICE:.0f}): {len(cands)}")

    if cands.empty:
        _send_tg(f"📋 <b>INCUBATOR v1.0 — {date_label}</b>\nNo candidates in Stone price range.")
        return []

    # Score all candidates
    stones: List[dict] = []
    total = len(cands)
    for i, (_, row) in enumerate(cands.iterrows()):
        sym = str(row.get("symbol","")).upper()
        if not sym:
            continue
        if (i + 1) % 50 == 0:
            log.info(f"Progress: {i+1}/{total} | stones found: {len(stones)}")
        try:
            result = score_stone(sym, row.to_dict())
            if result:
                stones.append(result)
        except Exception as e:
            log.debug(f"score_stone {sym}: {e}")

    # Sort by score descending
    stones.sort(key=lambda x: x["total_score"], reverse=True)
    top_stones = stones[:TOP_N_STONES]

    log.info("─" * 60)
    log.info(f"INCUBATOR SUMMARY | scanned={total} | passed={len(stones)} | "
             f"top{TOP_N_STONES}={[s['symbol'] for s in top_stones]}")
    log.info("─" * 60)

    _write_sentinel("SCORING_DONE", {
        "SCANNED ": total,
        "PASSED  ": len(stones),
        "TOP_N   ": len(top_stones),
    })

    if not top_stones:
        _send_tg(
            f"📋 <b>INCUBATOR v1.0 — {date_label}</b>\n"
            f"Scanned {total} stocks. No Stones passed all 3 gates.\n"
            f"Market in transition — Stones need more base-building time. 🕐"
        )
        return []

    # Push to Sheets
    push_stones_to_sheets(top_stones, date_label)

    # Telegram
    send_telegram_stones(top_stones, date_label, total)

    _write_sentinel("COMPLETE", {
        "STONES  ": len(top_stones),
        "SYMBOLS ": " ".join(s["symbol"] for s in top_stones),
    })

    return top_stones

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 16 — CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fortress Incubator v1.0 Stone Hunter")
    parser.add_argument("--symbol", help="Score a single symbol for debug")
    args = parser.parse_args()

    if args.symbol:
        # Debug mode: score one symbol verbatim
        logging.getLogger().setLevel(logging.DEBUG)
        sym  = args.symbol.upper()
        bhav = load_universe()
        row  = bhav[bhav["symbol"] == sym]
        if row.empty:
            print(f"{sym} not in universe — using close=100")
            result = score_stone(sym, {"symbol": sym, "close": 100.0,
                                        "volume": 100000, "turnover_lakhs": 100.0})
        else:
            result = score_stone(sym, row.iloc[0].to_dict())
        print(json.dumps(result, indent=2, default=str) if result else f"{sym}: did not pass gates")
    else:
        run()
