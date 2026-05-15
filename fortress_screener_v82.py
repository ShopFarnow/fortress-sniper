"""
╔══════════════════════════════════════════════════════════════════════╗
║   FORTRESS SCREENER v8.2 — GAP-PATCHED UNIFIED SNIPER ENGINE       ║
║   Bismillah — In the name of Allah, the Most Gracious              ║
║                                                                      ║
║   v8.1 CHANGES (over v8.0) — ALL AUDIT FINDINGS RESOLVED:         ║
║   ─────────────────────────────────────────────────────────────     ║
║   CRITICAL                                                           ║
║   FIX-AUDIT-01  MarkdownV2 _escape_md covers all 18 special chars  ║
║   FIX-AUDIT-02  HALAL_EXCLUDED checked BEFORE HALAL_LIST (safety)  ║
║   FIX-AUDIT-03  Positions table has status col; get_position        ║
║                 filters open only; close_position() added           ║
║   FIX-AUDIT-04  Earnings veto covers day-0 (0 <= days <= 2)        ║
║   FIX-AUDIT-05  yfinance multi-ticker column bug fixed (both        ║
║                 yfinance <0.2 and >=0.2 MultiIndex layouts)         ║
║   FIX-AUDIT-06  Shariah universe cache uses threading lock          ║
║   FIX-AUDIT-07  VPOC zero-volume guard; returns mid-range;          ║
║                 layer1/layer2 disabled when volume data absent      ║
║   FIX-AUDIT-08  ADX NaN guard — returns 0.0 on flat price action   ║
║                                                                      ║
║   HIGH                                                               ║
║   FIX-AUDIT-09  dyn_max capped at MAX_SCORE — phantom ceiling fixed ║
║   FIX-AUDIT-10  gspread version detection uses tuple comparison;    ║
║                 no packaging dependency required                     ║
║   FIX-AUDIT-11  vix_fog added to SNIPER_CFG as single source of    ║
║                 truth; calc_fog_enhanced uses it consistently       ║
║   FIX-AUDIT-12  VSA net-signal logic: mixed bars → NEUTRAL (0 pts) ║
║   FIX-AUDIT-13  CB_FAIL_SAFE env var — conservative block on        ║
║                 circuit-breaker data fetch failure                   ║
║   FIX-AUDIT-14  TOTTRDVAL volume derivation unit bug fixed          ║
║                 (removed erroneous ×1e5 multiplier)                 ║
║   FIX-AUDIT-15  Shared NSE session cache (_get_shared_nse_session) ║
║                 prevents 400 warmup calls in sector lookup loop     ║
║   FIX-AUDIT-16  Monte Carlo uses Student-t (df=4) for fat tails;   ║
║                 configurable via MC_FAT_TAILS env var               ║
║   FIX-AUDIT-17  SNAPSHOT_FALLBACK data-quality now set explicitly  ║
║                 when yfinance history is substituted for live data  ║
║                                                                      ║
║   MEDIUM                                                             ║
║   FIX-AUDIT-18  ROCE label corrected to "ROE(proxy)" everywhere    ║
║   FIX-AUDIT-19  Sector RS override exception sets sect_20=None;    ║
║                 boost only granted when sector data confirmed       ║
║   FIX-AUDIT-20  Shariah SQLite TTL reduced to 1 day (was 7)        ║
║                 configurable via SHARIAH_CACHE_TTL_DAYS env var     ║
║   FIX-AUDIT-21  NSE bhavcopy retry loop reuses single session;     ║
║                 no redundant warmup per attempt                     ║
║   FIX-AUDIT-22  Footer raw-string MarkdownV2 escape corrected      ║
║                                                                      ║
║   v8.2 CHANGES (over v8.1) — POST-AUDIT GAP FIXES:                 ║
║   ─────────────────────────────────────────────────────────────     ║
║   CRITICAL                                                           ║
║   FIX-GAP-01  Degraded mode: yfinance fallbacks for insider,       ║
║               filings, earnings when NSE+Sheets blocked.           ║
║               Empty earnings_cal was a silent safety failure —      ║
║               earnings veto could never fire in degraded mode.      ║
║   FIX-GAP-02  Earnings veto log elevated debug→warning; silently   ║
║               skipped symbols now visible in production logs.       ║
║   FIX-GAP-03  volume_reliable propagated into calc_cvd_divergence, ║
║               calc_momentum_exhaustion, calc_exit_liquidity;        ║
║               all three now return NEUTRAL/zero when vol absent.    ║
║                                                                      ║
║   HIGH                                                               ║
║   FIX-GAP-04  MC convergence check: two half-batch runs compared;  ║
║               gap>8pp flagged mc_converged=False in label.         ║
║   FIX-GAP-05  VDU price confirmation: VDU+price drop→distribution  ║
║               penalty (-3pts) instead of coil bonus.               ║
║   FIX-GAP-06  Sector-aware ATR multiplier: METAL×1.20, IT×0.90    ║
║               etc. Dead ≥1000 branch removed (PRICE_CAP=800).      ║
║                                                                      ║
║   MEDIUM                                                             ║
║   FIX-GAP-07  DB migration verify: PRAGMA table_info post-ALTER;   ║
║               DB-locked now raises RuntimeError, not silent pass.  ║
║   FIX-GAP-08  Telegram hard-split walks back up to 20 chars to     ║
║               avoid cutting inside a MarkdownV2 \X escape seq.     ║
║   FIX-GAP-09  Sector RS fetch failure now logged at DEBUG (was      ║
║               bare except: pass) — persistent outages now visible. ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, io, sys, time, json, logging, math, random, warnings, sqlite3
import threading, inspect
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Union

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("peewee").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.WARNING)


# ══════════════════════════════════════════════════════════════════════
# SECTION 1 — CONFIG & CONSTANTS
# ══════════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_SHARE_IDS = [
    cid.strip() for cid in os.getenv("TELEGRAM_SHARE_IDS", "").split(",")
    if cid.strip()
]
GOOGLE_SHEET_ID    = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_CREDS_JSON  = os.getenv("GOOGLE_CREDS_JSON", "")
EXCEL_OUTPUT_PATH  = Path("outputs/fortress_screener.xlsx")
HTML_OUTPUT_PATH   = Path("outputs/report.html")
DB_PATH            = Path(os.getenv("CACHE_PATH", "outputs/fortress_cache.db"))

PAPER_MODE         = os.getenv("PAPER_MODE", "false").lower() == "true"

# FIX-AUDIT-13: CB_FAIL_SAFE — when True, a failed circuit-breaker
# data fetch blocks entries conservatively (safe default=False to
# preserve backward compat; set to "true" in prod env).
CB_FAIL_SAFE = os.getenv("CB_FAIL_SAFE", "false").lower() == "true"

# FIX-AUDIT-16: MC_FAT_TAILS — use Student-t (df=4) for Monte Carlo
# simulation instead of Normal distribution. More realistic for Indian
# market returns which exhibit fat tails and circuit-breaker gaps.
MC_FAT_TAILS = os.getenv("MC_FAT_TAILS", "true").lower() == "true"
MC_FAT_TAILS_DF = int(os.getenv("MC_FAT_TAILS_DF", "4"))  # degrees of freedom

# FIX-AUDIT-20: Shariah cache TTL now configurable (was hardcoded 7 days).
# Reduced default to 1 day so Nifty500 Shariah quarterly rebalances
# are reflected within 24 hours.
SHARIAH_CACHE_TTL_DAYS = int(os.getenv("SHARIAH_CACHE_TTL_DAYS", "1"))


def _parse_positive_float(env_key: str, default: float, min_val: float, max_val: float) -> float:
    raw = os.getenv(env_key, str(default))
    try:
        val = float(raw)
    except (ValueError, TypeError):
        log.warning(f"{env_key}='{raw}' is not a valid number — using default {default}")
        return default
    if not (min_val <= val <= max_val):
        log.warning(f"{env_key}={val} outside [{min_val}, {max_val}] — clamping")
        return max(min_val, min(val, max_val))
    return val

ACCOUNT_EQUITY     = _parse_positive_float("ACCOUNT_EQUITY",   500_000.0, 1_000.0, 1_000_000_000.0)
ACCOUNT_RISK_PCT   = _parse_positive_float("ACCOUNT_RISK_PCT", 0.01,      0.001,   0.05)

FORCE_SHEETS   = os.getenv("FORCE_SHEETS",   "false").lower() == "true"
FORCE_YFINANCE = os.getenv("FORCE_YFINANCE", "false").lower() == "true"

SHEET_BHAVCOPY   = os.getenv("SHEET_BHAVCOPY",   "BHAVCOPY")
SHEET_FII_DII    = os.getenv("SHEET_FII_DII",    "FII_DII")
SHEET_INSIDER    = os.getenv("SHEET_INSIDER",    "INSIDER")
SHEET_FILINGS    = os.getenv("SHEET_FILINGS",    "FILINGS")
SHEET_EARNINGS   = os.getenv("SHEET_EARNINGS",   "EARNINGS")
SHEET_SCREENER   = os.getenv("SHEET_SCREENER",   "SCREENER")
SHEET_HALAL_LIST = os.getenv("SHEET_HALAL_LIST", "HALAL_LIST")

SCORE_WEIGHTS = dict(fortress=80, fii_dii=30, insider=30, filing=30, earnings=30)
MAX_SCORE     = sum(SCORE_WEIGHTS.values())   # 200

MID_CAP_PICKS   = 2
SMALL_CAP_PICKS = 2
PRICE_CAP       = 800

RANKS = [
    (160, "⚔️ ELITE",    "FULL 100%"),
    (130, "🟢 PRISTINE", "FULL 100%"),
    (105, "🟡 HIGH",     "HALF 50%"),
    ( 85, "🟠 MODERATE", "QTR 25%"),
    ( 65, "🔵 PROBE",    "PROBE 10%"),
    (  0, None,           None),
]

CFG = dict(
    vol_ratio        = 2.5,
    turnover_lakhs   = 150,
    atr_t2           = 1.5,
    atr_t3           = 1.75,
    alt_warn_pct     = 40.0,
    alt_stop_pct     = 60.0,
    mfi_accum        = 40,
    mfi_dist         = 60,
    recency_days     = 45,
    adx_trend        = 25.0,
    adx_range        = 18.0,
    top_n            = 5,
    max_candidates   = 200,
    ma200_tolerance  = 0.05,
    min_hist_bars    = 30,
)

SNIPER_CFG = dict(
    vix_panic          = 22.0,
    vix_chop           = 15.0,
    # FIX-AUDIT-11: vix_fog is the single source of truth for FOG engine VIX
    # threshold. Sits between chop(15) and panic(22). calc_fog_enhanced and
    # any other consumer must reference SNIPER_CFG["vix_fog"] — never hardcode.
    vix_fog            = 20.0,
    nifty_massacre     = -3.0,
    vpoc_band_pct      = 0.02,
    vpoc_weeks         = 52,
    vol_spikes_52w     = 35,
    bounce_recency     = 45,
    min_bounces        = 3,
    liquidity_mult     = 2.0,
    min_turnover_cr    = 3.0,
    alt_warn_pct       = 40.0,
    alt_stop_pct       = 60.0,
    score_pristine     = 85,
    score_good         = 70,
    score_marginal     = 58,
    score_probe        = 45,
    risk_per_trade     = 0.015,
    max_pos_pct        = 0.10,
    atr_stop_mult      = 2.0,
    be_atr_mult        = 2.0,
    exit_trail_mult    = 1.5,
    trail_trigger_pct  = 15.0,
    trail_atr_mult     = 2.5,
    score_size_blend   = 0.50,
    r1_pct             = 30.0,
    r2_pct             = 60.0,
    r3_pct             = 100.0,
    r1_sell_pct        = 30,
    r2_sell_pct        = 30,
    r3_sell_pct        = 40,
    bayes_alpha        = 0.12,
    vpoc_3m_wt         = 0.40,
    vpoc_6m_wt         = 0.35,
    vpoc_12m_wt        = 0.25,
)

SECTOR_INDICES = {
    "NIFTY IT":     "CNXIT",
    "NIFTY PHARMA": "CNXPHARMA",
    "NIFTY AUTO":   "CNXAUTO",
    "NIFTY FMCG":   "CNXFMCG",
    "NIFTY METAL":  "CNXMETAL",
}

_OVERLAPPING_KEYS = {"alt_warn_pct", "alt_stop_pct"}
for _k in _OVERLAPPING_KEYS:
    assert CFG[_k] == SNIPER_CFG[_k], (
        f"CFG['{_k}']={CFG[_k]} != SNIPER_CFG['{_k}']={SNIPER_CFG[_k]}. "
        f"Keep these in sync or remove the duplicate."
    )

SECTOR_TRUTH = {
    "NIFTY PHARMA":  1.15,
    "NIFTY IT":      1.10,
    "NIFTY AUTO":    1.00,
    "NIFTY FMCG":    0.95,
    "NIFTY METAL":   0.85,
    "NIFTY BANK":    0.00,
    "NIFTY REALTY":  0.75,
    "NIFTY ENERGY":  0.20,
    "DIVERSIFIED":   1.00,
}
SECTOR_BLOCKED = {"NIFTY BANK", "NIFTY ENERGY"}

HALAL_EXCLUDED = {
    "HDFCBANK","ICICIBANK","SBIN","KOTAKBANK","AXISBANK","INDUSINDBK",
    "BANDHANBNK","IDFCFIRSTB","FEDERALBNK","RBLBANK","BANKBARODA",
    "CANBK","UNIONBANK","PNB","INDIANB","AUBANK","DCBBANK","YESBANK",
    "BAJFINANCE","BAJAJFINSV","SBICARD","CHOLAFIN","HDFC","LICHSGFIN",
    "M&MFIN","SHRIRAMFIN","MUTHOOTFIN","MANAPPURAM","IIFL","SUNDARMFIN",
    "RECLTD","PFC","IRFC","HUDCO","PNBHOUSING",
    "HDFCLIFE","SBILIFE","ICICIPRU","LICI","STARHEALTH","GICRE","NIACL",
    "LTIM","NIFTYBEES","JUNIORBEES","GOLDBEES","BANKBEES","LIQUIDBEES",
}

HALAL_KW = (
    "bank","bancorp","finance","finserv","fincorp","financial",
    "insurance","insur","nifty","etf","reit","invit",
    "liquid","overnight","gilt","treasury",
)
import re as _re
_HALAL_KW_REGEX_EXACT = _re.compile(r'\bbees\b', _re.IGNORECASE)

SYMBOL_SECTOR = {
    "TCS":"NIFTY IT","INFY":"NIFTY IT","WIPRO":"NIFTY IT",
    "HCLTECH":"NIFTY IT","TECHM":"NIFTY IT","LTIM":"NIFTY IT",
    "MPHASIS":"NIFTY IT","COFORGE":"NIFTY IT","PERSISTENT":"NIFTY IT",
    "SUNPHARMA":"NIFTY PHARMA","DRREDDY":"NIFTY PHARMA",
    "CIPLA":"NIFTY PHARMA","DIVISLAB":"NIFTY PHARMA",
    "AUROPHARMA":"NIFTY PHARMA","LUPIN":"NIFTY PHARMA",
    "TORNTPHARM":"NIFTY PHARMA","ALKEM":"NIFTY PHARMA",
    "MARUTI":"NIFTY AUTO","TATAMOTORS":"NIFTY AUTO",
    "M&M":"NIFTY AUTO","HEROMOTOCO":"NIFTY AUTO",
    "BAJAJ-AUTO":"NIFTY AUTO","EICHERMOT":"NIFTY AUTO",
    "TVSMOTORS":"NIFTY AUTO","BOSCHLTD":"NIFTY AUTO",
    "TATASTEEL":"NIFTY METAL","JSWSTEEL":"NIFTY METAL",
    "HINDALCO":"NIFTY METAL","SAIL":"NIFTY METAL",
    "NMDC":"NIFTY METAL","VEDL":"NIFTY METAL",
    "HINDUNILVR":"NIFTY FMCG","NESTLEIND":"NIFTY FMCG",
    "BRITANNIA":"NIFTY FMCG","DABUR":"NIFTY FMCG",
    "MARICO":"NIFTY FMCG","COLPAL":"NIFTY FMCG",
    "DLF":"NIFTY REALTY","GODREJPROP":"NIFTY REALTY",
    "OBEROIRLTY":"NIFTY REALTY","PHOENIXLTD":"NIFTY REALTY",
}

_HALAL_FALLBACK_85 = {
    "TCS","INFY","WIPRO","HCLTECH","TECHM","LTIM","MPHASIS","COFORGE","PERSISTENT",
    "KPITTECH","TATAELXSI","TANLA","MASTEK","ROUTE",
    "NEWGEN","SAKSOFT","INTELLECT","DATAMATICS","ZENSAR",
    "SUNPHARMA","DRREDDY","CIPLA","DIVISLAB","AUROPHARMA","LUPIN","TORNTPHARM",
    "ALKEM","IPCALAB","NATCOPHARM","GRANULES","GLENMARK","AJANTPHARM","SYNGENE",
    "LALPATHLAB","METROPOLIS","MARKSANS","LAURUSLABS","GLAND",
    "MARUTI","TATAMOTORS","M&M","HEROMOTOCO","BAJAJ-AUTO","EICHERMOT","TVSMOTORS",
    "MOTHERSON","BOSCHLTD","ENDURANCE","APOLLOTYRE","BALKRISIND","SUPRAJIT","GABRIEL",
    "CEATLTD","CRAFTSMAN","TIINDIA",
    "HINDUNILVR","NESTLEIND","BRITANNIA","DABUR","MARICO","COLPAL","EMAMILTD",
    "TATACONSUM","VBL","JUBLFOOD","KRBL","JYOTHYLAB",
    "PIDILITIND","FINEORG","GALAXYSURF","VINATIORG","NAVINFLUOR","ALKYLAMINE",
    "DEEPAKNI","TATACHEM","GHCL","ANUPAM","PCBL","AARTI","HIMADRI",
    "ATUL","NOCIL","EPIGRAL","SUDARSCHEM","LAXMICHEM","BALAMINES",
    "LT","HAVELLS","VOLTAS","SIEMENS","ABB","CUMMINSIND","THERMAX","KEC",
    "POLYCAB","SCHAEFFLER","TIMKEN","GRINDWELL","PRAJ","ELGIEQUIP","KAYNES","SYRMA",
    "DLF","GODREJPROP","OBEROIRLTY","PHOENIXLTD","SOBHA",
    "CONCOR","BLUEDART","TCI","DELHIVERY","ALLCARGO","GATI","AEGISLOG",
    "KAVERI","DHANUKA","UPL","PIIND","AVANTIFEED","COROMANDEL","CHAMBLFERT","GSFC",
    "PAGEIND","RAYMOND","WELSPUNIND","VARDHMAN","TRIDENT","KPRMILL",
    "TATASTEEL","HINDALCO","JSWSTEEL","NMDC","RATNAMANI",
    "TITAN","TRENT","ASIANPAINT","BERGERPAINTS","DIXON","AMBER",
    "SUZLON","INOXWIND","WEBELSOLAR","TATAPOWER","TORNTPOWER",
}
_HALAL_FALLBACK     = _HALAL_FALLBACK_85
_HALAL_FALLBACK_150 = _HALAL_FALLBACK_85

_YF_UNIVERSE_150 = [
    "TCS","INFY","WIPRO","HCLTECH","TECHM","LTIM","MPHASIS","COFORGE",
    "PERSISTENT","KPITTECH","TATAELXSI","ROUTE","TANLA","MASTEK",
    "NEWGEN","SAKSOFT","INTELLECT","DATAMATICS","ZENSAR",
    "SUNPHARMA","DRREDDY","CIPLA","DIVISLAB","AUROPHARMA","LUPIN",
    "TORNTPHARM","ALKEM","IPCALAB","NATCOPHARM","GRANULES","GLENMARK",
    "AJANTPHARM","LALPATHLAB","METROPOLIS","SYNGENE","MARKSANS",
    "MARUTI","TATAMOTORS","M&M","HEROMOTOCO","BAJAJ-AUTO","EICHERMOT",
    "TVSMOTORS","MOTHERSON","BOSCHLTD","ENDURANCE","APOLLOTYRE","CEATLTD",
    "BALKRISIND","SUPRAJIT","GABRIEL","CRAFTSMAN","TIINDIA",
    "HINDUNILVR","NESTLEIND","BRITANNIA","DABUR","MARICO","COLPAL",
    "EMAMILTD","TATACONSUM","VBL","JUBLFOOD","KRBL","JYOTHYLAB",
    "PIDILITIND","FINEORG","GALAXYSURF","VINATIORG","NAVINFLUOR",
    "ALKYLAMINE","DEEPAKNI","TATACHEM","GHCL","ANUPAM","PCBL",
    "AARTI","HIMADRI","EPIGRAL","ATUL","NOCIL",
    "LT","HAVELLS","VOLTAS","SIEMENS","ABB","CUMMINSIND",
    "THERMAX","KEC","POLYCAB","SCHAEFFLER","TIMKEN",
    "GRINDWELL","PRAJ","ELGIEQUIP","KAYNES","SYRMA",
    "DLF","GODREJPROP","OBEROIRLTY","PHOENIXLTD","SOBHA",
    "CONCOR","BLUEDART","TCI","DELHIVERY","ALLCARGO",
    "KAVERI","DHANUKA","UPL","PIIND","COROMANDEL",
    "PAGEIND","RAYMOND","WELSPUNIND","VARDHMAN","TRIDENT",
    "TATASTEEL","HINDALCO","JSWSTEEL","NMDC","RATNAMANI",
    "TITAN","TRENT","ASIANPAINT","BERGERPAINTS","DIXON","AMBER",
    "NTPC","TATAPOWER","TORNTPOWER","SUZLON","INOXWIND",
]

# FIX-AUDIT-06: threading lock for Shariah universe cache write.
_SHARIAH_UNIVERSE_LOCK  = threading.Lock()
_SHARIAH_UNIVERSE_CACHE: Optional[set] = None
_SECTOR_LIVE_CACHE: dict = {}
_MACRO_REGIME_CACHE: Optional[Dict] = None
_MACRO_REGIME_LOCK  = threading.Lock()
_smallcap_index_cache: dict = {}
_ROCE_CACHE_TTL_SECONDS = 86_400
_roce_cache: dict = {}
_HALAL_LIST_CUSTOM: set = set()

# FIX-GAP-01: alias used by degraded-mode fallbacks in fetch_insider_trades,
# fetch_recent_filings, and fetch_earnings_calendar so they can probe a
# representative subset of symbols via yfinance when NSE + Sheets are blocked.
_YF_WATCHLIST: list = _YF_UNIVERSE_150

# FIX-AUDIT-15: module-level shared NSE session cache to avoid creating
# 400+ warmup sessions during sector-lookup and bhavcopy-retry loops.
_NSE_SESSION_CACHE: Optional[requests.Session] = None
_NSE_SESSION_LOCK  = threading.Lock()



# ══════════════════════════════════════════════════════════════════════
# SECTION 2 — GOOGLE SHEETS CLIENT
# ══════════════════════════════════════════════════════════════════════

_GS_CLIENT    = None
_GS_WORKBOOK  = None
_GS_WS_CACHE  = {}
_GS_INIT_LOCK = threading.Lock()

_SHEETS_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_GS_RETRY_ATTEMPTS = 4
_GS_RETRY_BACKOFFS = [5, 15, 45, 120]


def _sheets_retry(fn, *args, label="sheets_call", **kwargs):
    import gspread.exceptions as gse
    last_exc = None
    for attempt, backoff in enumerate(_GS_RETRY_BACKOFFS[:_GS_RETRY_ATTEMPTS], start=1):
        try:
            return fn(*args, **kwargs)
        except gse.APIError as e:
            code = getattr(e, "response", None)
            code = code.status_code if code else 0
            if code == 429 or "RESOURCE_EXHAUSTED" in str(e):
                log.warning(f"[{label}] Google 429 (attempt {attempt}/{_GS_RETRY_ATTEMPTS}) — sleeping {backoff}s ...")
            else:
                log.warning(f"[{label}] gspread APIError {code}: {e} — retry {attempt}")
            last_exc = e
            time.sleep(backoff + random.uniform(0, 2))
        except Exception as e:
            log.warning(f"[{label}] transient error: {e} — retry {attempt}")
            last_exc = e
            time.sleep(backoff)
    raise last_exc


def _init_sheets_client() -> bool:
    global _GS_CLIENT, _GS_WORKBOOK
    if _GS_WORKBOOK is not None:
        return True
    with _GS_INIT_LOCK:
        if _GS_WORKBOOK is not None:
            return True
        if not GOOGLE_SHEET_ID or not GOOGLE_CREDS_JSON:
            log.info("Google Sheets: not configured — skipping")
            return False
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            import base64
            raw = GOOGLE_CREDS_JSON.strip()
            try:
                decoded    = base64.b64decode(raw).decode("utf-8")
                creds_dict = json.loads(decoded)
            except Exception:
                creds_dict = json.loads(raw)
            creds        = Credentials.from_service_account_info(creds_dict, scopes=_SHEETS_SCOPES)
            _GS_CLIENT   = gspread.authorize(creds)
            _GS_WORKBOOK = _sheets_retry(_GS_CLIENT.open_by_key, GOOGLE_SHEET_ID, label="open_workbook")
            log.info(f"Google Sheets workbook opened: '{_GS_WORKBOOK.title}' ✅")
            return True
        except Exception as e:
            log.error(f"Google Sheets auth/open failed: {e}")
            return False


def _get_worksheet(tab_name: str):
    if tab_name in _GS_WS_CACHE:
        return _GS_WS_CACHE[tab_name]
    if not _init_sheets_client():
        return None
    try:
        ws = _sheets_retry(_GS_WORKBOOK.worksheet, tab_name, label=f"worksheet({tab_name})")
        _GS_WS_CACHE[tab_name] = ws
        log.info(f"  Worksheet '{tab_name}' opened ✅")
        return ws
    except Exception as e:
        log.warning(f"Worksheet '{tab_name}' not found: {e}")
        _GS_WS_CACHE[tab_name] = None
        return None


def _bulk_read_sheet(tab_name: str) -> pd.DataFrame:
    ws = _get_worksheet(tab_name)
    if ws is None:
        return pd.DataFrame()
    try:
        raw = _sheets_retry(ws.get_all_values, label=f"get_all_values({tab_name})")
        if not raw or len(raw) < 2:
            log.info(f"  Sheet '{tab_name}': empty or header-only — 0 data rows")
            return pd.DataFrame()
        headers = [str(h).strip().lstrip(",").rstrip(",").strip().upper() for h in raw[0]]
        df      = pd.DataFrame(raw[1:], columns=headers)
        df      = df[~df.apply(lambda r: r.str.strip().eq("").all(), axis=1)].reset_index(drop=True)
        log.info(f"  Sheet '{tab_name}': {len(df)} data rows loaded (1 API call) ✅")
        return df
    except Exception as e:
        log.error(f"  Sheet '{tab_name}' bulk read failed: {e}")
        return pd.DataFrame()


def _read_sheet_bhavcopy()  -> pd.DataFrame: return _bulk_read_sheet(SHEET_BHAVCOPY)
def _read_sheet_fii_dii()   -> pd.DataFrame: return _bulk_read_sheet(SHEET_FII_DII)
def _read_sheet_insider()   -> pd.DataFrame: return _bulk_read_sheet(SHEET_INSIDER)
def _read_sheet_filings()   -> pd.DataFrame: return _bulk_read_sheet(SHEET_FILINGS)
def _read_sheet_earnings()  -> pd.DataFrame: return _bulk_read_sheet(SHEET_EARNINGS)

def _read_sheet_halal_list() -> set:
    if not _sheets_configured():
        return set()
    log.info("Reading Tab 7 (HALAL_LIST) — single bulk API call ...")
    df = _bulk_read_sheet(SHEET_HALAL_LIST)
    if df.empty:
        log.info("  HALAL_LIST tab: empty or not found — no custom overrides")
        return set()
    sym_col = next(
        (c for c in df.columns if "SYMBOL" in c or "SCRIP" in c or "TICKER" in c),
        df.columns[0]
    )
    syms = {
        str(s).strip().upper()
        for s in df[sym_col]
        if str(s).strip() and str(s).strip().upper() not in ("SYMBOL","SCRIP","TICKER","")
    }
    log.info(f"  HALAL_LIST: {len(syms)} custom halal symbols loaded ✅")
    return syms


def _sheets_configured() -> bool:
    return bool(GOOGLE_SHEET_ID and GOOGLE_CREDS_JSON)


# ══════════════════════════════════════════════════════════════════════
# SECTION 3 — HALAL / SHARIAH UNIVERSE
# ══════════════════════════════════════════════════════════════════════

def _fetch_shariah_csv() -> set:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/csv,text/plain,application/octet-stream,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.niftyindices.com/",
    })
    try:
        sess.get("https://www.niftyindices.com/", timeout=15)
        time.sleep(1)
    except Exception:
        pass

    urls = [
        "https://www.niftyindices.com/IndexConstituents/ind_nifty500shariah.csv",
        "https://archives.nseindia.com/content/indices/ind_nifty500shariah.csv",
        "https://www.nseindia.com/content/indices/ind_nifty500shariah.csv",
    ]
    VALID_HEADERS = ("symbol","company","ticker","isin","scrip","name","security")

    for url in urls:
        try:
            resp = sess.get(url, timeout=25, allow_redirects=True)
            if resp.status_code != 200:
                continue
            text = resp.text.lstrip()
            if len(text) < 200:
                continue
            first_line = text[:200].lower()
            if not any(kw in first_line for kw in VALID_HEADERS):
                continue
            df = pd.read_csv(io.StringIO(text))
            df.columns = df.columns.str.strip().str.upper()
            sym_col = next(
                (c for c in df.columns if any(k in c for k in ("SYMBOL","TICKER","SCRIP","SECURITY","NAME","COMPANY"))),
                None
            )
            if sym_col is None:
                continue
            syms = set()
            for s in df[sym_col]:
                if pd.isna(s):
                    continue
                sym = str(s).strip().upper()
                if sym and not sym.startswith(("INDEX","NIFTY","TOTAL","DATE","SYMBOL","SL","SR")):
                    syms.add(sym)
            if len(syms) >= 100:
                log.info(f"Shariah CSV loaded LIVE: {len(syms)} symbols ✅")
                return syms
        except Exception as e:
            log.debug(f"Shariah CSV {url}: {e}")

    try:
        nse_sess = _get_shared_nse_session()
        data = _nse_json(nse_sess, "https://www.nseindia.com/api/equity-stockIndices",
                         params={"index": "NIFTY500 SHARIAH"}, timeout=15)
        if isinstance(data, dict) and "data" in data:
            syms = {str(r.get("symbol","")).strip().upper() for r in data["data"] if str(r.get("symbol","")).strip()}
            if len(syms) >= 100:
                log.info(f"Shariah JSON API loaded: {len(syms)} symbols ✅")
                return syms
    except Exception as e:
        log.debug(f"Shariah JSON API failed: {e}")

    log.warning("All live Shariah sources failed — will use curated fallback")
    return set()


def _load_shariah_from_db() -> set:
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("SELECT symbol, cached_date FROM halal_cache LIMIT 1")
        row = cur.fetchone()
        if row:
            cached_date = datetime.strptime(row[1], "%Y-%m-%d").date()
            # FIX-AUDIT-20: use configurable TTL (default 1 day, was 7)
            if (datetime.today().date() - cached_date).days <= SHARIAH_CACHE_TTL_DAYS:
                cur.execute("SELECT symbol FROM halal_cache")
                syms = {r[0] for r in cur.fetchall()}
                con.close()
                return syms
        con.close()
    except Exception:
        pass
    return set()


def _save_shariah_to_db(syms: set):
    try:
        today = datetime.today().strftime("%Y-%m-%d")
        con = sqlite3.connect(DB_PATH)
        con.execute("DELETE FROM halal_cache")
        con.executemany(
            "INSERT OR REPLACE INTO halal_cache (symbol, cached_date) VALUES (?,?)",
            [(s, today) for s in syms]
        )
        con.commit()
        con.close()
    except Exception as e:
        log.debug(f"Shariah DB save: {e}")


def get_halal_universe() -> set:
    """
    FIX-AUDIT-06: double-checked locking pattern added so only one thread
    ever runs the expensive live fetch; all others wait and use the result.
    """
    global _SHARIAH_UNIVERSE_CACHE
    if _SHARIAH_UNIVERSE_CACHE is not None:
        return _SHARIAH_UNIVERSE_CACHE

    with _SHARIAH_UNIVERSE_LOCK:
        # Re-check inside lock (another thread may have populated while we waited)
        if _SHARIAH_UNIVERSE_CACHE is not None:
            return _SHARIAH_UNIVERSE_CACHE

        cached = _load_shariah_from_db()
        if cached and len(cached) >= 100:
            log.info(f"Halal universe from SQLite cache: {len(cached)} symbols")
            _SHARIAH_UNIVERSE_CACHE = cached
            return cached

        live = _fetch_shariah_csv()
        if live and len(live) >= 100:
            _save_shariah_to_db(live)
            _SHARIAH_UNIVERSE_CACHE = live
            log.info(f"Halal universe LIVE: {len(live)} symbols")
            return live

        # Try Sheets HALAL_LIST
        sheets_list = _read_sheet_halal_list()
        if sheets_list and len(sheets_list) >= 50:
            log.info(f"Halal universe from Sheets HALAL_LIST: {len(sheets_list)} symbols")
            _SHARIAH_UNIVERSE_CACHE = sheets_list
            return sheets_list

        log.warning(f"All dynamic halal sources failed — using minimal fallback")
        # Minimal fallback: just major known halal names
        minimal_fallback = {
            "TCS","INFY","WIPRO","HCLTECH","TECHM","SUNPHARMA","DRREDDY","CIPLA",
            "MARUTI","TATAMOTORS","HINDUNILVR","NESTLEIND","BRITANNIA","TATASTEEL",
            "HINDALCO","JSWSTEEL","LT","HAVELLS","ASIANPAINT","TITAN","TRENT"
        }
        _SHARIAH_UNIVERSE_CACHE = minimal_fallback
        return minimal_fallback


def is_halal(symbol: str) -> bool:
    sym_upper = symbol.upper()

    # FIX-AUDIT-02: HALAL_EXCLUDED is a HARD safety net — checked FIRST,
    # before HALAL_LIST. Banks, insurance, and ETFs are haram regardless
    # of what appears in the user's custom whitelist. Previously HALAL_LIST
    # was checked first, allowing HDFCBANK to bypass the exclusion.
    if sym_upper in HALAL_EXCLUDED:
        return False

    # PRIORITY 2: Keyword exclusion (catches "bank", "finance", "nifty", etc.)
    sl = symbol.lower()
    if any(kw in sl for kw in HALAL_KW) or _HALAL_KW_REGEX_EXACT.search(sl):
        return False

    # PRIORITY 3: Custom HALAL_LIST from Google Sheets — only applies to
    # non-excluded stocks. Lets user whitelist mid-caps not yet in Nifty500
    # Shariah but only after passing the hard exclusion checks above.
    if _HALAL_LIST_CUSTOM and sym_upper in _HALAL_LIST_CUSTOM:
        return True

    # PRIORITY 4: Nifty 500 Shariah universe
    universe = get_halal_universe()
    return sym_upper in universe


# ══════════════════════════════════════════════════════════════════════
# SECTION 4 — SECTOR LOOKUP
# ══════════════════════════════════════════════════════════════════════

def get_sector(sym: str) -> str:
    sym_upper = sym.upper()
    static = SYMBOL_SECTOR.get(sym_upper)
    if static:
        return static
    if sym_upper in _SECTOR_LIVE_CACHE:
        return _SECTOR_LIVE_CACHE[sym_upper]
    sector = _lookup_sector_nse(sym_upper)
    _SECTOR_LIVE_CACHE[sym_upper] = sector
    return sector


def _lookup_sector_nse(sym: str) -> str:
    try:
        # FIX-AUDIT-15: use shared cached session, not a new nse_session() per call
        sess = _get_shared_nse_session()
        data = _nse_json(sess, "https://www.nseindia.com/api/quote-equity",
                         params={"symbol": sym}, timeout=10)
        if isinstance(data, dict):
            info     = data.get("info", data)
            industry = (info.get("industry") or info.get("macro") or
                        info.get("basicIndustry") or "")
            if industry:
                il = industry.lower()
                if any(k in il for k in ("pharma","health","drug","biotech")):    return "NIFTY PHARMA"
                if any(k in il for k in ("software","it services","technology","computer")): return "NIFTY IT"
                if any(k in il for k in ("auto","vehicle","tyre","ancillar")):    return "NIFTY AUTO"
                if any(k in il for k in ("fmcg","consumer","food","beverag")):    return "NIFTY FMCG"
                if any(k in il for k in ("metal","steel","alumin","copper","mining")): return "NIFTY METAL"
                if any(k in il for k in ("energy","power","oil","gas","petro")):  return "NIFTY ENERGY"
                if any(k in il for k in ("realty","real estate","construct","housing")): return "NIFTY REALTY"
    except Exception as e:
        log.debug(f"Sector lookup {sym}: {e}")
    return "DIVERSIFIED"


# ══════════════════════════════════════════════════════════════════════
# SECTION 5 — NSE SESSION & JSON HELPERS
# ══════════════════════════════════════════════════════════════════════

def nse_session() -> requests.Session:
    """Create a fresh NSE session with cookie priming."""
    s = requests.Session()
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/html, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.nseindia.com",
        "Connection":      "keep-alive",
        "DNT":             "1",
    })
    for url in ["https://www.nseindia.com",
                "https://www.nseindia.com/market-data/live-equity-market"]:
        try:
            s.get(url, timeout=15)
            time.sleep(1.0)
        except Exception:
            pass
    return s


def _get_shared_nse_session() -> requests.Session:
    """
    FIX-AUDIT-15: Return a module-level cached NSE session.
    Creates and warms the session exactly once per process run.
    All sector lookups, Shariah JSON API calls, and retry loops
    reuse this session — eliminating ~400 redundant warmup HTTP calls.
    """
    global _NSE_SESSION_CACHE
    if _NSE_SESSION_CACHE is not None:
        return _NSE_SESSION_CACHE
    with _NSE_SESSION_LOCK:
        if _NSE_SESSION_CACHE is None:
            log.info("Initialising shared NSE session (once per run)...")
            _NSE_SESSION_CACHE = nse_session()
    return _NSE_SESSION_CACHE


def _nse_json(sess: requests.Session, url: str, params: dict = None, timeout: int = 15):
    resp = sess.get(url, params=params, timeout=timeout)
    body = resp.text.strip()
    if not body or body.startswith("<"):
        raise ValueError(f"NSE returned empty/HTML body for {url} (status={resp.status_code})")
    return resp.json()


# ══════════════════════════════════════════════════════════════════════
# SECTION 6 — SQLITE DATABASE
# ══════════════════════════════════════════════════════════════════════

def _init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        result = con.execute("PRAGMA journal_mode=WAL").fetchone()
        if not (result and result[0].upper() == "WAL"):
            con.execute("PRAGMA journal_mode=DELETE")
    except Exception:
        pass
    con.execute("PRAGMA busy_timeout=5000")
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS eod_cache (
            symbol        TEXT NOT NULL,
            trade_date    TEXT NOT NULL,
            open          REAL,
            high          REAL,
            low           REAL,
            close         REAL NOT NULL,
            volume        REAL,
            turnover_lakhs REAL,
            data_quality  TEXT NOT NULL,
            fetched_at    TEXT NOT NULL,
            PRIMARY KEY (symbol, trade_date)
        );
        CREATE TABLE IF NOT EXISTS halal_cache (
            symbol        TEXT PRIMARY KEY,
            cached_date   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS roce_cache (
            symbol        TEXT PRIMARY KEY,
            value         REAL,
            label         TEXT NOT NULL,
            fetched_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS positions (
            entry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol        TEXT NOT NULL,
            entry_price   REAL NOT NULL,
            entry_date    TEXT NOT NULL,
            initial_t3    REAL NOT NULL,
            peak_price    REAL NOT NULL,
            trailing_stop REAL NOT NULL,
            be_triggered  INTEGER DEFAULT 0,
            updated_at    TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'open',
            UNIQUE(symbol, entry_date)
        );
    """)
    # FIX-AUDIT-03 / FIX-GAP-07: ALTER TABLE can fail silently if the DB is
    # locked by another process (sqlite3 raises OperationalError: database is
    # locked, not the "duplicate column" error).  We now:
    #   1. Attempt the ALTER.
    #   2. Verify the column actually exists via PRAGMA table_info.
    #   3. Raise on lock failure so the caller knows the DB is unhealthy.
    try:
        con.execute("ALTER TABLE positions ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
        con.commit()
        log.info("DB: positions.status column added (migration)")
    except Exception as alter_exc:
        # Distinguish "column already exists" (benign) from DB-locked (fatal).
        err_msg = str(alter_exc).lower()
        if "duplicate column" in err_msg or "already exists" in err_msg:
            pass  # column already present — expected on subsequent runs
        elif "locked" in err_msg or "busy" in err_msg:
            log.error(f"DB: positions migration FAILED — database locked: {alter_exc}")
            # Don't silently swallow a lock; propagate so the caller can abort
            con.close()
            raise RuntimeError(f"DB locked during migration: {alter_exc}") from alter_exc
        else:
            log.warning(f"DB: ALTER TABLE positions unexpected error (proceeding): {alter_exc}")

    # Verify migration succeeded regardless of path taken above
    try:
        pragma_rows = con.execute("PRAGMA table_info(positions)").fetchall()
        col_names   = {row[1] for row in pragma_rows}
        if "status" not in col_names:
            log.error("DB: positions.status column MISSING after migration attempt — DB may be corrupt")
        else:
            log.debug("DB: positions.status column verified ✓")
    except Exception as verify_exc:
        log.warning(f"DB: could not verify positions schema: {verify_exc}")

    con.commit()
    con.close()


def _db_get_eod(symbol: str, trade_date: str) -> Optional[dict]:
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute(
            "SELECT open,high,low,close,volume,turnover_lakhs,data_quality "
            "FROM eod_cache WHERE symbol=? AND trade_date=?",
            (symbol.upper(), trade_date)
        )
        row = cur.fetchone()
        con.close()
        if row:
            return dict(zip(["open","high","low","close","volume","turnover_lakhs","data_quality"], row))
    except Exception as e:
        log.debug(f"DB read {symbol}: {e}")
    return None


def _db_put_eod(symbol: str, trade_date: str, rec: dict):
    dq = rec.get("data_quality")
    if dq is not None and str(dq).strip().upper() == "STALE":
        return
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.execute(
            """INSERT OR REPLACE INTO eod_cache
               (symbol,trade_date,open,high,low,close,volume,turnover_lakhs,data_quality,fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (symbol.upper(), trade_date,
             rec.get("open"), rec.get("high"), rec.get("low"),
             rec["close"], rec.get("volume"), rec.get("turnover_lakhs"),
             rec.get("data_quality","UNKNOWN"), datetime.now().isoformat())
        )
        con.commit()
        con.close()
    except Exception as e:
        log.debug(f"DB write {symbol}: {e}")


def _db_get_roce(symbol: str):
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("SELECT value, label, fetched_at FROM roce_cache WHERE symbol=?", (symbol.upper(),))
        row = cur.fetchone()
        con.close()
        if row:
            return row[0], row[1], float(row[2]) if row[2] else 0.0
    except Exception:
        pass
    return None


def _db_put_roce(symbol: str, value, label: str, fetched_at: float):
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "INSERT OR REPLACE INTO roce_cache (symbol, value, label, fetched_at) VALUES (?,?,?,?)",
            (symbol.upper(), value, label, str(fetched_at))
        )
        con.commit()
        con.close()
    except Exception:
        pass


def _get_position(symbol: str) -> Optional[dict]:
    """
    FIX-AUDIT-03: only return positions with status='open'.
    Closed positions no longer trigger PARTIAL_SELL / TRAIL directives.
    """
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute(
            "SELECT entry_price,entry_date,initial_t3,peak_price,trailing_stop,be_triggered "
            "FROM positions WHERE symbol=? AND status='open' ORDER BY entry_date DESC LIMIT 1",
            (symbol.upper(),)
        )
        row = cur.fetchone()
        con.close()
        if row:
            return dict(zip(["entry_price","entry_date","initial_t3",
                              "peak_price","trailing_stop","be_triggered"], row))
    except Exception:
        pass
    return None


def _put_position(symbol: str, entry_price: float, entry_date: str,
                  initial_t3: float, peak_price: float,
                  trailing_stop: float, be_triggered: int = 0):
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "INSERT OR REPLACE INTO positions "
            "(symbol,entry_price,entry_date,initial_t3,peak_price,trailing_stop,be_triggered,updated_at,status) "
            "VALUES (?,?,?,?,?,?,?,?,'open')",
            (symbol.upper(), entry_price, entry_date, initial_t3,
             peak_price, trailing_stop, be_triggered, datetime.today().isoformat())
        )
        con.commit()
        con.close()
    except Exception:
        pass


def close_position(symbol: str, entry_date: str = None):
    """
    FIX-AUDIT-03: mark a position as closed so future screener runs
    no longer route to TRAIL / PARTIAL_SELL directives for this symbol.
    Call this after executing an exit order.

    symbol     : NSE symbol (case-insensitive)
    entry_date : ISO date string — if None, closes the most recent open position.
    """
    try:
        con = sqlite3.connect(DB_PATH)
        if entry_date:
            con.execute(
                "UPDATE positions SET status='closed', updated_at=? "
                "WHERE symbol=? AND entry_date=? AND status='open'",
                (datetime.today().isoformat(), symbol.upper(), entry_date)
            )
        else:
            # Close most recent open position
            con.execute(
                "UPDATE positions SET status='closed', updated_at=? "
                "WHERE symbol=? AND status='open' "
                "AND entry_date=(SELECT MAX(entry_date) FROM positions WHERE symbol=? AND status='open')",
                (datetime.today().isoformat(), symbol.upper(), symbol.upper())
            )
        rows = con.total_changes
        con.commit()
        con.close()
        if rows:
            log.info(f"Position closed: {symbol.upper()} ✅")
        else:
            log.warning(f"close_position: no open position found for {symbol.upper()}")
    except Exception as e:
        log.error(f"close_position {symbol}: {e}")


# ══════════════════════════════════════════════════════════════════════
# SECTION 7 — BHAVCOPY DATA (NSE → Sheets → yfinance)
# ══════════════════════════════════════════════════════════════════════

def get_last_trading_day():
    d = datetime.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%d%m%Y"), d.strftime("%Y-%m-%d")


def _month_abbr(mm: str) -> str:
    m = {"01":"JAN","02":"FEB","03":"MAR","04":"APR","05":"MAY","06":"JUN",
         "07":"JUL","08":"AUG","09":"SEP","10":"OCT","11":"NOV","12":"DEC"}
    return m.get(mm, mm)


def download_bhavcopy(date_str: str, sess: requests.Session = None) -> pd.DataFrame:
    """
    FIX-AUDIT-21: accepts optional session parameter so the main loop
    can reuse a single session across all retry attempts instead of
    creating a new nse_session() (2 warmup HTTP calls) per date tried.
    """
    dd, mm, yyyy = date_str[:2], date_str[2:4], date_str[4:]
    mon      = _month_abbr(mm)
    yyyymmdd = f"{yyyy}{mm}{dd}"
    urls = [
        (f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip", True),
        (f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv", False),
        (f"https://archives.nseindia.com/content/historical/EQUITIES/{yyyy}/{mon}/cm{date_str}bhav.csv.zip", True),
    ]
    if sess is None:
        sess = _get_shared_nse_session()
    for url, is_zip in urls:
        try:
            resp = sess.get(url, timeout=30)
            if resp.status_code == 200 and len(resp.content) > 1000:
                df = (pd.read_csv(io.BytesIO(resp.content), compression="zip")
                      if is_zip else pd.read_csv(io.BytesIO(resp.content)))
                df.columns = df.columns.str.strip()
                if len(df) > 100:
                    return df
        except Exception as e:
            log.warning(f"Bhavcopy URL failed: {e}")
            time.sleep(1)
    raise Exception(f"All bhavcopy URLs failed for {date_str}")


def clean_bhavcopy(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = df.columns.str.strip().str.upper()
    col_maps = [
        {"TCKRSYMB":"symbol","SCTYSRS":"series","OPNPRIC":"open","HGHPRIC":"high",
         "LWPRIC":"low","CLSPRIC":"close","TTLTRADGVOL":"volume","TTLTRFVAL":"turnover"},
        {"SYMBOL":"symbol","SERIES":"series","OPEN":"open","HIGH":"high","LOW":"low",
         "CLOSE":"close","TOTTRDQTY":"volume","TOTTRDVAL":"turnover"},
        {"SYMBOL":"symbol","SERIES":"series","OPEN_PRICE":"open","HIGH_PRICE":"high",
         "LOW_PRICE":"low","CLOSE_PRICE":"close","TTL_TRD_QNTY":"volume",
         "TURNOVER_LACS":"turnover_lakhs"},
    ]
    matched = False
    for mapping in col_maps:
        if all(k in df.columns for k in mapping.keys()):
            df = df.rename(columns=mapping)
            matched = True
            break
    if not matched:
        log.warning(f"Bhavcopy: unrecognised columns: {list(df.columns[:8])}")
        return pd.DataFrame()
    if "series" in df.columns:
        df = df[df["series"].astype(str).str.strip() == "EQ"].copy()
    if "turnover_lakhs" not in df.columns:
        if "turnover" in df.columns:
            df["turnover_lakhs"] = pd.to_numeric(df["turnover"], errors="coerce").fillna(0) / 100_000
        else:
            df["turnover_lakhs"] = 0
    for col in ["open","high","low","close","volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[["symbol","open","high","low","close","volume","turnover_lakhs"]].reset_index(drop=True)
    df = df[df["close"] > 0].dropna(subset=["close"])
    df["data_quality"] = "EOD_FRESH"
    return df


def load_bhavcopy_from_sheets() -> pd.DataFrame:
    if not _sheets_configured():
        return pd.DataFrame()

    log.info(f"Reading Tab 1 (BHAVCOPY) — single bulk API call ...")
    raw = _read_sheet_bhavcopy()
    if raw.empty:
        log.info("  BHAVCOPY tab: empty or not found")
        return pd.DataFrame()

    col_map = {}
    targets = {
        "symbol":         ["SYMBOL","SCRIP","TICKER"],
        "open":           ["OPEN","OPEN_PRICE","OPNPRIC"],
        "high":           ["HIGH","HIGH_PRICE","HGHPRIC"],
        "low":            ["LOW","LOW_PRICE","LWPRIC"],
        "close":          ["CLOSE","CLOSE_PRICE","CLSPRIC","LTP"],
        "volume":         ["VOLUME","TOTTRDQTY","TTLTRADGVOL","QTY"],
        "turnover_lakhs": ["TURNOVER_LAKHS","TURNOVER_LACS","TOTTRDVAL","TTLTRFVAL"],
        "series":         ["SERIES","SCTYSRS"],
    }
    for internal, candidates in targets.items():
        for cand in candidates:
            if cand in raw.columns:
                col_map[cand] = internal
                break
        else:
            matched = next((c for c in raw.columns if any(sub in c for sub in candidates[:2])), None)
            if matched:
                col_map[matched] = internal

    df = raw.rename(columns=col_map)

    required = {"symbol","open","high","low","close","volume"}
    missing  = required - set(df.columns)

    if missing == {"volume"}:
        if "turnover_lakhs" in df.columns:
            df["volume"] = (
                pd.to_numeric(df["turnover_lakhs"], errors="coerce") * 100_000
            ) / pd.to_numeric(df["close"], errors="coerce").replace(0, np.nan)
            log.warning("  BHAVCOPY: VOLUME derived from TURNOVER÷CLOSE.")
            missing = set()
        elif "TOTTRDVAL" in raw.columns:
            # FIX-AUDIT-14: TOTTRDVAL in NSE bhavcopy is stored in ACTUAL RUPEES,
            # not in lakhs.  The v8.0 code incorrectly multiplied by 100_000 which
            # overstated volume by five orders of magnitude.
            # Correct formula: total_rupees ÷ close_price = shares_traded.
            df["volume"] = (
                pd.to_numeric(raw["TOTTRDVAL"], errors="coerce")
            ) / pd.to_numeric(df["close"], errors="coerce").replace(0, np.nan)
            log.warning(
                "  BHAVCOPY: VOLUME derived from TOTTRDVAL (rupees) ÷ close. "
                "Add TOTTRDQTY for accurate volume."
            )
            missing = set()
        else:
            df["volume"] = 0
            log.warning(
                "  BHAVCOPY: TOTTRDQTY missing — volume set to 0. "
                "Volume-based signals will be suppressed."
            )
            missing = set()

    if missing:
        log.warning(f"  BHAVCOPY tab missing required columns: {missing}. Available: {list(raw.columns[:10])}")
        return pd.DataFrame()

    if "series" in df.columns:
        before = len(df)
        df = df[df["series"].astype(str).str.strip().str.upper() == "EQ"].copy()
        log.info(f"  BHAVCOPY: {before} total rows → {len(df)} EQ rows after series filter")

    for col in ["open","high","low","close","volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "turnover_lakhs" in df.columns:
        df["turnover_lakhs"] = pd.to_numeric(df["turnover_lakhs"], errors="coerce")
        median_t = df["turnover_lakhs"].median()
        if median_t > 1_000_000:
            df["turnover_lakhs"] = df["turnover_lakhs"] / 100_000
            log.info("  BHAVCOPY: auto-converted TURNOVER from ₹ to Lakhs")
    else:
        df["turnover_lakhs"] = (df["volume"] * df["close"]) / 100_000

    df["symbol"]       = df["symbol"].astype(str).str.strip().str.upper()
    df["data_quality"] = "SHEETS_EOD"

    keep_cols = ["symbol","open","high","low","close","volume","turnover_lakhs","data_quality"]
    df = df[[c for c in keep_cols if c in df.columns]]
    df = df[df["close"] > 0].dropna(subset=["close"]).reset_index(drop=True)

    log.info(f"  BHAVCOPY: {len(df)} clean EQ records ready ✅")
    return df


def build_yfinance_universe() -> pd.DataFrame:
    """FIX-AUDIT-05: multi-ticker column bug fixed for both yfinance >=0.2 and <0.2."""
    try:
        import yfinance as yf
    except ImportError:
        return pd.DataFrame()

    universe   = get_halal_universe()
    candidates = ([s for s in _YF_UNIVERSE_150 if s.upper() in universe]
                  if universe is not _HALAL_FALLBACK_85
                  else [s for s in _YF_UNIVERSE_150 if is_halal(s)])
    if len(candidates) < 50:
        candidates = [s for s in _YF_UNIVERSE_150 if is_halal(s)]

    log.info(f"yfinance batch fallback: {len(candidates)} halal candidates")
    _, trade_date = get_last_trading_day()

    CHUNK_SIZE  = 50
    MIN_CHUNK   = 10
    BACKOFF     = [3, 12, 45]
    consec_fail = 0
    batch_close: dict = {}
    batch_vol:   dict = {}

    chunks = [candidates[i:i+CHUNK_SIZE] for i in range(0, len(candidates), CHUNK_SIZE)]
    for chunk_idx, chunk in enumerate(chunks, 1):
        if consec_fail >= 2 and len(chunk) > MIN_CHUNK:
            half       = max(MIN_CHUNK, len(chunk) // 2)
            sub_chunks = [chunk[i:i+half] for i in range(0, len(chunk), half)]
        else:
            sub_chunks = [chunk]

        for sub_chunk in sub_chunks:
            tickers = " ".join(f"{s}.NS" for s in sub_chunk)
            for attempt, backoff in enumerate(BACKOFF, 1):
                try:
                    raw = yf.download(tickers, period="2d", interval="1d",
                                      progress=False, auto_adjust=False, group_by="ticker")
                    if raw.empty:
                        consec_fail += 1
                        time.sleep(backoff + random.uniform(0, 2))
                        continue

                    for sym in sub_chunk:
                        try:
                            tk = f"{sym}.NS"
                            # FIX-AUDIT-05: yfinance >=0.2 uses (PriceType, Ticker) MultiIndex
                            # where tickers are at level 1. yfinance <0.2 uses (Ticker, PriceType)
                            # where tickers are at level 0. Handle both layouts explicitly.
                            if hasattr(raw.columns, "levels"):
                                lvl0 = list(raw.columns.get_level_values(0))
                                lvl1 = list(raw.columns.get_level_values(1))
                                if tk in lvl1:
                                    # yfinance >=0.2: (PriceType, Ticker)
                                    sub = raw.xs(tk, axis=1, level=1)
                                    sub.columns = [c.lower() for c in sub.columns]
                                elif tk in lvl0:
                                    # yfinance <0.2: (Ticker, PriceType)
                                    sub = raw[tk]
                                    sub.columns = [c.lower() for c in sub.columns]
                                else:
                                    continue
                            else:
                                # Single ticker returned as flat DataFrame
                                sub = raw.copy()
                                sub.columns = [c.lower() if isinstance(c, str) else str(c).lower()
                                               for c in sub.columns]

                            cs = sub["close"].dropna()  if "close"  in sub.columns else pd.Series(dtype=float)
                            vs = sub["volume"].dropna() if "volume" in sub.columns else pd.Series(dtype=float)
                            if not cs.empty:
                                batch_close[sym] = float(cs.iloc[-1])
                                batch_vol[sym]   = float(vs.iloc[-1]) if not vs.empty else 0.0
                        except Exception:
                            continue

                    consec_fail = 0
                    time.sleep(1)
                    break
                except Exception as e:
                    consec_fail += 1
                    time.sleep(backoff + random.uniform(0, 2))

    records = []
    for sym in candidates:
        close = batch_close.get(sym, 0.0)
        vol   = batch_vol.get(sym, 0.0)
        if close > 0:
            records.append({
                "symbol":         sym,
                "open":           close, "high": close, "low": close,
                "close":          round(close, 2),
                "volume":         vol,
                "turnover_lakhs": round((vol * close) / 100_000, 2),
                # FIX-AUDIT-17: yfinance universe data is explicitly tagged
                # SNAPSHOT_FALLBACK so downstream caps (fortress ≤55) apply.
                "data_quality":   "SNAPSHOT_FALLBACK",
            })

    log.info(f"yfinance batch complete: {len(records)}/{len(candidates)} symbols")
    return pd.DataFrame(records) if records else pd.DataFrame()

# ══════════════════════════════════════════════════════════════════════
# SECTION 8 — HISTORICAL OHLCV
# ══════════════════════════════════════════════════════════════════════

def validate_no_lookahead(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df
    df    = df.sort_values("date").drop_duplicates(subset=["date"]).reset_index(drop=True)
    today = pd.Timestamp(datetime.today().date())
    return df[df["date"] <= today].copy()


def fetch_history_nse(symbol: str, days: int = 300,
                      sess: "requests.Session | None" = None) -> pd.DataFrame:
    end   = datetime.today()
    start = end - timedelta(days=days + 50)
    if sess is None:
        sess = _get_shared_nse_session()
    params = {
        "symbol": symbol, "series": "[\"EQ\"]",
        "from": start.strftime("%d-%m-%Y"), "to": end.strftime("%d-%m-%Y"),
    }
    try:
        data    = _nse_json(sess, "https://www.nseindia.com/api/historical/cm/equity",
                            params=params, timeout=20)
        records = data.get("data", []) if isinstance(data, dict) else []
        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records).rename(columns={
            "CH_TIMESTAMP":"date","CH_OPENING_PRICE":"open",
            "CH_TRADE_HIGH_PRICE":"high","CH_TRADE_LOW_PRICE":"low",
            "CH_CLOSING_PRICE":"close","CH_TOT_TRADED_QTY":"volume",
        })
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        for c in ["open","high","low","close","volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df[["date","open","high","low","close","volume"]].dropna()
    except Exception as e:
        log.debug(f"NSE history {symbol}: {e}")
        return pd.DataFrame()


def fetch_history_yfinance(symbol: str, days: int = 300) -> pd.DataFrame:
    try:
        import yfinance as yf
        end   = datetime.today()
        start = end - timedelta(days=days + 50)
        df    = yf.download(f"{symbol}.NS", start=start, end=end,
                            progress=False, auto_adjust=False)
        if df.empty:
            return pd.DataFrame()
        df = df.reset_index()
        if hasattr(df.columns, "levels"):
            df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
        else:
            df.columns = [c.lower() if isinstance(c, str) else str(c).lower() for c in df.columns]
        df = df.rename(columns={"date":"date","open":"open","high":"high",
                                 "low":"low","close":"close","volume":"volume"})
        if "close" not in df.columns and "adj close" in df.columns:
            df = df.rename(columns={"adj close":"close"})
        df["date"] = pd.to_datetime(df["date"])
        return df[["date","open","high","low","close","volume"]].dropna()
    except Exception as e:
        log.debug(f"yfinance history {symbol}: {e}")
        return pd.DataFrame()


def fetch_history(symbol: str, days: int = 300,
                  sess: "requests.Session | None" = None) -> pd.DataFrame:
    df = fetch_history_nse(symbol, days, sess=sess)
    if len(df) < CFG["min_hist_bars"]:
        df = fetch_history_yfinance(symbol, days)
    return validate_no_lookahead(df)


# ══════════════════════════════════════════════════════════════════════
# SECTIONS 9–12: FII/DII, INSIDER, FILINGS, EARNINGS  (unchanged from v8.0)
# ══════════════════════════════════════════════════════════════════════

def _load_fii_dii_from_sheets() -> Optional[dict]:
    if not _sheets_configured(): return None
    log.info(f"Reading Tab 2 (FII_DII) — single bulk API call ...")
    df = _read_sheet_fii_dii()
    if df.empty: return None

    fii_col = next((c for c in df.columns if "FII" in c and any(k in c for k in ("NET","BUY","VALUE","CR"))),
                   next((c for c in df.columns if "FII" in c), None))
    dii_col = next((c for c in df.columns if "DII" in c and any(k in c for k in ("NET","BUY","VALUE","CR"))),
                   next((c for c in df.columns if "DII" in c), None))
    fii_buy_col  = next((c for c in df.columns if "FII" in c and "BUY"  in c), None)
    fii_sell_col = next((c for c in df.columns if "FII" in c and "SELL" in c), None)
    dii_buy_col  = next((c for c in df.columns if "DII" in c and "BUY"  in c), None)
    dii_sell_col = next((c for c in df.columns if "DII" in c and "SELL" in c), None)
    if not fii_col and not (fii_buy_col and fii_sell_col): return None
    if not dii_col and not (dii_buy_col and dii_sell_col): return None

    df_valid = df[df[fii_col].astype(str).str.strip().ne("")]
    if df_valid.empty: return None

    def _parse_cr(cell) -> float:
        s = str(cell).replace(",","").replace("₹","").replace("(","").replace(")","")
        s = s.replace("CRORES","").replace("CR","").replace(" ","").strip()
        try: return float(s or 0)
        except ValueError: return 0.0

    try:
        row     = df_valid.iloc[-1]
        fii_net = _parse_cr(row[fii_col]) if fii_col else _parse_cr(row[fii_buy_col]) - _parse_cr(row[fii_sell_col])
        dii_net = _parse_cr(row[dii_col]) if dii_col else _parse_cr(row[dii_buy_col]) - _parse_cr(row[dii_sell_col])
    except Exception as e:
        log.warning(f"  FII_DII parse error: {e}"); return None

    both_buy  = fii_net > 0 and dii_net > 0
    fii_buy   = fii_net > 0; dii_buy = dii_net > 0
    both_sell = fii_net < 0 and dii_net < 0
    if both_buy:    score=30; label="🟢 FII+DII BUYING"
    elif fii_buy:   score=22; label="✅ FII BUYING"
    elif dii_buy:   score=18; label="✅ DII BUYING"
    elif both_sell: score=5;  label="🔴 FII+DII SELLING"
    else:           score=12; label="↔ MIXED"
    mag_bonus = min(5, int((abs(fii_net)+abs(dii_net))/1000))
    score     = min(30, score + (mag_bonus if fii_buy else 0))
    return {"fii_net":round(fii_net,0),"dii_net":round(dii_net,0),"score":score,"label":label,
            "detail":f"FII ₹{fii_net:+,.0f} Cr | DII ₹{dii_net:+,.0f} Cr [SHEETS Tab 2]"}


def fetch_fii_dii() -> dict:
    neutral = {"fii_net":0,"dii_net":0,"score":15,"label":"↔ MIXED","detail":"FII/DII data unavailable — neutral score"}
    if not FORCE_SHEETS and not FORCE_YFINANCE:
        try:
            sess = _get_shared_nse_session()
            data = _nse_json(sess, "https://www.nseindia.com/api/fiidiiTradeReact")
            if data:
                row     = data[0] if isinstance(data, list) else data
                fii_net = float(str(row.get("fiiNet", row.get("FII_NET_PURCHASE_SALES",0))).replace(",",""))
                dii_net = float(str(row.get("diiNet", row.get("DII_NET_PURCHASE_SALES",0))).replace(",",""))
                both_buy  = fii_net>0 and dii_net>0; fii_buy=fii_net>0; dii_buy=dii_net>0
                both_sell = fii_net<0 and dii_net<0
                if both_buy:    score=30; label="🟢 FII+DII BUYING"
                elif fii_buy:   score=22; label="✅ FII BUYING"
                elif dii_buy:   score=18; label="✅ DII BUYING"
                elif both_sell: score=5;  label="🔴 FII+DII SELLING"
                else:           score=12; label="↔ MIXED"
                mag_bonus = min(5, int((abs(fii_net)+abs(dii_net))/1000))
                score     = min(30, score+(mag_bonus if fii_buy else 0))
                fii_cr    = fii_net/100; dii_cr=dii_net/100
                return {"fii_net":round(fii_cr,0),"dii_net":round(dii_cr,0),"score":score,"label":label,
                        "detail":f"FII ₹{fii_cr:+,.0f} Cr | DII ₹{dii_cr:+,.0f} Cr"}
        except Exception as e:
            log.warning(f"FII/DII NSE failed: {e}")
    sheets_result = _load_fii_dii_from_sheets()
    if sheets_result: return sheets_result
    try:
        import yfinance as yf
        vix_df = yf.download("^INDIAVIX", period="10d", progress=False, auto_adjust=True)
        if not vix_df.empty and len(vix_df) >= 2:
            vix_vals = vix_df["Close"].to_numpy().flatten()
            vix_now  = float(vix_vals[-1]); vix_prev=float(vix_vals[-2])
            vix_chg  = (vix_now-vix_prev)/vix_prev*100
            if vix_chg<-5:   score=25; label="🟢 VIX falling sharply"
            elif vix_chg<-2: score=20; label="✅ VIX declining"
            elif vix_now<14: score=18; label="↔ VIX low/complacent"
            elif vix_chg>5:  score=7;  label="🔴 VIX rising sharply"
            elif vix_now>20: score=8;  label="↔ VIX elevated"
            else:            score=14; label="↔ VIX stable"
            return {"fii_net":0,"dii_net":0,"score":score,"label":label,
                    "detail":f"VIX proxy: {vix_now:.1f} ({vix_chg:+.1f}% 1d) — ⚠️ NSE+Sheets blocked"}
    except Exception:
        pass
    return neutral


def _load_insider_from_sheets() -> Optional[dict]:
    if not _sheets_configured(): return None
    log.info(f"Reading Tab 3 (INSIDER) — single bulk API call ...")
    df = _read_sheet_insider()
    if df.empty: return None
    sym_col = next((c for c in df.columns if "SYMBOL" in c or "SCRIP" in c), None)
    typ_col = next((c for c in df.columns if any(k in c for k in ("TYPE","ACQMODE","MODE","TRANSACTION","BUYSELL"))), None)
    val_col = next((c for c in df.columns if any(k in c for k in ("VALUE","LAKH","AMOUNT","CONSIDERATION"))), None)
    shr_col = next((c for c in df.columns if any(k in c for k in ("SHARE","QTY","QUANTITY","SECACQ","TOTSHR"))), None)
    dt_col  = next((c for c in df.columns if any(k in c for k in ("DATE","ACQFROM","TXDATE"))), None)
    per_col = next((c for c in df.columns if any(k in c for k in ("PERSON","NAME","ACQNAME","INSIDER","WHO"))), None)
    if not sym_col: return None
    if typ_col:
        buy_mask = df[typ_col].astype(str).str.lower().str.contains(r"buy|purchase|acqui|market|open|\bb\b", na=False, regex=True)
        df = df[buy_mask].copy()
    if df.empty: return None
    insider_map: dict = {}
    cutoff = datetime.today() - timedelta(days=30)
    for _, row in df.iterrows():
        sym = str(row.get(sym_col,"")).strip().upper()
        if not sym or not is_halal(sym): continue
        days_ago = 30
        if dt_col:
            try:
                tx_date = pd.to_datetime(str(row[dt_col]), dayfirst=True, errors="coerce")
                if pd.isna(tx_date) or tx_date < cutoff: continue
                days_ago = max(0, (datetime.today()-tx_date).days)
            except Exception: pass
        val_rupees = 0.0
        if val_col:
            try:
                raw_val    = str(row[val_col]).replace(",","").replace("₹","").strip()
                val_lakh   = float(raw_val or 0)
                val_rupees = val_lakh*100_000 if val_lakh<100_000 else val_lakh
            except Exception: pass
        shares = 0.0
        if shr_col:
            try: shares = float(str(row[shr_col]).replace(",","").strip() or 0)
            except Exception: pass
        person = str(row[per_col]).strip() if per_col else "Insider"
        if sym not in insider_map:
            insider_map[sym] = {"transactions":[],"total_shares":0.0,"total_value_rupees":0.0}
        insider_map[sym]["transactions"].append({"person":person,"shares":shares,"days_ago":days_ago,"type":"buy","value_rupees":val_rupees})
        insider_map[sym]["total_shares"]       += shares
        insider_map[sym]["total_value_rupees"] += val_rupees
    for sym, d in insider_map.items():
        val_rupees     = d["total_value_rupees"]
        if val_rupees==0 and d["total_shares"]>0: val_rupees = d["total_shares"]*10
        recency_weight = 0.70
        for tx in d["transactions"]:
            da = tx.get("days_ago",30)
            if da<7: recency_weight=max(recency_weight,1.00)
            elif da<14: recency_weight=max(recency_weight,0.85)
        if val_rupees>0:
            log_val = math.log10(max(1,val_rupees))
            score   = max(5, min(30, round((log_val-4)*5*recency_weight)))
        else: score=0
        n = len(d["transactions"])
        d["score"]  = score
        d["detail"] = f"{n} insider buy(s) [SHEETS] | ~₹{val_rupees/1e7:.1f}Cr | ×{recency_weight:.2f} → {score}pts"
    if insider_map: return insider_map
    return None


def fetch_insider_trades(days_back: int = 30) -> dict:
    insider_map: dict = {}
    if not FORCE_SHEETS and not FORCE_YFINANCE:
        try:
            sess   = _get_shared_nse_session()
            data   = _nse_json(sess, "https://www.nseindia.com/api/corporates-pit", params={"index":"equities"})
            data   = data.get("data",[]) if isinstance(data,dict) else data
            cutoff = datetime.today()-timedelta(days=days_back)
            for row in data:
                try:
                    sym      = str(row.get("symbol","")).upper()
                    if not sym or not is_halal(sym): continue
                    acq_type = str(row.get("acqMode","")).lower()
                    if "sell" in acq_type or "pledge" in acq_type: continue
                    try:
                        trade_date = pd.to_datetime(row.get("date",row.get("acqfromDt","")))
                        if trade_date < cutoff: continue
                    except Exception: pass
                    val_shrs   = float(str(row.get("totAcqShrs",row.get("secAcq",0))).replace(",",""))
                    val_lakh   = row.get("secVal",row.get("totVal",0))
                    try: val_rupees=float(str(val_lakh).replace(",",""))*100_000
                    except: val_rupees=val_shrs*10
                    if sym not in insider_map:
                        insider_map[sym]={"transactions":[],"total_shares":0,"total_value_rupees":0}
                    insider_map[sym]["transactions"].append({"person":row.get("acqName","Insider"),"shares":val_shrs,"date":row.get("date",""),"type":acq_type,"value_rupees":val_rupees})
                    insider_map[sym]["total_shares"]       += val_shrs
                    insider_map[sym]["total_value_rupees"] += val_rupees
                except Exception: continue
            for sym,d in insider_map.items():
                value_rupees   = d.get("total_value_rupees",d["total_shares"]*10)
                recency_weight = 0.70
                for tx in d["transactions"]:
                    try:
                        da = (datetime.today()-pd.to_datetime(tx.get("date",""))).days
                        if da<7: recency_weight=max(recency_weight,1.00)
                        elif da<14: recency_weight=max(recency_weight,0.85)
                    except: pass
                if value_rupees>0:
                    log_val = math.log10(max(1,value_rupees))
                    score   = max(5,min(30,round((log_val-4)*5*recency_weight)))
                else: score=0
                d["score"]  = score
                d["detail"] = f"{len(d['transactions'])} buy(s) | ~₹{value_rupees/1e7:.1f}Cr | ×{recency_weight:.2f} → {score}pts"
            if insider_map: return insider_map
        except Exception as e:
            log.warning(f"Insider NSE failed: {e}")
    sheets_insider = _load_insider_from_sheets()
    if sheets_insider: return sheets_insider
    # FIX-GAP-01: DEGRADED MODE — NSE + Sheets both unavailable.
    # Fall back to yfinance major_holders for stocks in the yfinance watchlist.
    # This gives noisy but non-empty insider data so the screener isn't blind.
    if not insider_map:
        log.warning("Insider: NSE + Sheets unavailable — attempting yfinance major_holders fallback")
        try:
            import yfinance as yf
            # Probe a small representative set; full universe is too slow
            _yf_probe_syms = list(getattr(sys.modules[__name__], "_YF_WATCHLIST", []))[:30]
            for sym in _yf_probe_syms:
                try:
                    mh = yf.Ticker(f"{sym}.NS").major_holders
                    if mh is None or (hasattr(mh, "empty") and mh.empty):
                        continue
                    # yfinance major_holders[0] = % held by insiders (float row)
                    insider_pct = None
                    try:
                        insider_pct = float(str(mh.iloc[0, 0]).replace("%", ""))
                    except Exception:
                        pass
                    if insider_pct is not None and insider_pct > 30:
                        score = min(20, max(5, int(insider_pct / 5)))
                        insider_map[sym] = {
                            "transactions": [],
                            "total_shares": 0,
                            "total_value_rupees": 0,
                            "score": score,
                            "detail": f"yfinance major_holders: {insider_pct:.1f}% insider held → {score}pts [DEGRADED]",
                        }
                except Exception:
                    continue
            if insider_map:
                log.info(f"Insider degraded fallback: {len(insider_map)} symbols from yfinance")
        except Exception as e:
            log.warning(f"Insider yfinance degraded fallback failed: {e}")
    return insider_map


def _load_filings_from_sheets() -> Optional[dict]:
    if not _sheets_configured(): return None
    log.info(f"Reading Tab 4 (FILINGS) — single bulk API call ...")
    df = _read_sheet_filings()
    if df.empty: return None
    sym_col  = next((c for c in df.columns if "SYMBOL" in c or "SCRIP" in c), None)
    subj_col = next((c for c in df.columns if any(k in c for k in ("SUBJECT","DESC","FILING","ANNOUNCEMENT","HEADLINE","TITLE"))), None)
    sent_col = next((c for c in df.columns if any(k in c for k in ("SENTIMENT","SIGNAL","IMPACT","OUTLOOK"))), None)
    dt_col   = next((c for c in df.columns if "DATE" in c), None)
    if not sym_col or not subj_col: return None
    positive_kw = ["bonus","dividend","buyback","split","record date","profit","growth","expansion","order","contract","win","award","acquisition","launch","guidance raised","upgrade","beat","record","approved"]
    negative_kw = ["loss","write-off","write off","penalty","fraud","probe","npa","default","downgrade","miss","warning","regulatory","sebi notice","court","litigation"]
    filings: dict = {}
    cutoff  = datetime.today() - timedelta(days=14)
    for _, row in df.iterrows():
        sym = str(row.get(sym_col,"")).strip().upper()
        if not sym: continue
        if dt_col:
            try:
                row_date = pd.to_datetime(str(row[dt_col]), dayfirst=True, errors="coerce")
                if pd.isna(row_date) or row_date < cutoff: continue
            except Exception: pass
        subject = str(row.get(subj_col,"")).lower() if subj_col else ""
        if sent_col:
            sent = str(row.get(sent_col,"")).strip().lower()
            if any(s in sent for s in ("strong positive","very positive","bullish strong")): score=30
            elif any(s in sent for s in ("positive","bullish")): score=25
            elif any(s in sent for s in ("negative","bearish")): score=5
            elif any(s in sent for s in ("strong negative","very negative")): score=2
            else:
                pos=sum(1 for k in positive_kw if k in subject); neg=sum(1 for k in negative_kw if k in subject)
                score=min(30,max(0,15+pos*5-neg*8))
        else:
            pos=sum(1 for k in positive_kw if k in subject); neg=sum(1 for k in negative_kw if k in subject)
            score=min(30,max(0,15+pos*5-neg*8))
        detail = (subject[:80].capitalize() if subject else "Corporate filing")+" [SHEETS Tab 4]"
        if sym not in filings or score > filings[sym]["score"]:
            filings[sym] = {"score":score,"detail":detail}
    if filings: return filings
    return None


def fetch_recent_filings(days_back: int = 14) -> dict:
    filings: dict = {}
    if not FORCE_SHEETS and not FORCE_YFINANCE:
        try:
            sess = _get_shared_nse_session()
            data = _nse_json(sess, "https://www.nseindia.com/api/corporates-corporateActions",
                             params={"index":"equities",
                                     "from_date":(datetime.today()-timedelta(days=days_back)).strftime("%d-%m-%Y"),
                                     "to_date":datetime.today().strftime("%d-%m-%Y"),
                                     "type":"announcements"})
            if isinstance(data, dict): data=data.get("data",[])
            if not data or not isinstance(data,list): raise ValueError("NSE filings: no usable data")
            first = data[0] if data else {}
            if not any(k in first for k in ("symbol","subject","desc","Symbol","Subject")):
                raise ValueError("NSE filings: unexpected schema")
            pos_kw=["bonus","dividend","buyback","split","record date","profit","growth","expansion","order","contract","win"]
            neg_kw=["loss","write-off","penalty","fraud","probe","npa","default"]
            for row in data:
                try:
                    sym=str(row.get("symbol","")).upper(); subject=str(row.get("subject",row.get("desc",""))).lower()
                    if not sym: continue
                    pos=sum(1 for k in pos_kw if k in subject); neg=sum(1 for k in neg_kw if k in subject)
                    score=min(30,max(0,15+pos*5-neg*8))
                    if sym not in filings or score>filings[sym]["score"]:
                        filings[sym]={"score":score,"detail":subject[:80].capitalize()}
                except Exception: continue
            if filings: return filings
        except Exception as e:
            log.warning(f"Filings NSE failed: {e}")
    sheets_filings = _load_filings_from_sheets()
    if sheets_filings: return sheets_filings
    # FIX-GAP-01: DEGRADED MODE — NSE + Sheets both unavailable.
    # yfinance news feed gives recent corporate announcements as a rudimentary proxy.
    if not filings:
        log.warning("Filings: NSE + Sheets unavailable — attempting yfinance news fallback [DEGRADED]")
        try:
            import yfinance as yf
            pos_kw = ["bonus", "dividend", "buyback", "split", "profit", "growth",
                      "expansion", "order", "contract", "win", "award", "acquisition"]
            neg_kw = ["loss", "write-off", "penalty", "fraud", "probe", "npa",
                      "default", "downgrade", "litigation"]
            _yf_probe_syms = list(getattr(sys.modules[__name__], "_YF_WATCHLIST", []))[:40]
            for sym in _yf_probe_syms:
                try:
                    news = yf.Ticker(f"{sym}.NS").news or []
                    for item in news[:5]:
                        title = str(item.get("title", "")).lower()
                        pos   = sum(1 for k in pos_kw if k in title)
                        neg   = sum(1 for k in neg_kw if k in title)
                        score = min(30, max(0, 15 + pos * 5 - neg * 8))
                        detail = title[:80].capitalize() + " [yfinance news — DEGRADED]"
                        if sym not in filings or score > filings[sym]["score"]:
                            filings[sym] = {"score": score, "detail": detail}
                except Exception:
                    continue
            if filings:
                log.info(f"Filings degraded fallback: {len(filings)} symbols from yfinance news")
        except Exception as e:
            log.warning(f"Filings yfinance degraded fallback failed: {e}")
    return filings


def _count_nse_trading_days(from_date: datetime, to_date: datetime) -> int:
    try:
        import pandas_market_calendars as mcal
        nse_cal  = mcal.get_calendar("NSE")
        schedule = nse_cal.schedule(start_date=from_date.strftime("%Y-%m-%d"), end_date=to_date.strftime("%Y-%m-%d"))
        return max(1, len(schedule))
    except Exception:
        calendar_days = max(1, (to_date-from_date).days)
        return max(1, round(calendar_days*5/7))


def _load_earnings_from_sheets() -> Optional[dict]:
    if not _sheets_configured(): return None
    log.info(f"Reading Tab 5 (EARNINGS) — single bulk API call ...")
    df = _read_sheet_earnings()
    if df.empty: return None
    sym_col  = next((c for c in df.columns if "SYMBOL" in c or "SCRIP" in c), None)
    date_col = next((c for c in df.columns if any(k in c for k in ("RESULT_DATE","DATE","RESULT","EVENT_DATE"))), None)
    pur_col  = next((c for c in df.columns if any(k in c for k in ("PURPOSE","TYPE","EVENT","CATEGORY"))), None)
    if not sym_col or not date_col: return None
    cal: dict = {}; today=datetime.today()
    for _, row in df.iterrows():
        try:
            sym = str(row.get(sym_col,"")).strip().upper()
            if not sym: continue
            if pur_col:
                pur=str(row.get(pur_col,"")).strip().lower()
                if pur and not any(k in pur for k in ("result","dividend","earning","q1","q2","q3","q4","annual")): continue
            raw_date=str(row[date_col]).strip()
            if not raw_date: continue
            try:   dt=pd.to_datetime(raw_date,format="%d-%m-%Y",errors="raise")
            except: dt=pd.to_datetime(raw_date,dayfirst=True,errors="coerce")
            if pd.isna(dt): continue
            dt_py=dt.to_pydatetime(); calendar_days=(dt_py-today).days
            td_delta = _count_nse_trading_days(today,dt_py) if calendar_days>=0 else -_count_nse_trading_days(dt_py,today)
            if sym not in cal or abs(td_delta)<abs(cal[sym]): cal[sym]=td_delta
        except Exception: continue
    if cal: return cal
    return None


def fetch_earnings_calendar() -> dict:
    cal: dict = {}
    if not FORCE_SHEETS and not FORCE_YFINANCE:
        try:
            sess   = _get_shared_nse_session()
            events = _nse_json(sess,"https://www.nseindia.com/api/event-calendar",params={"index":"equities"})
            if isinstance(events,dict): events=events.get("data",[])
            today=datetime.today()
            for ev in events:
                try:
                    sym=str(ev.get("symbol","")).upper(); pur=str(ev.get("purpose","")).lower()
                    if "result" not in pur and "dividend" not in pur: continue
                    dt=pd.to_datetime(ev.get("date","")); cal_days=(dt-today).days
                    td_delta = _count_nse_trading_days(today,dt.to_pydatetime()) if cal_days>=0 else -_count_nse_trading_days(dt.to_pydatetime(),today)
                    if sym not in cal or abs(td_delta)<abs(cal[sym]): cal[sym]=td_delta
                except Exception: continue
            if cal: return cal
        except Exception as e:
            log.warning(f"Earnings NSE failed: {e}")
    sheets_cal = _load_earnings_from_sheets()
    if sheets_cal: return sheets_cal
    # FIX-GAP-01: DEGRADED MODE — NSE + Sheets both unavailable.
    # A SILENT empty earnings_cal means the earnings veto in assemble_result_v8
    # can NEVER fire — stocks going ex-results tomorrow pass unvetted.
    # yfinance calendar gives next_earnings_date for listed companies.
    if not cal:
        log.warning(
            "Earnings: NSE + Sheets unavailable — attempting yfinance calendar fallback. "
            "DEGRADED MODE: veto window may miss unlisted earnings dates."
        )
        try:
            import yfinance as yf
            _yf_probe_syms = list(getattr(sys.modules[__name__], "_YF_WATCHLIST", []))[:60]
            today = datetime.today()
            for sym in _yf_probe_syms:
                try:
                    t   = yf.Ticker(f"{sym}.NS")
                    cal_data = t.calendar
                    if cal_data is None:
                        continue
                    # calendar is a dict with key 'Earnings Date' → list of datetimes
                    earn_dates = None
                    if isinstance(cal_data, dict):
                        earn_dates = cal_data.get("Earnings Date")
                    elif hasattr(cal_data, "T"):
                        # older yfinance returns a DataFrame
                        try:
                            earn_dates = cal_data.T.get("Earnings Date")
                        except Exception:
                            pass
                    if not earn_dates:
                        continue
                    if not hasattr(earn_dates, "__iter__"):
                        earn_dates = [earn_dates]
                    for ed in earn_dates:
                        try:
                            dt = pd.to_datetime(ed)
                            calendar_days = (dt.to_pydatetime() - today).days
                            td_delta = (
                                _count_nse_trading_days(today, dt.to_pydatetime())
                                if calendar_days >= 0
                                else -_count_nse_trading_days(dt.to_pydatetime(), today)
                            )
                            if sym not in cal or abs(td_delta) < abs(cal[sym]):
                                cal[sym] = td_delta
                        except Exception:
                            continue
                except Exception:
                    continue
            if cal:
                log.info(f"Earnings degraded fallback: {len(cal)} symbols from yfinance")
            else:
                log.warning(
                    "Earnings: yfinance fallback also returned nothing. "
                    "Earnings veto will be INACTIVE this run — exercise manual caution."
                )
        except Exception as e:
            log.warning(f"Earnings yfinance degraded fallback failed: {e}")
    return cal


# ══════════════════════════════════════════════════════════════════════
# SECTION 13 — ROCE QUALITY GATE
# ══════════════════════════════════════════════════════════════════════

def fetch_roce_proxy(symbol: str) -> tuple:
    sym_upper = symbol.upper()
    if sym_upper in _roce_cache:
        cached_val, cached_label, cached_at = _roce_cache[sym_upper]
        if time.time()-cached_at < _ROCE_CACHE_TTL_SECONDS:
            return cached_val, cached_label
    db_row = _db_get_roce(sym_upper)
    if db_row is not None:
        db_val, db_label, db_at = db_row
        if time.time()-db_at < _ROCE_CACHE_TTL_SECONDS:
            _roce_cache[sym_upper] = (db_val, db_label, db_at)
            return db_val, db_label
    result = (None, "ROE quality data unavailable")
    try:
        import yfinance as yf
        info = yf.Ticker(f"{symbol}.NS").info
        roe  = info.get("returnOnEquity")
        roa  = info.get("returnOnAssets")
        debt = info.get("debtToEquity", 0) or 0
        if roe is not None:
            roe_pct = float(roe)*100
            # FIX-AUDIT-18: label consistently says ROE(proxy) — not ROCE —
            # so users know the metric is returnOnEquity, not Capital Employed.
            quality = ("HIGH ✓" if roe_pct>=15 else "ACCEPTABLE" if roe_pct>=5
                       else "LOW ⚠️" if roe_pct>=0 else "NEGATIVE ❌")
            debt_note = f" | D/E:{debt:.1f}" if debt else ""
            result = (roe_pct, f"ROE(proxy) {roe_pct:.1f}% [{quality}]{debt_note}")
        elif roa is not None:
            roa_pct = float(roa)*100
            quality = "ACCEPTABLE" if roa_pct>=5 else "LOW ⚠️"
            result  = (roa_pct, f"ROA(proxy) {roa_pct:.1f}% [{quality}]")
    except Exception as e:
        log.debug(f"ROCE fetch {symbol}: {e}")
    _roce_cache[sym_upper] = (result[0], result[1], time.time())
    _db_put_roce(sym_upper, result[0], result[1], time.time())
    return result


# ══════════════════════════════════════════════════════════════════════
# SECTION 14 — INDICATOR ENGINE
# ══════════════════════════════════════════════════════════════════════

def calc_atr(df, period=14):
    h,l,c = df["high"],df["low"],df["close"]
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.ewm(span=period,adjust=False).mean()

def calc_rsi(s, period=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(span=period,adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(span=period,adjust=False).mean()
    return 100-(100/(1+g/l.replace(0,np.nan)))

def calc_mfi(df, period=14):
    tp  = (df["high"]+df["low"]+df["close"])/3
    rmf = tp*df["volume"]
    pos = rmf.where(tp>tp.shift(),0)
    neg = rmf.where(tp<tp.shift(),0)
    mfr = pos.rolling(period).sum()/neg.rolling(period).sum().replace(0,np.nan)
    return 100-(100/(1+mfr))

def calc_adx(df, period=14) -> float:
    """
    FIX-AUDIT-08: added explicit NaN guard.
    When price is perfectly flat (pdi+ndi==0), dx becomes NaN and
    propagates silently through all regime checks.  Return 0.0 (ranging)
    as a safe default — no trend is the correct interpretation of flat price.
    """
    h,l,c = df["high"],df["low"],df["close"]
    tr  = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr = tr.ewm(span=period,adjust=False).mean()
    up  = h-h.shift(); dn=l.shift()-l
    pdm = up.where((up>dn)&(up>0),0); ndm=dn.where((dn>up)&(dn>0),0)
    pdi = 100*pdm.ewm(span=period,adjust=False).mean()/atr
    ndi = 100*ndm.ewm(span=period,adjust=False).mean()/atr
    dx  = 100*(pdi-ndi).abs()/(pdi+ndi).replace(0,np.nan)
    result = float(dx.ewm(span=period,adjust=False).mean().iloc[-1])
    # Guard: NaN means flat/zero range — treat as fully ranging (ADX=0)
    return result if not math.isnan(result) else 0.0


def _calc_vpoc_single(df: pd.DataFrame, lookback: int, n_bins: int = 100) -> float:
    """
    FIX-AUDIT-07: zero-volume guard added.
    When all volume values are zero (yfinance returns zeros for illiquid
    stocks), the bin_volume array stays all-zero and argmax returns 0
    (the lowest price bin), making VPOC = price_min.  This is NOT the
    VPOC — it's a silent artefact.  Return the mid-range price instead
    as a neutral estimate; callers must also check _vpoc_volume_reliable().
    """
    r = df.tail(lookback).copy()
    if len(r) < 20:
        return float(df["close"].iloc[-1])
    price_min = float(r["low"].min()); price_max=float(r["high"].max())
    if price_max <= price_min:
        return float(r["close"].iloc[-1])

    # FIX-AUDIT-07: guard zero-volume data
    total_vol = float(r["volume"].sum())
    if total_vol <= 0:
        # No volume data — return mid-range as a neutral, non-inflating estimate.
        # Callers that check layer1 (within 2% of VPOC) must also gate on
        # _vpoc_volume_reliable() to avoid false PRISTINE signals.
        return float((price_min + price_max) / 2.0)

    bins       = np.linspace(price_min, price_max, n_bins+1)
    bin_volume = np.zeros(n_bins)
    n          = len(r)
    lows       = r["low"].values.astype(float)
    highs      = r["high"].values.astype(float)
    volumes    = r["volume"].values.astype(float)
    recency_weights = np.linspace(0.5, 1.0, n)
    for i in range(n):
        bl,bh,vol = lows[i],highs[i],volumes[i]
        if vol<=0 or bh<=bl: continue
        overlap    = np.maximum(0.0, np.minimum(bh,bins[1:])-np.maximum(bl,bins[:-1]))
        bin_volume += recency_weights[i]*vol*(overlap/(bh-bl))
    vpoc_idx = int(np.argmax(bin_volume))
    return float((bins[vpoc_idx]+bins[vpoc_idx+1])/2.0)


def _vpoc_volume_reliable(df: pd.DataFrame, lookback: int = 63) -> bool:
    """
    FIX-AUDIT-07: helper to check if there is sufficient volume data
    in the lookback window to trust the VPOC calculation.
    Returns False when > 80% of bars have zero volume.
    """
    r     = df.tail(lookback)
    if len(r) == 0: return False
    zeros = (r["volume"] <= 0).sum()
    return (zeros / len(r)) < 0.80


def calc_vpoc(df: pd.DataFrame, lookback: int = 252, n_bins: int = 100) -> float:
    wt_3m  = SNIPER_CFG.get("vpoc_3m_wt",  0.40)
    wt_6m  = SNIPER_CFG.get("vpoc_6m_wt",  0.35)
    wt_12m = SNIPER_CFG.get("vpoc_12m_wt", 0.25)
    lb_3m  = min(63,  len(df))
    lb_6m  = min(126, len(df))
    lb_12m = min(252, len(df))
    vpoc_3m  = _calc_vpoc_single(df, lb_3m,  n_bins)
    vpoc_6m  = _calc_vpoc_single(df, lb_6m,  n_bins)
    vpoc_12m = _calc_vpoc_single(df, lb_12m, n_bins)
    divergence = abs(vpoc_3m-vpoc_6m)/max(vpoc_6m,1e-6)
    if divergence > 0.10:
        wt_3m, wt_6m, wt_12m = 0.20, 0.45, 0.35
    total_wt      = wt_3m+wt_6m+wt_12m
    vpoc_blended  = (vpoc_3m*wt_3m+vpoc_6m*wt_6m+vpoc_12m*wt_12m)/total_wt
    return round(float(vpoc_blended), 2)


# ══════════════════════════════════════════════════════════════════════
# SECTION 15 — FORWARD-LOOKING SIGNALS
# ══════════════════════════════════════════════════════════════════════

def calc_52w_compression(hist: pd.DataFrame, close: float, atr14: float) -> tuple:
    if len(hist) < 20: return 0,"52W: insufficient data"
    lookback  = hist.tail(252)
    high_52w  = float(lookback["high"].max())
    if high_52w<=0 or close<=0: return 0,"52W: price error"
    pct_from_high = (high_52w-close)/high_52w*100
    atr100_val    = float(calc_atr(hist,100).iloc[-1]) if len(hist)>=100 else atr14
    atr_tight     = atr14>0 and atr100_val>0 and (atr14/atr100_val)<0.70
    if pct_from_high<=5.0:
        bonus=12 if atr_tight else 9; tier="ELITE COIL 🎯" if atr_tight else "AT 52W HIGH"
    elif pct_from_high<=10.0:
        bonus=7 if atr_tight else 5; tier="NEAR HIGH+COIL" if atr_tight else "NEAR 52W HIGH"
    elif pct_from_high<=15.0:
        bonus=3; tier="APPROACHING HIGH"
    else:
        bonus=0; tier=f"{pct_from_high:.0f}% from 52W high"
    return bonus,f"52W: {pct_from_high:.1f}% from ₹{high_52w:.0f} [{tier}] +{bonus}pts"


def calc_atr_velocity(hist: pd.DataFrame) -> tuple:
    if len(hist)<55: return 0,"ATR-V: insufficient data"
    atr7=float(calc_atr(hist,7).iloc[-1]); atr20=float(calc_atr(hist,20).iloc[-1]); atr50=float(calc_atr(hist,50).iloc[-1])
    if atr50<=0: return 0,"ATR-V: baseline zero"
    full_contraction = atr7<atr20 and atr20<atr50; partial=atr7<atr50; rate=1.0-(atr7/atr50)
    if full_contraction:
        if rate>0.50: bonus,tier=8,"🌀 COIL CRITICAL (+8pts)"
        elif rate>0.30: bonus,tier=6,"🌀 COIL TIGHT (+6pts)"
        else: bonus,tier=4,"🌀 COILING (+4pts)"
    elif partial: bonus,tier=2,"COMPRESSING (+2pts)"
    else: bonus,tier=0,"EXPANDING"
    return bonus,f"ATR-V: {rate*100:.0f}% compressed [{tier}]"


def calc_pead_bonus(symbol: str, earnings_cal: dict, hist: pd.DataFrame) -> tuple:
    days = earnings_cal.get(symbol.upper())
    if days is None or days>=0: return 0,""
    recency = abs(days)
    if recency>21 or len(hist)<5: return 0,""
    try:
        lookback  = min(recency,len(hist)-1)
        close_now = float(hist["close"].iloc[-1]); close_pre=float(hist["close"].iloc[-lookback-1])
        drift_pct = (close_now-close_pre)/close_pre*100 if close_pre>0 else 0
        if drift_pct>=3.0 and recency<=5:
            return 10,f"🔥 HOT PEAD — {drift_pct:.1f}% drift {recency}td post-results (+10pts)"
        elif drift_pct>=1.5 and recency<=14:
            return 7,f"📈 PEAD DRIFT — {drift_pct:.1f}% over {recency}td (+7pts)"
        elif recency<=21 and drift_pct>=0:
            return 4,f"PEAD window {recency}td, drift {drift_pct:.1f}% (+4pts)"
    except Exception: pass
    return 0,""


# ══════════════════════════════════════════════════════════════════════
# SECTION 16 — HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════

def get_entry_tolerance(price: float, atr14: float = 0.0, gap_buffer: float = 0.01) -> tuple:
    if atr14>0 and price>0:
        atr_pct = atr14/price
        lo_pct  = max(0.005,min(0.05,atr_pct*0.8))
        hi_pct  = max(0.003,min(0.03,atr_pct*0.5))+gap_buffer
        return lo_pct,hi_pct
    if price<100:    return 0.030,0.025+gap_buffer
    elif price<300:  return 0.020,0.015+gap_buffer
    elif price<1000: return 0.012,0.010+gap_buffer
    else:            return 0.008,0.006+gap_buffer


def get_atr_stop_multiplier(price: float, sector: str = "") -> float:
    """
    FIX-GAP-06: Sector-aware ATR multiplier.
    Metals / commodities need wider stops (higher volatility regime);
    IT / FMCG can use tighter stops.
    Dead code: the ≥1000 branch was unreachable with PRICE_CAP=800 — removed.

    Base multiplier by price tier:
      <100  → 0.75  (micro-cap, tight range)
      <300  → 1.00  (small-cap standard)
      <800  → 1.40  (mid-cap standard)  ← PRICE_CAP ceiling

    Sector overlay applied on top of price-tier base:
      NIFTY METAL   → ×1.20  (high vol, gap risk)
      NIFTY PHARMA  → ×1.10  (FDA event risk)
      NIFTY AUTO    → ×1.05  (commodity input risk)
      NIFTY IT      → ×0.90  (lower historical ATR)
      NIFTY FMCG    → ×0.85  (defensive, low vol)
    """
    # Price-tier base
    if price < 100:   base = 0.75
    elif price < 300: base = 1.00
    else:             base = 1.40   # covers 300–800 (PRICE_CAP)

    # Sector overlay
    _SECTOR_ATR_OVERLAY: dict = {
        "NIFTY METAL":  1.20,
        "NIFTY PHARMA": 1.10,
        "NIFTY AUTO":   1.05,
        "NIFTY IT":     0.90,
        "NIFTY FMCG":   0.85,
    }
    overlay = _SECTOR_ATR_OVERLAY.get(sector, 1.0)
    return round(base * overlay, 3)


def check_smallcap_circuit_breaker() -> tuple:
    """
    FIX-AUDIT-13: CB_FAIL_SAFE env var controls behaviour on fetch failure.
    CB_FAIL_SAFE=true  → block entries conservatively when data is unavailable.
    CB_FAIL_SAFE=false → pass entries (original behaviour, default for backwards compat).
    """
    if "result" in _smallcap_index_cache:
        return _smallcap_index_cache["result"]

    # Default result now depends on CB_FAIL_SAFE setting
    if CB_FAIL_SAFE:
        fail_result = (True, "⚠️ Smallcap CB: data fetch FAILED — entries BLOCKED (CB_FAIL_SAFE=true)")
    else:
        fail_result = (False, "Smallcap circuit breaker: data unavailable (pass — set CB_FAIL_SAFE=true to block)")

    result = fail_result
    try:
        import yfinance as yf
        df = yf.download("^CNXSC", period="60d", progress=False, auto_adjust=True)
        if df.empty:
            df = yf.download("NIFTYSMLCAP100.NS", period="60d", progress=False, auto_adjust=True)
        if not df.empty and len(df) >= 20:
            closes = df["Close"].squeeze().values
            ma20   = float(np.mean(closes[-20:]))
            last   = float(closes[-1])
            if last < ma20:
                pct_below = (ma20-last)/ma20*100
                result = (True, f"⚠️ SMALLCAP CIRCUIT BREAKER ACTIVE — Nifty Smallcap 100 {pct_below:.1f}% below 20-DMA")
            else:
                pct_above = (last-ma20)/ma20*100
                result = (False, f"Smallcap healthy — {pct_above:.1f}% above 20-DMA ✓")
    except Exception as e:
        log.debug(f"Circuit breaker: {e}")
        result = fail_result   # use the configured fail behaviour

    _smallcap_index_cache["result"] = result
    return result


def earnings_safety_score(symbol: str, earnings_cal: dict) -> tuple:
    days = earnings_cal.get(symbol.upper())
    if days is None: return 20,"No result date found (neutral)"
    if days<0:
        recency=abs(days)
        if recency<=5:   return 28,f"Results just {recency}td ago — fresh data"
        elif recency<=21: return 25,f"Results {recency}td ago — clear runway"
        else:             return 20,f"Results {recency}td ago"
    else:
        if days<=2:   return 5, f"⚠️ Results in {days}td — SIZE SMALL (10%)"
        elif days<=5: return 10,f"⚠️ Results in {days}td — risky, size to PROBE"
        elif days<=10: return 18,f"Results in {days}td — caution"
        elif days<=21: return 24,f"Results in {days}td — acceptable window"
        else:          return 30,f"Results in {days}td — safe runway ✓"


def _get_vix_now() -> float:
    try:
        import yfinance as yf
        vdf = yf.download("^INDIAVIX", period="5d", progress=False, auto_adjust=True)
        if not vdf.empty:
            return float(vdf["Close"].squeeze().iloc[-1])
    except Exception: pass
    return 18.0


# ══════════════════════════════════════════════════════════════════════
# SECTION 17 — v5.7 FEATURE FUNCTIONS
# ══════════════════════════════════════════════════════════════════════

def calc_position_size(close: float, atr14: float, atr_mult: float) -> dict:
    if atr14<=0 or atr_mult<=0 or close<=0:
        return {"pos_shares":0,"pos_amount":0.0,"pos_label":"—"}
    risk_rupees = ACCOUNT_RISK_PCT*ACCOUNT_EQUITY
    shares      = max(1,int(risk_rupees/(atr14*atr_mult)))
    amount      = round(shares*close,2)
    return {"pos_shares":shares,"pos_amount":amount,
            "pos_label":f"{shares} shares · ₹{amount:,.0f}  (risk ₹{risk_rupees:,.0f})"}


def calc_trailing_stop(symbol: str, close: float, t3_initial: float,
                       atr14: float, atr_mult: float) -> dict:
    pos = _get_position(symbol)
    if pos is None:
        return {"trailing_stop":round(t3_initial,2),
                "trailing_label":f"Initial stop ₹{t3_initial:.2f}","trail_be_active":False}
    entry_price  = pos["entry_price"]; initial_t3=pos["initial_t3"]
    peak_price   = max(pos["peak_price"],close); be_triggered=bool(pos["be_triggered"])
    be_threshold = entry_price+1.0*atr14
    if not be_triggered and close>=be_threshold: be_triggered=True
    if be_triggered:
        trail_t3 = max(entry_price+0.25*atr14, peak_price-1.5*atr14)
        label    = f"Trail ₹{trail_t3:.2f}  BE+0.25ATR floor"
    else:
        trail_t3 = max(initial_t3, peak_price-atr_mult*atr14)
        label    = f"Trail ₹{trail_t3:.2f}  ratchet"
    trail_t3 = round(max(trail_t3,0.01),2)
    _put_position(symbol,entry_price,pos["entry_date"],initial_t3,peak_price,trail_t3,int(be_triggered))
    return {"trailing_stop":trail_t3,"trailing_label":label,"trail_be_active":be_triggered,"trail_peak":round(peak_price,2)}


def calc_cvd_divergence(hist: pd.DataFrame, close: float) -> dict:
    if len(hist)<12: return {"cvd_signal":"NEUTRAL","cvd_label":"","cvd_bonus":0}
    h = hist.copy()
    h["cvd_bar"] = h.apply(lambda r: float(r["volume"]) if r["close"]>r["open"] else -float(r["volume"]),axis=1)
    h["cvd"]     = h["cvd_bar"].cumsum()
    window       = 10
    cvd_now=float(h["cvd"].iloc[-1]); cvd_10d=float(h["cvd"].iloc[-window-1])
    px_now=float(h["close"].iloc[-1]); px_10d=float(h["close"].iloc[-window-1])
    cvd_chg=cvd_now-cvd_10d; px_chg=px_now-px_10d
    if px_chg>0 and cvd_chg<0:   return {"cvd_signal":"DISTRIBUTION","cvd_label":"🔴 CVD Diverge — distribution","cvd_bonus":-5}
    elif px_chg<=0 and cvd_chg>0: return {"cvd_signal":"ACCUMULATION","cvd_label":"🟢 CVD Accum — smart money","cvd_bonus":+5}
    return {"cvd_signal":"NEUTRAL","cvd_label":"","cvd_bonus":0}


def calc_vsa_absorption(hist: pd.DataFrame, atr14: float, adv20: float) -> dict:
    """
    FIX-AUDIT-12: mixed-signal (bullish AND bearish bars present) now returns
    NEUTRAL with 0 bonus instead of incorrectly penalising when net<0 despite
    bullish absorption being present.  Logic:
      net > 0 → bullish dominant  → BULLISH signal (+bonus)
      net < 0 → bearish dominant  → BEARISH signal (−penalty)
      net = 0 → conflicting       → NEUTRAL (0 pts)
    """
    if len(hist)<5 or atr14<=0 or adv20<=0:
        return {"vsa_absorption":False,"vsa_label":"","vsa_bonus":0}
    bullish_bars = 0; bearish_bars = 0
    for _, row in hist.tail(5).iterrows():
        spread = float(row["high"])-float(row["low"]); vol=float(row["volume"])
        cl=float(row["close"]); lo=float(row["low"]); hi=float(row["high"])
        bar_rng=hi-lo
        if bar_rng<=0: continue
        close_pct=(cl-lo)/bar_rng
        bullish_absorb = spread<0.5*atr14 and vol>1.5*adv20 and close_pct>=0.60
        bearish_absorb = spread<0.5*atr14 and vol>1.5*adv20 and close_pct<=0.40
        if bullish_absorb: bullish_bars+=1
        elif bearish_absorb: bearish_bars+=1

    net = bullish_bars - bearish_bars

    if net > 0:   # bullish dominant
        return {"vsa_absorption":True,
                "vsa_signal":"BULLISH",
                "vsa_label":f"🟢 VSA Bullish Absorption ({bullish_bars} bar{'s' if bullish_bars>1 else ''})",
                "vsa_bonus":min(8, bullish_bars*4)}
    elif net < 0: # bearish dominant
        return {"vsa_absorption":False,
                "vsa_signal":"BEARISH",
                "vsa_label":f"🔴 VSA Distribution ({bearish_bars} bar{'s' if bearish_bars>1 else ''})",
                "vsa_bonus":-min(4, bearish_bars*2)}
    else:         # FIX-AUDIT-12: mixed / neutral — conflicting signals cancel
        return {"vsa_absorption":False,
                "vsa_signal":"NEUTRAL",
                "vsa_label":("VSA conflicting signals" if bullish_bars>0 else ""),
                "vsa_bonus":0}


def calc_momentum_exhaustion(hist: pd.DataFrame, rsi_v: float, close: float, adv20: float) -> dict:
    if len(hist)<12 or adv20<=0:
        return {"exhaustion_flag":False,"exhaustion_label":"","exhaustion_penalty":0}
    warnings_=[]; penalty=0
    if rsi_v>75: warnings_.append(f"RSI {rsi_v:.1f}>75"); penalty+=5
    if len(hist)>=11:
        px_10=float(hist["close"].iloc[-11])
        rsi_s=calc_rsi(hist["close"]); rsi_10=float(rsi_s.iloc[-11]) if len(rsi_s)>=11 else rsi_v
        if close>px_10 and rsi_v<rsi_10: warnings_.append("RSI bearish divergence"); penalty+=8
    last_vol=float(hist["volume"].iloc[-1])
    if last_vol>3*adv20: warnings_.append(f"Vol climax {last_vol/adv20:.1f}×ADV"); penalty+=5
    if len(warnings_)>=2:
        return {"exhaustion_flag":True,"exhaustion_label":"⚠️ EXHAUSTION_WARNING — "+" · ".join(warnings_),"exhaustion_penalty":penalty}
    return {"exhaustion_flag":False,"exhaustion_label":"","exhaustion_penalty":0}


def calc_exit_liquidity(hist: pd.DataFrame, close: float, rsi_v: float,
                        vpoc: float, atr14: float, adv20: float) -> dict:
    if len(hist)<2 or atr14<=0 or adv20<=0:
        return {"exit_liq_score":0,"exit_liq_flag":False,"exit_liq_label":""}
    last=hist.iloc[-1]
    op=float(last["open"]); hi=float(last["high"]); lo=float(last["low"])
    cl=float(last["close"]); vol=float(last["volume"])
    body=abs(cl-op); wick=hi-max(cl,op); score=0; sigs=[]
    if vol>2*adv20 and cl<op: score+=1; sigs.append("vol spike on red")
    if cl<op and vol>1.5*adv20: score+=1; sigs.append("close<open high vol")
    if body>0 and wick>2*body: score+=1; sigs.append("upper wick 2×body")
    if rsi_v>70: score+=1; sigs.append(f"RSI {rsi_v:.0f}>70")
    if vpoc>0 and close>vpoc+3*atr14: score+=1; sigs.append("close>VPOC+3ATR")
    flag  = score>=3
    label = (f"🚨 EXIT_LIQUIDITY ({score}/5) — "+" · ".join(sigs)) if flag else ""
    return {"exit_liq_score":score,"exit_liq_flag":flag,"exit_liq_label":label}


def calc_fog_enhanced(adx_v: float, adx_prev: float, vix_now: float,
                      ma50: float, ma200: float, w52_bonus: int) -> dict:
    """
    FIX-AUDIT-11: VIX threshold now reads SNIPER_CFG["vix_fog"] (=20.0)
    instead of the hardcoded literal 20.  This is the single source of
    truth aligned with vix_panic (22) and vix_chop (15).
    """
    fog_score=0; reasons=[]
    ranging = adx_v <= 18.0
    if ranging and adx_v<adx_prev: fog_score+=1; reasons.append(f"ADX {adx_v:.1f}↓")
    # FIX-AUDIT-11: use SNIPER_CFG["vix_fog"] — never hardcode
    vix_fog_threshold = SNIPER_CFG["vix_fog"]
    if vix_now > vix_fog_threshold:
        fog_score+=1; reasons.append(f"VIX {vix_now:.1f}>{vix_fog_threshold:.0f}")
    if ma200>0 and ma50>0:
        ma_diff_pct=abs(ma50-ma200)/ma200
        if ma_diff_pct<=0.03: fog_score+=1; reasons.append(f"MA compressed {ma_diff_pct*100:.1f}%")
    if ranging and w52_bonus==0: fog_score+=1; reasons.append("no 52W coil")
    if fog_score>=3: tier="FOG_SEVERE"; block=True
    elif fog_score>=2: tier="FOG_WARNING"; block=True
    else: tier="CLEAR"; block=False
    label = (f"🌫️ {tier} — "+" · ".join(reasons)) if block else ""
    return {"fog_tier":tier,"fog_block":block,"fog_label":label,"fog_score":fog_score}


def calc_bayesian_score(adx_v: float, mfi_v: float, cvd_signal: str,
                        layer3: bool, fii_pts: int, vix_now: float) -> dict:
    prior  = 0.35
    nodes  = [
        (adx_v>=25.0,   0.72,0.30),(mfi_v<=45.0,0.68,0.40),
        (cvd_signal=="ACCUMULATION",0.75,0.45),(layer3,0.70,0.35),
        (fii_pts>=22,   0.65,0.40),(vix_now<15.0,0.60,0.45),
    ]
    posterior = prior
    for condition,p_true,p_false in nodes:
        likelihood = p_true if condition else p_false
        posterior  = (likelihood*posterior)/max(1e-9,likelihood*posterior+(1-likelihood)*(1-posterior))
    posterior = round(posterior,3); bayes_pct=round(posterior*100)
    if posterior>=0.70: bonus=10; label=f"🧠 Bayes {bayes_pct}% — HIGH conviction"
    elif posterior>=0.55: bonus=5; label=f"🧠 Bayes {bayes_pct}% — moderate"
    elif posterior>=0.40: bonus=0; label=f"🧠 Bayes {bayes_pct}% — neutral"
    else: bonus=-5; label=f"🧠 Bayes {bayes_pct}% — LOW conviction"
    return {"bayes_prob":bayes_pct,"bayes_bonus":bonus,"bayes_label":label}


def calc_dynamic_score_weights(fii_data: dict, vix_now: float) -> dict:
    """
    FIX-AUDIT-09: dyn_max is now capped at MAX_SCORE (200).
    The old code could return dyn_max=205 when insider weight was boosted
    to 35, creating a phantom ceiling that the min(dyn_max, total) line
    never actually enforced (since components could never sum beyond 200).
    """
    weights=dict(SCORE_WEIGHTS); reasons=[]
    if vix_now > SNIPER_CFG["vix_fog"]:
        weights["fii_dii"]  = 30
        weights["fortress"] = max(60, weights["fortress"]-10)
        reasons.append(f"VIX>{SNIPER_CFG['vix_fog']:.0f}: FII weight prioritised")
    fii_score = fii_data.get("score",15)
    if fii_score < 10:
        weights["insider"] = min(30, weights.get("insider",30)+5)  # cap at actual component max
        weights["filing"]  = max(20, weights.get("filing",30)-5)
        reasons.append("FII selling: insider weight intent +5")
    # FIX-AUDIT-09: cap at MAX_SCORE — components can never exceed their
    # individual caps (each=30, fortress=80+30forward), so dyn_max > 200
    # was a phantom that made the min() guard ineffective.
    dyn_max = min(MAX_SCORE, sum(weights.values()))
    label   = " | ".join(reasons) if reasons else "Standard weights"
    return {"dyn_weights":weights,"dyn_max":dyn_max,"dyn_label":label}


def calc_monte_carlo_survival(hist: pd.DataFrame, t3: float,
                               n_sims: int = 500, horizon: int = 20) -> dict:
    """
    FIX-AUDIT-16: Student-t distribution (df=MC_FAT_TAILS_DF, default 4)
    replaces Normal when MC_FAT_TAILS=true (default).  Indian market returns
    exhibit fat tails and circuit-breaker gaps that Normal distribution
    systematically underestimates.  t(df=4) has excess kurtosis ≈1.5 which
    better matches empirical NSE daily return distributions.

    FIX-GAP-04: Convergence check.  500 sims at 20-bar horizon produce ±5%
    natural variance, making the survival% unreliable for position sizing.
    We now run two independent half-batches and compare; if the gap > 8pp
    we flag mc_converged=False and add a caution note to mc_label.
    """
    if len(hist)<30 or t3<=0:
        return {"mc_survival_pct":None,"mc_label":"MC: insufficient data","mc_converged":None}
    try:
        closes    = hist["close"].values.astype(float)
        log_ret   = np.diff(np.log(closes[closes>0]))
        if len(log_ret)<10:
            return {"mc_survival_pct":None,"mc_label":"MC: insufficient returns","mc_converged":None}
        mu, sigma = float(np.mean(log_ret)), float(np.std(log_ret))
        current   = float(closes[-1])
        rng       = np.random.default_rng()

        def _run_batch(n: int) -> int:
            batch_survived = 0
            if MC_FAT_TAILS:
                df      = MC_FAT_TAILS_DF
                t_scale = sigma * math.sqrt((df-2)/df) if df > 2 else sigma
                for _ in range(n):
                    raw_ret = rng.standard_t(df, size=horizon)
                    ret     = np.cumsum(mu + t_scale * raw_ret)
                    path    = current * np.exp(ret)
                    if float(np.min(path)) > t3:
                        batch_survived += 1
            else:
                for _ in range(n):
                    ret  = np.cumsum(rng.normal(mu, sigma, horizon))
                    path = current * np.exp(ret)
                    if float(np.min(path)) > t3:
                        batch_survived += 1
            return batch_survived

        half    = n_sims // 2
        surv_a  = _run_batch(half)
        surv_b  = _run_batch(n_sims - half)
        survived= surv_a + surv_b
        pct_a   = round(surv_a / half * 100, 1)
        pct_b   = round(surv_b / (n_sims - half) * 100, 1)
        pct     = round(survived / n_sims * 100, 1)

        # Convergence: two half-batches within 8pp → converged
        converged     = abs(pct_a - pct_b) <= 8.0
        model         = f"t(df={MC_FAT_TAILS_DF})" if MC_FAT_TAILS else "Normal"
        converge_note = "" if converged else f" ⚠️ HIGH VARIANCE ({pct_a}% vs {pct_b}% — use caution)"
        label = (f"✅ MC survival {pct}% ({n_sims} sims, {horizon}d, {model}){converge_note}" if pct>=70
                 else f"⚠️ MC survival {pct}% ({model}){converge_note}")
        return {"mc_survival_pct":pct,"mc_label":label,"mc_converged":converged}
    except Exception as e:
        return {"mc_survival_pct":None,"mc_label":f"MC error: {e}","mc_converged":None}


def calc_round_trip_guard(hist: pd.DataFrame, close: float, t1: float) -> dict:
    if len(hist)<20: return {"round_trip_risk":False,"round_trip_label":""}
    peak_20=float(hist["high"].tail(20).max())
    if close<peak_20*0.90 and close>t1:
        return {"round_trip_risk":True,"round_trip_label":f"⚠️ ROUND_TRIP_RISK — close {close:.0f} < 90% of {peak_20:.0f}"}
    return {"round_trip_risk":False,"round_trip_label":""}


# ══════════════════════════════════════════════════════════════════════
# SECTION 18 — FORTRESS CORE SCORING ENGINE
# ══════════════════════════════════════════════════════════════════════

def fortress_score(symbol: str, today_row, hist: pd.DataFrame) -> Optional[dict]:
    if len(hist) < CFG["min_hist_bars"]:
        return None

    close  = float(today_row["close"])
    volume = float(today_row.get("volume", hist["volume"].iloc[-1] if "volume" in hist.columns else 0))

    atr14_s  = calc_atr(hist, 14)
    atr14    = float(atr14_s.iloc[-1]) if not atr14_s.empty else 0.0
    rsi_v    = float(calc_rsi(hist["close"]).iloc[-1])
    mfi_v    = float(calc_mfi(hist, 14).iloc[-1])
    adx_v    = calc_adx(hist, 14)          # FIX-AUDIT-08: NaN-safe
    adx_prev = float(calc_adx(hist.iloc[:-1], 14)) if len(hist) > 14 else adx_v
    vpoc     = calc_vpoc(hist, lookback=252)

    # FIX-AUDIT-07: determine whether volume data is reliable before
    # using it to gate layer1 / layer2 signals.
    volume_reliable = _vpoc_volume_reliable(hist, lookback=63)

    adv20    = float(hist["volume"].tail(20).mean()) if len(hist) >= 20 else volume
    ma50_v   = float(hist["close"].tail(50).mean())  if len(hist) >= 50 else close
    ma200_v  = float(hist["close"].tail(200).mean()) if len(hist) >= 200 else close

    if len(hist) >= 21:
        close_20 = float(hist["close"].iloc[-21])
        velocity = (close - close_20) / close_20 * 100 if close_20 > 0 else 0.0
    else:
        velocity = 0.0

    if len(hist) >= 200:
        ma_ref = ma200_v; ma_label = "MA200"
    elif len(hist) >= 100:
        ma_ref = float(hist["close"].tail(100).mean()); ma_label = "MA100"
        log.debug(f"{symbol}: using MA100 fallback")
    else:
        ma_ref = ma50_v; ma_label = "MA50"
        log.debug(f"{symbol}: using MA50 fallback")

    below_tolerance = (close < ma_ref * (1 - CFG["ma200_tolerance"]))
    if below_tolerance and ma_label == "MA200":
        return None   # hard veto

    alt_pct  = ((close - ma_ref) / ma_ref * 100) if ma_ref > 0 else 0.0
    alt_warn = alt_pct > CFG["alt_warn_pct"]
    alt_stop = alt_pct > CFG["alt_stop_pct"]
    if alt_stop:
        return None

    sector      = get_sector(symbol)
    sector_mult = SECTOR_TRUTH.get(sector, 1.0)
    if sector in SECTOR_BLOCKED:
        return None

    # FIX-AUDIT-19: sector RS override only fires when sector index data
    # was successfully fetched.  Exception sets sect_20=None (not 0.0),
    # preventing the accidental unconditional boost that occurred when
    # velocity > 0 + 5 was always True after a silent exception.
    # FIX-GAP-09: Silent exceptions previously hid when sector boost failed.
    # Now logged at DEBUG so operators can detect persistent yfinance outages.
    sect_20: Optional[float] = None
    try:
        if sector in SECTOR_INDICES:
            import yfinance as yf
            idx_df = yf.download(f"^{SECTOR_INDICES[sector]}", period="30d",
                                  progress=False, auto_adjust=True)
            if not idx_df.empty and len(idx_df) >= 2:
                ic = idx_df["Close"].squeeze().values
                sect_20 = (ic[-1]-ic[-20])/ic[-20]*100 if len(ic)>=20 else None
                log.debug(f"{symbol}: sector RS ({sector}) = {sect_20:.2f}% 20d change")
            else:
                log.debug(f"{symbol}: sector RS ({sector}) — yfinance returned empty data; boost skipped")
    except Exception as _sect_exc:
        log.debug(f"{symbol}: sector RS ({sector}) fetch failed — {_sect_exc}; boost skipped")
        sect_20 = None

    if sect_20 is not None and velocity > sect_20 + 5.0:
        sector_mult = max(sector_mult, 1.0)

    turnover_lakhs = float(today_row.get("turnover_lakhs", 0))
    turnover_cr    = turnover_lakhs / 100
    if turnover_lakhs < CFG["turnover_lakhs"]:
        return None

    if mfi_v <= CFG["mfi_accum"]:   mfi_status = "ACCUMULATION 🟢"
    elif mfi_v >= CFG["mfi_dist"]:  mfi_status = "DISTRIBUTION 🔴"
    else:                            mfi_status = "NEUTRAL ↔"

    if adx_v >= CFG["adx_trend"]:   regime = "MOMENTUM"
    elif adx_v >= CFG["adx_range"]: regime = "TRANSITION"
    else:                            regime = "RANGING"

    lo_pct, hi_pct = get_entry_tolerance(close, atr14)
    t1         = round(vpoc, 2)
    entry_lo   = round(t1 * (1 - lo_pct), 2)
    entry_hi   = round(t1 * (1 + hi_pct), 2)
    entry_zone = "PRISTINE" if entry_lo <= close <= entry_hi else ("ABOVE" if close > entry_hi else "BELOW")
    entry_band = f"₹{entry_lo:.2f}–₹{entry_hi:.2f}"

    atr_mult  = get_atr_stop_multiplier(close, sector)
    t2        = round(close + CFG["atr_t2"] * atr14, 2)
    t3        = round(close - atr_mult * atr14, 2)
    if t3 <= 0: t3 = round(close * 0.93, 2)
    risk_pct_val = round((close - t3) / close * 100, 2) if close > 0 else 0
    r1 = round(close * 1.15, 2); r2 = round(close * 1.30, 2); r3 = round(close * 1.50, 2)
    rr = round((r1 - close) / (close - t3), 2) if (close - t3) > 0 else 0
    stop_note = (f"⚠️ {alt_pct:.0f}% above {ma_label} — altitude warning" if alt_warn
                 else f"ATR-{atr_mult:.2f}× stop")

    atr100       = float(calc_atr(hist, 100).iloc[-1]) if len(hist) >= 100 else atr14
    vol_contract = (adv20 > 0 and volume < adv20 * 0.8)
    vcp_coil     = ("TIGHT 🟢"
                    if atr14 > 0 and atr100 > 0 and (atr14/atr100) < 0.70 and vol_contract
                    else "LOOSE")

    # VDU: Volume Dry-Up signal
    # FIX-GAP-05: Price confirmation gate added.
    # VDU is a bullish coil signal ONLY when price is flat or rising.
    # VDU + falling price = distribution (quiet selling into declining mkt).
    # We check whether close is above the N-bar-ago close; if price has
    # dropped more than 1% over the VDU window, bonus is zeroed and label
    # is overridden to warn of distribution.
    vdu_bars = 0
    if len(hist) >= 6 and volume_reliable:
        for _vdu_n in range(3, 6):
            if all(
                float(hist["volume"].iloc[-(i+1)]) < float(hist["volume"].iloc[-(i+2)])
                for i in range(_vdu_n - 1)
            ):
                vdu_bars = _vdu_n

    if vdu_bars >= 3:
        # Check price direction over the VDU window
        _vdu_close_now  = float(hist["close"].iloc[-1])
        _vdu_close_start= float(hist["close"].iloc[-(vdu_bars + 1)])
        _vdu_price_chg  = (_vdu_close_now - _vdu_close_start) / _vdu_close_start if _vdu_close_start > 0 else 0.0
        _vdu_confirmed  = _vdu_price_chg >= -0.01  # price flat or rising (within -1% tolerance)
    else:
        _vdu_confirmed  = True  # no VDU, doesn't matter

    if not _vdu_confirmed:
        # Price drop during VDU window → distribution, not coil
        vdu_bonus = -3
        vdu_label = f"🔴 VDU {vdu_bars}-bar + price drop {_vdu_price_chg*100:.1f}% — DISTRIBUTION (-3pts)"
    elif vdu_bars >= 5: vdu_bonus=7; vdu_label=f"🌀 VDU {vdu_bars}-bar deep coil (+7pts)"
    elif vdu_bars >= 4: vdu_bonus=5; vdu_label=f"🌀 VDU {vdu_bars}-bar confirmed (+5pts)"
    elif vdu_bars >= 3: vdu_bonus=3; vdu_label=f"🌀 VDU {vdu_bars}-bar mild (+3pts)"
    else:               vdu_bonus=0; vdu_label=""

    # FIX-AUDIT-07: Layer 1 and Layer 2 require reliable volume data.
    # With zero-volume data, VPOC returns mid-range (not a real HVN), so
    # close will almost never be within 2% of it — but we enforce the
    # guard explicitly to prevent any future path that could inflate scores.
    if volume_reliable:
        layer1 = abs(close - vpoc) / vpoc <= 0.02 if vpoc > 0 else False
        layer2 = any(
            float(hist["volume"].iloc[-(i+1)]) >= CFG["vol_ratio"] * adv20
            for i in range(min(5, len(hist)))
        ) if adv20 > 0 else False
    else:
        layer1 = False   # cannot validate VPOC alignment without volume
        layer2 = False   # cannot validate volume spike without volume
        log.debug(f"{symbol}: layer1/layer2 suppressed — volume data unreliable")

    recency_bars  = min(CFG["recency_days"], len(hist))
    recent_hist   = hist.tail(recency_bars)
    vpoc_touches  = sum(1 for _, r in recent_hist.iterrows()
                        if abs(float(r["close"]) - vpoc) / vpoc <= 0.03) if vpoc > 0 else 0
    momentum_mode = (adx_v >= 25 and close > ma50_v)
    layer3        = vpoc_touches >= 2 or momentum_mode

    pts = 0.0
    if layer1: pts += 25
    elif abs(close - vpoc) / vpoc <= 0.05: pts += 15

    if layer2: pts += 20
    if layer3: pts += 15

    if regime == "MOMENTUM":    pts += 10
    elif regime == "TRANSITION": pts += 5

    if mfi_v <= 40: pts += 8
    elif mfi_v <= 50: pts += 4

    if vcp_coil == "TIGHT 🟢": pts += 5

    pts *= sector_mult

    if below_tolerance and ma_label != "MA200":
        pts *= 0.85

    if alt_pct > CFG["alt_warn_pct"]:
        excess_bands = min(5, int((alt_pct - CFG["alt_warn_pct"]) / 5))
        alt_pen      = 0.80 * (0.92 ** excess_bands)
        pts         *= alt_pen
    elif alt_pct > 30.0:
        pts *= 0.92

    w52_bonus,  w52_label  = calc_52w_compression(hist, close, atr14)
    atrv_bonus, atrv_label = calc_atr_velocity(hist)
    forward_bonus = w52_bonus + atrv_bonus + vdu_bonus
    pts += forward_bonus

    pts = min(int(pts), SCORE_WEIGHTS["fortress"] + 30)

    return {
        "fortress_pts":    pts,
        "layer1":          layer1,
        "layer2":          layer2,
        "layer3":          layer3,
        "volume_reliable": volume_reliable,
        "vcp_coil":        vcp_coil,
        "entry_zone":      entry_zone,
        "entry_band":      entry_band,
        "stop_note":       stop_note,
        "atr_mult":        atr_mult,
        "alt_pct":         round(alt_pct, 2),
        "sector_mult":     round(sector_mult, 3),
        "regime":          regime,
        "mfi":             round(mfi_v, 1),
        "mfi_status":      mfi_status,
        "rsi":             round(rsi_v, 1),
        "adx":             round(adx_v, 1),
        "adx_prev":        round(adx_prev, 1),
        "t1":              t1,  "t2": t2,  "t3": t3,
        "r1":              r1,  "r2": r2,  "r3": r3,
        "risk_pct":        risk_pct_val,
        "rr":              rr,
        "atr14_val":       round(atr14, 2),
        "adv20_val":       round(adv20, 0),
        "vpoc_val":        round(vpoc, 2),
        "ma50_val":        round(ma50_v, 2),
        "ma200_val":       round(ma200_v, 2),
        "ma_label":        ma_label,
        "turnover_cr":     round(turnover_cr, 2),
        "w52_bonus":       w52_bonus,  "w52_label":  w52_label,
        "atrv_bonus":      atrv_bonus, "atrv_label": atrv_label,
        "forward_bonus":   forward_bonus,
        "vdu_bonus":       vdu_bonus,  "vdu_label":  vdu_label,  "vdu_bars": vdu_bars,
        "momentum_velocity_pct": round(velocity, 2),
    }


# ══════════════════════════════════════════════════════════════════════
# SECTION 19 — PAPER MODE
# ══════════════════════════════════════════════════════════════════════

def paper_score(symbol: str, hist: pd.DataFrame, close: float) -> dict:
    if len(hist) < 30:
        return {"paper_total":0,"paper_momentum":0,"paper_volume":0,"paper_vpoc":0}
    vpoc  = calc_vpoc(hist, lookback=252)
    atr14 = float(calc_atr(hist, 14).iloc[-1])
    adv20 = float(hist["volume"].tail(20).mean())
    vol   = float(hist["volume"].iloc[-1])
    mfi_v = float(calc_mfi(hist, 14).iloc[-1])
    rsi_v = float(calc_rsi(hist["close"]).iloc[-1])
    close_20   = float(hist["close"].iloc[-21]) if len(hist) >= 21 else close
    mom_20     = (close-close_20)/close_20*100 if close_20 > 0 else 0
    mom_score  = (25 if mom_20>10 else 15 if mom_20>5 else 10 if mom_20>0 else 3)
    rsi_score  = (25 if 40<=rsi_v<=65 else 15 if 35<=rsi_v<=70 else 5)
    paper_momentum = min(50, mom_score + rsi_score)
    vol_score  = (15 if adv20>0 and vol>=adv20*1.5 else 8 if vol>=adv20 else 0)
    vol_score += (10 if mfi_v<=45 else 5 if mfi_v<=55 else 0)
    paper_volume   = min(25, vol_score)
    dist     = abs(close-vpoc)/atr14 if atr14 > 0 else 99
    vpoc_score = 25 if dist<=0.5 else 18 if dist<=1.0 else 10 if dist<=2.0 else 2
    return {"paper_total":paper_momentum+paper_volume+vpoc_score,
            "paper_momentum":paper_momentum,"paper_volume":paper_volume,"paper_vpoc":vpoc_score}


# ══════════════════════════════════════════════════════════════════════
# SECTION 20 — ASSEMBLE UNIFIED RESULT
# ══════════════════════════════════════════════════════════════════════

def get_rank(total: int) -> tuple:
    for threshold, label, alloc in RANKS:
        if total >= threshold and label:
            return label, alloc
    return None, None


def build_story(r: dict) -> str:
    parts = []
    fii = r.get("fii_label","")
    if "BUYING" in fii and "FII+DII" in fii: parts.append("institutional tide in — FII+DII both accumulating")
    elif "FII BUYING" in fii: parts.append("foreign money flowing in — FII net buyers")
    ins = r.get("insider_detail","")
    if ins and "buy" in ins.lower(): parts.append(f"insiders putting own money in ({ins[:50]})")
    fil = r.get("filing_detail","")
    if fil and fil != "—": parts.append(f"recent filing: {fil[:50]}")
    earn = r.get("earnings_detail","")
    if "safe runway" in earn: parts.append("clear of earnings risk")
    elif "just announced" in earn: parts.append("fresh results — full visibility")
    elif "SIZE SMALL" in earn: parts.append("⚠️ near earnings — size to 10%")
    l1,l2,l3 = r.get("layer1",False),r.get("layer2",False),r.get("layer3",False)
    layers=sum([l1,l2,l3]); vcp=r.get("vcp_coil",""); mult=r.get("sector_mult",1.0)
    if layers==3 and "TIGHT" in vcp: parts.append("all 3 Fortress layers + VCP coil")
    elif layers>=2: parts.append(f"{layers}/3 Fortress layers at VPOC floor")
    elif "TIGHT" in vcp: parts.append("VCP coil tightening at support")
    if mult>=1.10: parts.append(f"Sector Truth boost ({mult:.2f}x)")
    w52=r.get("w52_label","")
    if "ELITE COIL" in w52: parts.append("price coiling at 52W high — breakout imminent 🎯")
    pead=r.get("pead_label","")
    if "HOT PEAD" in pead: parts.append("post-earnings drift active 🔥")
    if not parts: parts.append(f"Fortress score {r.get('score_fortress',0)}/80 — setup active")
    return "; ".join(parts[:4]).capitalize()


def assemble_result(symbol: str, today_row, hist: pd.DataFrame,
                    fii_data: dict, insider_map: dict,
                    filings: dict, earnings_cal: dict,
                    vix_now_cached: float = None) -> Optional[dict]:

    ins_det="No insider trades in 30d"; fil_det="No recent filing"
    earn_det="—"; ins_pts=0; fil_pts=15; earn_pts=0
    roce_val, roce_label = None,"Not checked"

    fort = fortress_score(symbol, today_row, hist)
    if fort is None:
        return None

    dq = str(today_row.get("data_quality",""))
    if dq in ("SNAPSHOT_FALLBACK","STALE") and fort["fortress_pts"] > 55:
        fort["fortress_pts"] = 55

    fii_pts = fii_data.get("score",15)
    fii_lbl = fii_data.get("label","—")
    fii_det = fii_data.get("detail","—")

    ins_data = insider_map.get(symbol.upper(), {})
    ins_pts  = ins_data.get("score",  ins_pts)
    ins_det  = ins_data.get("detail", ins_det)

    fil_data = filings.get(symbol.upper(), {})
    fil_pts  = fil_data.get("score",  fil_pts)
    fil_det  = fil_data.get("detail", fil_det)

    price = float(today_row["close"])
    _is_yfinance_fallback = (
        str(today_row.get("data_quality","")) in ("SNAPSHOT_FALLBACK","STALE")
        or FORCE_YFINANCE
    )
    if fil_pts > 15 and not _is_yfinance_fallback:
        roce_val, roce_label = fetch_roce_proxy(symbol)
        if roce_val is None:
            fil_pts = min(fil_pts, 10); fil_det = f"{fil_det} | ⚠️ ROE(proxy) unverifiable — filing capped"
        elif roce_val < 5.0:
            fil_pts = min(fil_pts, 8);  fil_det = f"{fil_det} | ❌ {roce_label} — weak fundamentals"
        else:
            fil_det = f"{fil_det} | ✅ {roce_label}"

    earn_pts, earn_det = earnings_safety_score(symbol, earnings_cal)
    pead_bonus, pead_label = calc_pead_bonus(symbol, earnings_cal, hist)
    earn_pts = min(30, earn_pts + pead_bonus)

    total = fort["fortress_pts"] + fii_pts + ins_pts + fil_pts + earn_pts
    total = min(total, MAX_SCORE)

    rank, alloc = get_rank(total)
    if rank is None:
        return None

    close    = float(today_row["close"])
    atr14    = fort.get("atr14_val", 0.0)
    atr_mult = fort.get("atr_mult", 1.75)
    adv20    = fort.get("adv20_val", 1.0)
    adx_v    = fort.get("adx", 20.0)
    adx_prev = fort.get("adx_prev", adx_v)
    mfi_v    = fort.get("mfi", 50.0)
    rsi_v    = fort.get("rsi", 50.0)
    layer3   = fort.get("layer3", False)
    w52_bonus= fort.get("w52_bonus", 0)
    vpoc_val = fort.get("vpoc_val", close)
    t3_val   = fort.get("t3", 0.0)
    ma50_val = fort.get("ma50_val", 0.0)
    ma200_val= fort.get("ma200_val", 0.0)

    vix_now  = vix_now_cached if vix_now_cached is not None else _get_vix_now()

    # FIX-GAP-03: volume_reliable propagated from fortress_score so that
    # calc_cvd_divergence, calc_momentum_exhaustion, and calc_exit_liquidity
    # do not produce false signals when underlying volume data is absent.
    volume_reliable = fort.get("volume_reliable", True)

    if volume_reliable:
        cvd  = calc_cvd_divergence(hist, close)
        exh  = calc_momentum_exhaustion(hist, rsi_v, close, adv20 if adv20>0 else 1.0)
        exlq = calc_exit_liquidity(hist, close, rsi_v, vpoc_val,
                                   atr14 if atr14>0 else 1.0, adv20 if adv20>0 else 1.0)
    else:
        log.debug(f"{symbol}: CVD/exhaustion/exit-liq suppressed — volume data unreliable")
        cvd  = {"cvd_signal": "NEUTRAL", "cvd_label": "⚠️ Volume unreliable — CVD skipped", "cvd_bonus": 0}
        exh  = {"exhaustion_flag": False, "exhaustion_label": "", "exhaustion_penalty": 0}
        exlq = {"exit_liq_score": 0, "exit_liq_flag": False, "exit_liq_label": ""}

    vsa      = calc_vsa_absorption(hist, atr14 if atr14>0 else 1.0, adv20 if adv20>0 else 1.0)
    fog_enh  = calc_fog_enhanced(adx_v, adx_prev, vix_now, ma50_val, ma200_val, w52_bonus)
    dyn      = calc_dynamic_score_weights(fii_data, vix_now)

    score_adj = cvd["cvd_bonus"] + vsa.get("vsa_bonus",0) - exh["exhaustion_penalty"]
    total     = min(dyn["dyn_max"], max(0, total + score_adj))

    rank, alloc = get_rank(total)
    if rank is None:
        return None

    mc    = calc_monte_carlo_survival(hist, t3_val)
    pos   = calc_position_size(close, atr14 if atr14>0 else 1.0, atr_mult)
    trail = calc_trailing_stop(symbol, close, t3_val, atr14 if atr14>0 else 1.0, atr_mult)

    result = {
        "symbol":          symbol,
        "sector":          get_sector(symbol),
        "close":           round(close, 2),
        "total_score":     total,
        "max_score":       dyn["dyn_max"],
        "rank":            rank,
        "alloc":           alloc,
        "score_fortress":  fort["fortress_pts"],
        "score_fii":       fii_pts,
        "score_insider":   ins_pts,
        "score_filing":    fil_pts,
        "score_earnings":  earn_pts,
        "t1":  fort["t1"], "t2": fort["t2"], "t3": fort["t3"],
        "r1":  fort["r1"], "r2": fort["r2"], "r3": fort["r3"],
        "risk_pct":        fort["risk_pct"],
        "rr":              fort["rr"],
        "mfi":             fort["mfi"],
        "rsi":             fort["rsi"],
        "adx":             fort["adx"],
        "regime":          fort["regime"],
        "vcp_coil":        fort["vcp_coil"],
        "mfi_status":      fort["mfi_status"],
        "entry_zone":      fort["entry_zone"],
        "entry_band":      fort.get("entry_band","—"),
        "stop_note":       fort.get("stop_note","—"),
        "atr_mult":        atr_mult,
        "alt_pct":         fort["alt_pct"],
        "layer1":          fort["layer1"],
        "layer2":          fort["layer2"],
        "layer3":          layer3,
        "volume_reliable": fort.get("volume_reliable", True),
        "turnover_cr":     fort["turnover_cr"],
        "ma_label":        fort.get("ma_label","MA200"),
        "sector_mult":     fort.get("sector_mult",1.0),
        "fii_label":       fii_lbl,
        "fii_detail":      fii_det,
        "insider_detail":  ins_det,
        "filing_detail":   fil_det,
        "earnings_detail": earn_det,
        "roce_label":      roce_label,
        "halal":           True,
        "w52_label":       fort.get("w52_label","—"),
        "w52_bonus":       w52_bonus,
        "atrv_label":      fort.get("atrv_label","—"),
        "atrv_bonus":      fort.get("atrv_bonus",0),
        "vdu_label":       fort.get("vdu_label",""),
        "vdu_bonus":       fort.get("vdu_bonus",0),
        "vdu_bars":        fort.get("vdu_bars",0),
        "forward_bonus":   fort.get("forward_bonus",0),
        "pead_label":      pead_label,
        "pead_bonus":      pead_bonus,
        "data_quality":    dq,
        "fog_block":       fog_enh["fog_block"],
        "fog_label":       fog_enh["fog_label"],
        "fog_tier":        fog_enh["fog_tier"],
        "trailing_stop":   trail["trailing_stop"],
        "trailing_label":  trail["trailing_label"],
        "trail_be_active": trail["trail_be_active"],
        "pos_shares":      pos["pos_shares"],
        "pos_amount":      pos["pos_amount"],
        "pos_label":       pos["pos_label"],
        "bayes_prob":      0,
        "bayes_bonus":     0,
        "bayes_label":     "— (see sn_bayes_label for 9-node result)",
        "mc_survival_pct": mc["mc_survival_pct"],
        "mc_label":        mc["mc_label"],
        "cvd_signal":      cvd["cvd_signal"],
        "cvd_label":       cvd["cvd_label"],
        "vsa_absorption":  vsa["vsa_absorption"],
        "vsa_label":       vsa["vsa_label"],
        "vsa_bonus":       vsa.get("vsa_bonus",0),
        "dyn_weight_label":dyn["dyn_label"],
        "exhaustion_flag": exh["exhaustion_flag"],
        "exhaustion_label":exh["exhaustion_label"],
        "exit_liq_flag":   exlq["exit_liq_flag"],
        "exit_liq_label":  exlq["exit_liq_label"],
        "exit_liq_score":  exlq["exit_liq_score"],
        "momentum_velocity_pct": fort.get("momentum_velocity_pct",0.0),
        "atr14_val":       fort.get("atr14_val",0.0),
        "adv20_val":       fort.get("adv20_val",0.0),
        "vpoc_val":        fort.get("vpoc_val",0.0),
    }

    if price < PRICE_CAP:
        cb_active, cb_msg = check_smallcap_circuit_breaker()
        result["alloc"]           = "PROBE 10% ⚠️ CB" if cb_active else result["alloc"]
        result["circuit_breaker"] = cb_msg
    else:
        result["circuit_breaker"] = "N/A (≥₹800 excluded by PRICE_CAP)"

    result["story"] = build_story(result)
    rt_guard = calc_round_trip_guard(hist, close, result["t1"])
    result.update(rt_guard)

    # Graduated FOG sizing
    if result.get("fog_block"):
        fog_tier = result.get("fog_tier","FOG_WARNING")
        if fog_tier == "FOG_SEVERE":
            fog_deploy_cap = 10; fog_alloc_label = "PROBE 10% 🌫️ SEVERE"
        else:
            fog_deploy_cap = 25; fog_alloc_label = "QTR 25% 🌫️ WARNING"
        result["alloc"] = fog_alloc_label
        current_deploy  = result.get("sniper_deploy",0) or 0
        result["sniper_deploy"] = min(current_deploy, fog_deploy_cap)
        existing_dir = result.get("sniper_directive","")
        if "FOG" not in existing_dir:
            result["sniper_directive"] = (
                f"🌫️ {fog_tier} — CAUTION ({fog_deploy_cap}% max deploy)\n  ({existing_dir})"
            )

    if PAPER_MODE:
        result.update(paper_score(symbol, hist, close))

    return result


# ══════════════════════════════════════════════════════════════════════
# SECTION 21 — v6.0 SNIPER HYBRID SYSTEMS (SN-1 through SN-7)
# ══════════════════════════════════════════════════════════════════════

# ── SN-4: Macro Regime ─────────────────────────────────────────────

def fetch_macro_regime() -> dict:
    try:
        import yfinance as yf
        vix_df   = yf.download("^INDIAVIX", period="5d",  progress=False, auto_adjust=True)
        nifty_df = yf.download("^NSEI",     period="10d", progress=False, auto_adjust=True)
        cnx_df   = yf.download("^CNX500",   period="60d", progress=False, auto_adjust=True)
    except Exception as e:
        log.debug(f"Macro regime fetch failed: {e}")
        return {"macro_state":"CHOP","breadth_ok":True,"vix_val":18.0,
                "nifty_chg":0.0,"cnx500_below_ma50":False}

    vix_val = 18.0
    if not vix_df.empty:
        try: vix_val = float(vix_df["Close"].squeeze().iloc[-1])
        except: pass

    nifty_chg = 0.0
    if not nifty_df.empty and len(nifty_df) >= 2:
        try:
            nc = nifty_df["Close"].squeeze().values
            nifty_chg = (nc[-1]-nc[-2])/nc[-2]*100
        except: pass

    cnx500_below_ma50 = False
    if not cnx_df.empty and len(cnx_df) >= 50:
        try:
            cc = cnx_df["Close"].squeeze()
            cnx500_below_ma50 = float(cc.iloc[-1]) < float(cc.rolling(50).mean().iloc[-1])
        except: pass

    breadth_ok = not cnx500_below_ma50

    if nifty_chg <= SNIPER_CFG["nifty_massacre"]:
        macro_state = "MASSACRE"
    elif vix_val >= SNIPER_CFG["vix_panic"]:
        macro_state = "PANIC"
    elif vix_val >= SNIPER_CFG["vix_chop"]:
        macro_state = "CHOP"
    else:
        nifty_above_ma50 = True
        if not nifty_df.empty and len(nifty_df) >= 50:
            try:
                nc = nifty_df["Close"].squeeze()
                nifty_above_ma50 = float(nc.iloc[-1]) > float(nc.rolling(50).mean().iloc[-1])
            except: pass
        macro_state = "CLEAR" if nifty_above_ma50 else "CHOP"

    log.info(f"Macro: {macro_state} | VIX={vix_val:.1f} | NIFTY {nifty_chg:+.2f}%")
    return {"macro_state":macro_state,"breadth_ok":breadth_ok,
            "vix_val":round(vix_val,2),"nifty_chg":round(nifty_chg,2),
            "cnx500_below_ma50":cnx500_below_ma50}


def _get_macro_regime() -> dict:
    global _MACRO_REGIME_CACHE
    if _MACRO_REGIME_CACHE is not None:
        return _MACRO_REGIME_CACHE
    with _MACRO_REGIME_LOCK:
        if _MACRO_REGIME_CACHE is None:
            _MACRO_REGIME_CACHE = fetch_macro_regime()
    return _MACRO_REGIME_CACHE


# ── SN-1: Composite + Directive ─────────────────────────────────────

def calc_sniper_composite(fort: dict, fii_pts: int, macro_state: str,
                           sn_layers: dict = None) -> int:
    macro_map   = {"CLEAR":100,"CHOP":50,"PANIC":20,"MASSACRE":0}
    macro_score = macro_map.get(macro_state, 50)

    if sn_layers:
        l1 = sn_layers.get("sn_layer1", False)
        l2 = sn_layers.get("sn_layer2", False)
        l3 = sn_layers.get("sn_layer3", False)
        vcp_coil = fort.get("vcp_coil","LOOSE") == "TIGHT 🟢"
    else:
        l1 = fort.get("layer1", False)
        l2 = fort.get("layer2", False)
        l3 = fort.get("layer3", False)
        vcp_coil = fort.get("vcp_coil","LOOSE") == "TIGHT 🟢"

    alt_ok   = fort.get("alt_pct", 100) < SNIPER_CFG["alt_warn_pct"]
    sect_ok  = fort.get("sector_mult", 1.0) >= 1.0
    vcp_score= ((25 if l1 else 0)+(20 if l2 else 0)+(25 if l3 else 0)+
                (15 if vcp_coil else 0)+(10 if sect_ok else 0)+(5 if alt_ok else 0))
    rsi_v    = fort.get("rsi", 50.0)
    flow_score=(100 if rsi_v<40 else 70 if rsi_v<50 else 40 if rsi_v<60 else 20)
    composite = round(macro_score*0.30 + vcp_score*0.50 + flow_score*0.20)
    return min(100, max(0, composite))


def calc_sniper_directive(symbol, fort, result, macro_state, breadth_ok,
                          composite, has_position) -> dict:
    scfg      = SNIPER_CFG
    all_layers= fort.get("layer1") and fort.get("layer2") and fort.get("layer3")
    t1=result.get("t1",0.0); t3=result.get("t3",0.0); close=result.get("close",0.0)
    trail_stop=result.get("trailing_stop")
    active_stop=trail_stop if (trail_stop and trail_stop>t3) else t3
    entry_price=None

    is_pristine=(composite>=scfg["score_pristine"] and all_layers
                 and macro_state=="CLEAR" and breadth_ok
                 and fort.get("regime")=="MOMENTUM")
    is_good    =(composite>=scfg["score_good"] and all_layers
                 and macro_state not in ("PANIC","MASSACRE") and breadth_ok)
    is_marginal=(composite>=scfg["score_marginal"] and fort.get("layer1")
                 and fort.get("layer2") and macro_state not in ("PANIC","MASSACRE"))
    is_probe   =(composite>=scfg["score_probe"] and fort.get("layer1")
                 and macro_state!="MASSACRE")

    if macro_state == "MASSACRE":
        return {"sniper_directive":"⚠️ HALT — MARKET MASSACRE","sniper_action":"CLOSE_ALL",
                "sniper_deploy":0,"sniper_entry":None,"sn_active_stop":active_stop,
                "is_pristine":False,"is_good":False,"is_marginal":False,"is_probe":False,"all_layers":all_layers}
    elif macro_state == "PANIC":
        return {"sniper_directive":"🔴 HALT — VIX PANIC","sniper_action":"HOLD",
                "sniper_deploy":0,"sniper_entry":None,"sn_active_stop":active_stop,
                "is_pristine":False,"is_good":False,"is_marginal":False,"is_probe":False,"all_layers":all_layers}

    if has_position:
        r1_hit=close>=result.get("sn_r1",float("inf"))
        r2_hit=close>=result.get("sn_r2",float("inf"))
        if r2_hit:
            return {"sniper_directive":"📈 HOLD — R2 HIT — trail 2.5×ATR","sniper_action":"TRAIL",
                    "sniper_deploy":0,"sniper_entry":active_stop,"sn_active_stop":active_stop,
                    "is_pristine":is_pristine,"is_good":is_good,"is_marginal":is_marginal,"is_probe":is_probe,"all_layers":all_layers}
        if r1_hit:
            return {"sniper_directive":"🎯 PARTIAL SELL — R1 HIT (30% sell)","sniper_action":"PARTIAL_SELL",
                    "sniper_deploy":0,"sniper_entry":active_stop,"sn_active_stop":active_stop,
                    "is_pristine":is_pristine,"is_good":is_good,"is_marginal":is_marginal,"is_probe":is_probe,"all_layers":all_layers}

    if is_pristine:
        entry_price=t1; deploy=100
        directive=f"⚔️ SNIPER SHOT PRISTINE — BUY @ ₹{t1:.2f}"
    elif is_good:
        entry_price=t1; deploy=75
        directive=f"🟢 GOOD SHOT — BUY @ ₹{t1:.2f}"
    elif is_marginal:
        entry_price=t1*(1-0.005); deploy=50
        directive=f"🟡 MARGINAL — BUY LIMIT ₹{entry_price:.2f}"
    elif is_probe:
        entry_price=t1*(1-0.01); deploy=25
        directive=f"🔵 PROBE — SMALL BUY ₹{entry_price:.2f}"
    else:
        close_vs_t1=((close-t1)/t1*100) if t1>0 else 0
        directive=f"👁️ WATCHING — score {composite}/100 · {close_vs_t1:+.1f}% vs T1"
        deploy=0; entry_price=None

    return {"sniper_directive":directive,"sniper_action":"BUY" if deploy>0 else "WATCH",
            "sniper_deploy":deploy,"sniper_entry":entry_price,"sn_active_stop":active_stop,
            "is_pristine":is_pristine,"is_good":is_good,"is_marginal":is_marginal,
            "is_probe":is_probe,"all_layers":all_layers}


# ── SN-2: 6-Layer VPOC Validation ──────────────────────────────────

def calc_sniper_vpoc_layers(hist, close, atr14, adv20, turnover_cr, vpoc, ma200) -> dict:
    scfg  = SNIPER_CFG
    band  = scfg["vpoc_band_pct"]
    layer1= abs(close-vpoc)/vpoc <= band if vpoc>0 else False
    if len(hist)>=252 and adv20>0:
        spike_days = (hist["volume"].tail(252) > 2*adv20).sum()
        layer2     = spike_days >= scfg["vol_spikes_52w"]
    else:
        layer2 = False
    recency_bars=min(scfg["bounce_recency"],len(hist))
    touches  = sum(1 for _,r in hist.tail(recency_bars).iterrows()
                   if abs(float(r["close"])-vpoc)/vpoc<=0.03) if vpoc>0 else 0
    layer3   = touches >= scfg["min_bounces"]
    vol_ok   = adv20>0 and float(hist["volume"].iloc[-1]) >= scfg["liquidity_mult"]*adv20
    turn_ok  = turnover_cr >= scfg["min_turnover_cr"]
    layer4   = vol_ok and turn_ok
    if len(hist)>=52:
        h52=float(hist["high"].tail(252).max()); l52=float(hist["low"].tail(252).min())
        fib618=l52+0.618*(h52-l52) if h52>l52 else close
        layer5=close>vpoc or abs(close-fib618)/fib618<=0.03
    else:
        layer5=close>=vpoc
    layer6=(close-ma200)/ma200*100 < scfg["alt_stop_pct"] if ma200>0 else True
    sn_layer_score=((25 if layer1 else 0)+(20 if layer2 else 0)+(25 if layer3 else 0)+
                    (15 if layer4 else 0)+(10 if layer5 else 0)+(5 if layer6 else 0))
    return {"sn_layer1":layer1,"sn_layer2":layer2,"sn_layer3":layer3,
            "sn_layer4":layer4,"sn_layer5":layer5,"sn_layer6":layer6,
            "sn_all_layers":all([layer1,layer2,layer3,layer4,layer5,layer6]),
            "sn_layer_score":sn_layer_score,
            "sn_alt_pct":(close-ma200)/ma200*100 if ma200>0 else 0}


# ── SN-3: 9-Node Bayesian ──────────────────────────────────────────

def calc_sniper_bayesian(layer1, layer2, layer3, vcp_coil, mfi_v, cvd_signal,
                         vsa_absorption, breadth_ok, sector_mult, macro_state,
                         adx_v, velocity_pct, alt_pct) -> dict:
    scfg  = SNIPER_CFG
    prior = 0.35
    macro_boost={"CLEAR":0.65,"CHOP":0.45,"PANIC":0.20,"MASSACRE":0.05}
    nodes=[
        (macro_state=="CLEAR",         macro_boost.get(macro_state,0.45), 0.25),
        (layer1,                        0.72, 0.30),
        (layer2,                        0.68, 0.38),
        (layer3,                        0.70, 0.35),
        (vcp_coil,                      0.75, 0.42),
        (mfi_v<=45.0,                   0.68, 0.42),
        (cvd_signal=="ACCUMULATION",    0.74, 0.44),
        (vsa_absorption,                0.72, 0.45),
        (breadth_ok,                    0.62, 0.40),
        (sector_mult>=1.10,             0.65, 0.45),
        (adx_v>=25.0,                   0.68, 0.38),
        (velocity_pct>5.0,              0.65, 0.42),
        (alt_pct<30.0,                  0.60, 0.40),
    ]
    posterior = prior
    for condition,p_true,p_false in nodes:
        lk        = p_true if condition else p_false
        posterior = (lk*posterior)/max(1e-9, lk*posterior+(1-lk)*(1-posterior))
    try:
        alpha     = scfg["bayes_alpha"]
        posterior = alpha*prior+(1-alpha)*posterior
    except Exception: pass
    posterior = min(0.99, max(0.01, round(posterior,3)))
    bayes_pct = round(posterior*100)
    if posterior>=0.75:   bonus=12; label=f"🧠 Sniper Bayes {bayes_pct}% — VERY HIGH"
    elif posterior>=0.65: bonus=8;  label=f"🧠 Sniper Bayes {bayes_pct}% — HIGH"
    elif posterior>=0.55: bonus=4;  label=f"🧠 Sniper Bayes {bayes_pct}% — moderate"
    elif posterior>=0.45: bonus=0;  label=f"🧠 Sniper Bayes {bayes_pct}% — neutral"
    else:                 bonus=-5; label=f"🧠 Sniper Bayes {bayes_pct}% — LOW"
    return {"sn_bayes_prob":posterior,"sn_bayes_pct":bayes_pct,
            "sn_bayes_bonus":bonus,"sn_bayes_label":label}


# ── SN-5: Position Sizing ───────────────────────────────────────────

def calc_sniper_position(close, atr14, composite, deploy_pct, account=None) -> dict:
    if account is None: account=ACCOUNT_EQUITY
    if atr14<=0 or close<=0:
        return {"sn_shares":0,"sn_amount":0,"sn_pos_label":"—","sn_risk_pct_actual":0.0}
    scfg        = SNIPER_CFG
    risk_rupees = account*scfg["risk_per_trade"]
    risk_per_sh = atr14*scfg["atr_stop_mult"]
    shares_vol  = math.floor(risk_rupees/risk_per_sh) if risk_per_sh>0 else 0
    score_factor= (composite/100.0)**0.5
    shares_score= math.floor(shares_vol*score_factor)
    blend       = scfg["score_size_blend"]
    shares_blend= math.floor(shares_vol*(1-blend)+shares_score*blend)
    deploy_factor=deploy_pct/100.0
    shares_final= math.floor(shares_blend*deploy_factor)
    max_shares  = math.floor((account*scfg["max_pos_pct"])/close)
    shares_final= min(shares_final,max_shares)
    pos_amount  = shares_final*close
    risk_actual = (shares_final*risk_per_sh/account*100 if account>0 else 0.0)
    pos_label   = (f"{shares_final} sh | ₹{pos_amount:,.0f} | {deploy_pct}% deploy | "
                   f"Risk ₹{shares_final*risk_per_sh:,.0f}" if shares_final>0 else "— (below min)")
    return {"sn_shares":shares_final,"sn_amount":round(pos_amount,0),
            "sn_pos_label":pos_label,"sn_risk_pct_actual":round(risk_actual,2),
            "sn_risk_per_share":round(risk_per_sh,2)}


# ── SN-6: Dynamic Exit Plan ─────────────────────────────────────────

def calc_sniper_exit_plan(close, t1, t3, atr14, trail_stop_existing=None) -> dict:
    scfg=SNIPER_CFG
    if t1<=0 or atr14<=0:
        return {"sn_be_trigger":None,"sn_be_active":False,"sn_trail_active":False,
                "sn_trail_stop":t3,"sn_active_stop":t3,
                "sn_r1":t1*1.30,"sn_r2":t1*1.60,"sn_r3":t1*2.00,
                "sn_r1_action":f"Sell {scfg['r1_sell_pct']}% | Move stop to T1",
                "sn_r2_action":f"Sell {scfg['r2_sell_pct']}% | Trail 2.5×ATR",
                "sn_r3_action":f"Sell {scfg['r3_sell_pct']}% | Trail rest aggressively",
                "sn_gain_pct":0.0}
    gain_pct     = ((close-t1)/t1*100) if t1>0 else 0.0
    be_trigger   = t1+atr14*scfg["be_atr_mult"]
    be_active    = close>=be_trigger
    trail_trigger= t1*(1+scfg["trail_trigger_pct"]/100)
    trail_active = close>=trail_trigger
    if trail_active:
        new_trail  = close-atr14*scfg["trail_atr_mult"]
        trail_stop = (trail_stop_existing
                      if trail_stop_existing and trail_stop_existing>new_trail
                      else max(new_trail,t3))
    else:
        trail_stop = t3
    active_stop=trail_stop if (trail_active and trail_stop>t3) else t3
    if be_active: active_stop=max(active_stop,t1)
    r1=t1*(1+scfg["r1_pct"]/100); r2=t1*(1+scfg["r2_pct"]/100); r3=t1*(1+scfg["r3_pct"]/100)
    return {"sn_be_trigger":round(be_trigger,2),"sn_be_active":be_active,
            "sn_trail_active":trail_active,"sn_trail_stop":round(trail_stop,2),
            "sn_active_stop":round(active_stop,2),
            "sn_r1":round(r1,2),"sn_r2":round(r2,2),"sn_r3":round(r3,2),
            "sn_r1_action":f"Sell {scfg['r1_sell_pct']}% | Move stop to T1 ₹{t1:.2f}",
            "sn_r2_action":f"Sell {scfg['r2_sell_pct']}% | Trail 2.5×ATR",
            "sn_r3_action":f"Sell {scfg['r3_sell_pct']}% | Trail rest aggressively",
            "sn_gain_pct":round(gain_pct,1)}


# ── Assemble v8.2 enriched result ──────────────────────────────────

def assemble_result_v8(symbol, today_row, hist, fii_data, insider_map,
                       filings, earnings_cal) -> Optional[dict]:
    """Full v8.2 assemble — all audit + gap fixes applied."""
    macro       = _get_macro_regime()
    macro_state = macro["macro_state"]
    breadth_ok  = macro["breadth_ok"]

    # FIX-AUDIT-04: earnings veto now covers day-0 (results TODAY).
    # Original check was `0 < _earn_days <= 2` which missed _earn_days==0
    # (stock in live results-day trading — maximum binary event risk).
    _earn_days = earnings_cal.get(symbol.upper())
    if _earn_days is not None and 0 <= _earn_days <= 2:
        # FIX-GAP-02: Elevated from log.debug → log.warning so production users
        # can see exactly which symbols were hard-vetoed due to imminent earnings.
        # log.debug is invisible at INFO level; this is a safety-critical skip.
        log.warning(
            f"{symbol}: EARNINGS VETO — results in {_earn_days} trading day(s) "
            f"(including today). Hard skip — not scored."
        )
        return None

    result = assemble_result(symbol, today_row, hist, fii_data, insider_map,
                              filings, earnings_cal,
                              vix_now_cached=macro.get("vix_val"))
    if result is None:
        return None

    close   = float(today_row["close"])
    fort    = {k: result[k] for k in (
        "layer1","layer2","layer3","vcp_coil","entry_zone","entry_band",
        "stop_note","atr_mult","alt_pct","sector_mult","regime","mfi",
        "mfi_status","rsi","adx","adx_prev","t1","t2","t3","r1","r2","r3",
        "risk_pct","rr","atr14_val","adv20_val","vpoc_val","ma50_val",
        "ma200_val","ma_label","w52_bonus","w52_label","atrv_bonus",
        "atrv_label","vdu_bonus","vdu_label","vdu_bars","forward_bonus",
        "momentum_velocity_pct",
    ) if k in result}
    fort["fortress_pts"] = result.get("score_fortress", 0)

    if macro_state == "MASSACRE":
        result["sniper_directive"] = "⚠️ HALT — MARKET MASSACRE"
        result["sniper_action"]    = "CLOSE_ALL"
        result["macro_state"]      = macro_state
        return result

    atr14       = fort.get("atr14_val",1.0)
    adv20       = fort.get("adv20_val",1.0)
    vpoc        = fort.get("vpoc_val",close)
    ma200       = fort.get("ma200_val",0.0)
    turnover_cr = result.get("turnover_cr",0.0)

    # SN-2
    layers6 = calc_sniper_vpoc_layers(hist, close, atr14, adv20, turnover_cr, vpoc, ma200)
    result.update(layers6)

    # SN-3
    sn_bayes = calc_sniper_bayesian(
        layer1=layers6["sn_layer1"], layer2=layers6["sn_layer2"],
        layer3=layers6["sn_layer3"], vcp_coil=fort.get("vcp_coil","LOOSE")=="TIGHT 🟢",
        mfi_v=fort.get("mfi",50.0), cvd_signal=result.get("cvd_signal","NEUTRAL"),
        vsa_absorption=result.get("vsa_absorption",False), breadth_ok=breadth_ok,
        sector_mult=fort.get("sector_mult",1.0), macro_state=macro_state,
        adx_v=fort.get("adx",20.0), velocity_pct=fort.get("momentum_velocity_pct",0.0),
        alt_pct=fort.get("alt_pct",0.0),
    )
    result.update(sn_bayes)
    result["total_score"] = min(MAX_SCORE, result["total_score"] + sn_bayes["sn_bayes_bonus"])

    # Recompute FOG with final live VIX
    fog_enh_final = calc_fog_enhanced(
        fort.get("adx",20.0), fort.get("adx_prev",fort.get("adx",20.0)),
        result.get("vix_val", macro.get("vix_val",18.0)),
        fort.get("ma50_val",0.0), fort.get("ma200_val",0.0), fort.get("w52_bonus",0),
    )
    result["fog_block"] = fog_enh_final["fog_block"]
    result["fog_label"] = fog_enh_final["fog_label"]
    result["fog_tier"]  = fog_enh_final["fog_tier"]

    # SN-1 composite
    composite = calc_sniper_composite(fort, result.get("score_fii",15), macro_state,
                                      sn_layers=layers6)
    result["sniper_composite"] = composite

    # SN-6 exit plan
    t1=result.get("t1",close); t3=result.get("t3",close*0.90)
    exit_plan = calc_sniper_exit_plan(close, t1, t3, atr14, result.get("trailing_stop"))
    result.update(exit_plan)
    result["r1"]=exit_plan["sn_r1"]; result["r2"]=exit_plan["sn_r2"]; result["r3"]=exit_plan["sn_r3"]

    # SN-1 directive — FIX-AUDIT-03: position lookup now respects status='open'
    has_position = _get_position(symbol) is not None
    directive    = calc_sniper_directive(symbol, fort, result, macro_state,
                                         breadth_ok, composite, has_position)
    result.update(directive)

    # FOG post-directive override
    if result.get("fog_block"):
        fog_tier      = result.get("fog_tier","FOG_WARNING")
        fog_deploy_cap= 10 if fog_tier=="FOG_SEVERE" else 25
        existing_dir  = result.get("sniper_directive","")
        if "FOG" not in existing_dir:
            result["sniper_directive"] = (
                f"🌫️ {fog_tier} — CAUTION ({fog_deploy_cap}% max deploy)\n  ({existing_dir})"
            )
        current_deploy = result.get("sniper_deploy",0) or 0
        result["sniper_deploy"] = min(current_deploy, fog_deploy_cap)

    # SN-5 sizing
    sn_pos = calc_sniper_position(close, atr14, composite,
                                   result.get("sniper_deploy", directive["sniper_deploy"]))
    result.update(sn_pos)

    result["macro_state"] = macro_state
    result["breadth_ok"]  = breadth_ok
    result["vix_val"]     = macro.get("vix_val",18.0)
    result["nifty_chg"]   = macro.get("nifty_chg",0.0)
    return result


# ══════════════════════════════════════════════════════════════════════
# SECTION 22 — OUTPUT HELPERS
# ══════════════════════════════════════════════════════════════════════

def _escape_md(s) -> str:
    """
    FIX-AUDIT-01: Escape ALL 18 special characters required by Telegram
    MarkdownV2.  The old function only escaped  _ * ` [  which caused
    parse errors on any stock containing  -  (BAJAJ-AUTO, M&M) or any
    price/percentage containing  .  or  +  or  (  or  ).

    Telegram MarkdownV2 special chars (from API docs):
      _ * [ ] ( ) ~ ` > # + - = | { } . !

    This function is ONLY applied to DATA VALUES injected into the
    message template (symbols, prices, story text, directive text).
    The structural markdown markers  *bold*  _italic_  in the template
    are NOT passed through this function.
    """
    if s is None:
        return ""
    text = str(s)
    # Escape in a single pass using a translation table to avoid
    # double-escaping (e.g. turning \. into \\.)
    _MD2_SPECIAL = r'\_*[]()~`>#+-=|{}.!'
    result = []
    for ch in text:
        if ch in _MD2_SPECIAL:
            result.append('\\')
        result.append(ch)
    return "".join(result)


def _score_bar(score, max_score, color="#7c3aed", width=80) -> str:
    pct   = min(100, max(0, score/max_score*100)) if max_score>0 else 0
    bar_w = int(width*pct/100)
    return (f'<div style="background:#e5e7eb;border-radius:4px;height:8px;width:{width}px;display:inline-block">'
            f'<div style="background:{color};height:8px;border-radius:4px;width:{bar_w}px"></div></div>'
            f' <span style="font-size:11px;color:#555">{score}/{max_score}</span>')


def _dq_badge(dq: str) -> str:
    badges={"EOD_FRESH":"🟢 FRESH","SHEETS_EOD":"📊 SHEETS","EOD_CACHED":"✅ CACHED",
             "SNAPSHOT_FALLBACK":"⚠️ SNAPSHOT","STALE":"❌ STALE"}
    return badges.get(dq, dq)


def _rank_medal(rank: str) -> str:
    if "ELITE" in rank:    return "⚔️"
    if "PRISTINE" in rank: return "🟢"
    if "HIGH" in rank:     return "🟡"
    if "MODERATE" in rank: return "🟠"
    if "PROBE" in rank:    return "🔵"
    return "▪️"


def validate_telegram_token(token: str) -> tuple:
    if not token or token.strip()=="":
        return False,"EMPTY_TOKEN ❌"
    parts=token.strip().split(":")
    if len(parts)!=2 or not parts[0].isdigit() or len(parts[1])<30:
        return False,f"MALFORMED_TOKEN ❌ — expected 'bot_id:hash', got {token[:20]}..."
    try:
        resp=requests.get(f"https://api.telegram.org/bot{token}/getMe",timeout=10)
        if resp.status_code==200:
            bot_name=resp.json().get("result",{}).get("username","unknown")
            return True,f"Token valid ✅ — bot: @{bot_name}"
        return False,f"HTTP {resp.status_code} from Telegram"
    except Exception as e:
        return False,f"Network error: {e}"


def _split_telegram_message_v2(msg: str, limit: int = 4000) -> list:
    """Split at blank-line card boundaries; hard-split oversized blocks.

    FIX-GAP-08: Character-level hard split (chunk[:limit]) can land the
    cut-point immediately after a backslash, splitting a MarkdownV2 escape
    sequence across two messages (e.g. "\\-" becomes "\\" + "-").  Telegram
    then renders the second chunk with a bare "-" that fails parse validation.

    Safe hard-split: walk backwards from `limit` up to 20 chars to find a
    position that is not immediately after a backslash.  If no safe position
    is found (pathological case), fall back to the character boundary with a
    warning logged.
    """
    def _safe_split(chunk: str, at: int) -> tuple:
        """Return (head, tail) split at `at`, walking back to avoid mid-escape."""
        for offset in range(0, min(20, at)):
            pos = at - offset
            if pos > 0 and chunk[pos - 1] == "\\":
                continue  # would split a \X escape — try one character earlier
            return chunk[:pos], chunk[pos:]
        # Fallback: no safe position found, split at original boundary
        log.warning(f"Telegram split: could not find escape-safe boundary near {at} — hard-splitting")
        return chunk[:at], chunk[at:]

    if len(msg) <= limit:
        return [msg]
    cards  = msg.split("\n\n")
    chunks: list = []; cur = ""
    for card in cards:
        block = card + "\n\n"
        if len(block) > limit:
            if cur.strip(): chunks.append(cur.rstrip()); cur = ""
            lines    = block.split("\n"); card_buf = ""
            for line in lines:
                if len(card_buf) + len(line) + 1 > limit:
                    if card_buf.strip(): chunks.append(card_buf.rstrip())
                    card_buf = line + "\n"
                else:
                    card_buf += line + "\n"
            if card_buf.strip(): cur = card_buf
        elif len(cur) + len(block) > limit:
            if cur.strip(): chunks.append(cur.rstrip())
            cur = block
        else:
            cur += block
    if cur.strip(): chunks.append(cur.rstrip())
    result = []
    for chunk in chunks:
        while len(chunk) > limit:
            head, chunk = _safe_split(chunk, limit)
            if head.strip(): result.append(head)
        if chunk.strip(): result.append(chunk)
    return result


def _telegram_post(token: str, chat_id: str, text: str,
                   parse_mode: str = "Markdown") -> bool:
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id":chat_id,"text":text,"parse_mode":parse_mode}
    delays  = [0,2,4]
    for attempt, delay in enumerate(delays,1):
        if delay: time.sleep(delay)
        try:
            resp=requests.post(url,data=payload,timeout=15,verify=True)
            if resp.status_code==200: return True
            if resp.status_code==429:
                retry_after=int(resp.headers.get("Retry-After",5))
                log.warning(f"Telegram 429 — sleeping {retry_after}s (attempt {attempt}/3)")
                time.sleep(retry_after); continue
            if resp.status_code==400:
                if parse_mode!="":
                    log.warning(f"Telegram 400 on {chat_id} — retrying plain text")
                    return _telegram_post(token,chat_id,text,parse_mode="")
                log.error(f"Telegram 400 bad request: {resp.text[:300]}")
                return False
            log.error(f"Telegram HTTP {resp.status_code} (attempt {attempt}/3): {resp.text[:200]}")
        except requests.exceptions.Timeout:
            log.warning(f"Telegram timeout (attempt {attempt}/3) for {chat_id}")
        except Exception as e:
            log.error(f"Telegram send exception (attempt {attempt}/3): {e}")
    log.error(f"Telegram: all 3 attempts failed for chat_id={chat_id}")
    return False


# ══════════════════════════════════════════════════════════════════════
# SECTION 22b — SN-7: SNIPER TELEGRAM FORMAT v8.2
# ══════════════════════════════════════════════════════════════════════

def _fmt_price(val) -> str:
    try:   return f"₹{float(val):.2f}" if val is not None else "—"
    except: return "—"

def _fmt_pct(val, plus=False) -> str:
    try:
        f = float(val)
        return (f"{f:+.1f}%" if plus else f"{f:.1f}%") if val is not None else "—"
    except: return "—"

def _layer_bar(r: dict) -> str:
    return "".join("✓" if r.get(f"sn_layer{n}") else "✗" for n in range(1,7))

def _rank_clean(rank: str) -> str:
    for ch in ["⚔️","🟢","🟡","🟠","🔵","▪️"]:
        rank=rank.replace(ch,"").strip()
    return rank


def send_telegram_v7_clean(top5, sector_trends, fii_data, date_label, macro,
                           using_fallback=False, data_source="NSE"):
    """Clean Telegram format matching user specification."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("Telegram not configured — skipping"); return

    ms        = macro.get("macro_state","CHOP")
    vix       = macro.get("vix_val",0.0)
    nifty_chg = macro.get("nifty_chg",0.0)
    breadth   = macro.get("breadth_ok",True)

    ms_icon   = {"CLEAR":"✅","CHOP":"⚠️","PANIC":"🔴","MASSACRE":"🚨"}.get(ms,"↔")

    lines=[
        f"⚔️ FORTRESS SNIPER v8.2 | {date_label} | {data_source}",
        f"{ms_icon} {ms} | VIX {vix:.1f} | NIFTY {nifty_chg:+.2f}%",
        f"{'─' * 30}",
    ]

    if ms == "MASSACRE":
        lines += ["","🚨 MARKET MASSACRE — ALL ENTRIES HALTED"]
    elif ms == "PANIC":
        lines += ["","🔴 VIX PANIC — NO NEW ENTRIES"]
    elif not top5:
        lines += ["","📭 No halal setups passed all filters today"]
    else:
        for i, r in enumerate(top5,1):
            sym        = r["symbol"]
            close_px   = r.get("close",0.0)
            rank_raw   = r.get("rank","—")
            entry      = r.get("sniper_entry") or r.get("t1")
            stop       = r.get("sn_active_stop") or r.get("t3")
            r1         = r.get("sn_r1") or r.get("r1")
            r2         = r.get("sn_r2") or r.get("r2")
            days_est   = 12  # Default swing horizon
            story      = r.get("story","") or ""

            lines.append(f"")
            lines.append(f"{rank_raw} #{i} — {sym} (₹{close_px:.2f})")
            lines.append(f"Buy @ ₹{entry:.2f}" if entry else f"Buy @ ₹{close_px:.2f}")
            lines.append(f"Sell @ ₹{r1:.2f}" if r1 else "Sell @ —")
            lines.append(f"SL @ ₹{stop:.2f}" if stop else "SL @ —")
            lines.append(f"")
            lines.append(f"Will achieve in ~{days_est} days")
            lines.append(f"")
            lines.append(f"Why to buy: {story[:100]}{'...' if len(story)>100 else ''}")
            lines.append(f"{'─' * 30}")

    msg = "\n".join(lines)

    # Send plain text (no MarkdownV2 escaping issues)
    all_ids = [TELEGRAM_CHAT_ID] + (TELEGRAM_SHARE_IDS or [])
    for chat_id in all_ids:
        if not chat_id:
            continue
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            resp = requests.post(url, json={"chat_id": chat_id, "text": msg}, timeout=20)
            if resp.status_code == 200:
                log.info(f"Telegram → {chat_id} ✓")
            else:
                log.error(f"Telegram → {chat_id} FAILED: {resp.status_code}")
        except Exception as e:
            log.error(f"Telegram error: {e}")

# ══════════════════════════════════════════════════════════════════════
# SECTION 23 — SECTOR TRENDS
# ══════════════════════════════════════════════════════════════════════

def get_sector_trends() -> dict:
    trends={}
    for name,idx in list(SECTOR_INDICES.items())[:6]:
        try:
            h=fetch_history(f"^{idx}",days=30)
            if len(h)<5: trends[name]={"trend":"NEUTRAL","label":"—"}; continue
            c=h["close"].values; ma20=float(pd.Series(c).rolling(20).mean().iloc[-1]) if len(c)>=20 else c[-1]
            last=c[-1]; up3=sum(1 for i in range(1,min(3,len(c))) if c[-i]>c[-i-1])
            trend=("🔥 STRONG" if last>ma20 and up3>=2 else "✓ BULLISH" if last>ma20
                   else "↔ WEAK" if last>float(pd.Series(c).rolling(5).mean().iloc[-1]) else "⚠ BEARISH")
            trends[name]={"trend":trend,"label":trend}; time.sleep(0.2)
        except Exception as e:
            log.warning(f"Sector {name}: {e}"); trends[name]={"trend":"NEUTRAL","label":"—"}
    return trends


# ══════════════════════════════════════════════════════════════════════
# SECTION 24 — GOOGLE SHEETS OUTPUT
# ══════════════════════════════════════════════════════════════════════

def push_to_gsheets(top5: list, date_label: str):
    """
    FIX-AUDIT-10: gspread version detection now uses tuple comparison of
    the version string split, removing the dependency on the `packaging`
    library.  Falls back safely to the new-API call style if detection
    fails for any reason.
    """
    if not _sheets_configured():
        log.info("Google Sheets not configured — skipping output push"); return
    if not _init_sheets_client():
        log.warning("push_to_gsheets: sheets client not ready — skipping"); return
    try:
        ws = _get_worksheet(SHEET_SCREENER)
        if ws is None:
            log.info(f"  Creating output tab '{SHEET_SCREENER}' ...")
            ws = _sheets_retry(_GS_WORKBOOK.add_worksheet,
                               title=SHEET_SCREENER, rows=200, cols=35,
                               label=f"add_worksheet({SHEET_SCREENER})")
            _GS_WS_CACHE[SHEET_SCREENER] = ws

        _sheets_retry(ws.clear, label=f"clear({SHEET_SCREENER})")

        headers=[
            "Date","Symbol","Sector","Total Score","Max","Rank","Alloc",
            "Fortress/80","FII/30","Insider/30","Filing/30","Earnings/30",
            "Sniper Composite","Bayes%","Directive","T1 Floor","T3 Stop",
            "R1","R2","R3","RR","Entry Zone","VCP","Regime","Sector Mult",
            "MC Survival%","Data Quality","Volume Reliable","Story",
        ]
        rows_to_write=[headers]
        for r in top5:
            rows_to_write.append([
                date_label, r["symbol"], r.get("sector","—"),
                r["total_score"], r.get("max_score",MAX_SCORE),
                r.get("rank","—"), r.get("alloc","—"),
                r.get("score_fortress",0), r.get("score_fii",0),
                r.get("score_insider",0), r.get("score_filing",0),
                r.get("score_earnings",0), r.get("sniper_composite","—"),
                r.get("sn_bayes_pct","—"), r.get("sniper_directive","—"),
                r.get("t1","—"), r.get("t3","—"),
                r.get("sn_r1","—"), r.get("sn_r2","—"), r.get("sn_r3","—"),
                r.get("rr","—"), r.get("entry_zone","—"),
                r.get("vcp_coil","—"), r.get("regime","—"),
                r.get("sector_mult","—"), r.get("mc_survival_pct","—"),
                r.get("data_quality","—"),
                str(r.get("volume_reliable",True)),
                r.get("story",""),
            ])

        # FIX-AUDIT-10: version detection via tuple comparison — no packaging dep.
        # gspread >= 6.0: ws.update(range, values)
        # gspread <  6.0: ws.update(values) — range is positional
        try:
            import gspread as _gs
            ver_parts = _gs.__version__.split(".")
            gspread_major = int(ver_parts[0]) if ver_parts else 0
            gspread_minor = int(ver_parts[1]) if len(ver_parts) > 1 else 0
            _gspread_new_api = (gspread_major, gspread_minor) >= (6, 0)
        except Exception:
            _gspread_new_api = True   # safe default: try new API first

        try:
            if _gspread_new_api:
                _sheets_retry(ws.update, "A1", rows_to_write,
                              label=f"batch_update({SHEET_SCREENER})")
            else:
                _sheets_retry(ws.update, rows_to_write,
                              label=f"batch_update_legacy({SHEET_SCREENER})")
        except TypeError:
            # If new-API call fails with TypeError, fall back to legacy signature
            log.warning("gspread update TypeError — retrying with legacy signature")
            _sheets_retry(ws.update, rows_to_write,
                          label=f"batch_update_fallback({SHEET_SCREENER})")

        log.info(f"  Google Sheets Tab '{SHEET_SCREENER}' updated: {len(top5)} picks ✅")
    except Exception as e:
        log.error(f"push_to_gsheets failed: {e}")


def save_excel(top5: list, all_results: list, date_label: str, fii_data: dict):
    if not top5 and not all_results: return
    try:
        EXCEL_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with pd.ExcelWriter(EXCEL_OUTPUT_PATH, engine="openpyxl") as writer:
            pd.DataFrame(top5).to_excel(writer, sheet_name="Top Picks",   index=False)
            pd.DataFrame(all_results).to_excel(writer, sheet_name="All Results", index=False)
            pd.DataFrame([fii_data]).to_excel(writer, sheet_name="FII_DII", index=False)
        log.info(f"Excel saved: {EXCEL_OUTPUT_PATH}")
    except Exception as e:
        log.error(f"Excel save failed: {e}")


def save_html_report(top5: list, date_label: str, fii_data: dict, sector_trends: dict):
    try:
        HTML_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        rows=""
        for i,r in enumerate(top5,1):
            rank=r.get("rank","—"); score=r.get("total_score",0)
            max_s=r.get("max_score",MAX_SCORE); sym=r["symbol"]
            directive=r.get("sniper_directive","—"); composite=r.get("sniper_composite","—")
            entry=r.get("sniper_entry") or r.get("t1","—"); stop=r.get("sn_active_stop") or r.get("t3","—")
            vol_warn=""
            if not r.get("volume_reliable",True):
                vol_warn='<span style="color:#dc2626;font-size:10px"> ⚠️ No Volume Data</span>'
            rows+=f"""<tr>
              <td>{i}</td><td><b>{sym}</b><br><small>{r.get('sector','—')}</small>{vol_warn}</td>
              <td>{score}/{max_s}<br><small>{rank}</small>
                  <br><small style="color:#7c3aed">Sniper {composite}/100</small></td>
              <td><small>{directive}</small></td>
              <td>
                <table style="font-size:11px;border-collapse:collapse">
                  <tr><td style="color:#555;padding:1px 4px">Fortress</td><td>{_score_bar(r.get('score_fortress',0),80,'#7c3aed',60)}</td></tr>
                  <tr><td style="color:#555;padding:1px 4px">FII/DII</td><td>{_score_bar(r.get('score_fii',0),30,'#0891b2',60)}</td></tr>
                  <tr><td style="color:#555;padding:1px 4px">Insider</td><td>{_score_bar(r.get('score_insider',0),30,'#16a34a',60)}</td></tr>
                  <tr><td style="color:#555;padding:1px 4px">Filing</td><td>{_score_bar(r.get('score_filing',0),30,'#ca8a04',60)}</td></tr>
                  <tr><td style="color:#555;padding:1px 4px">Earnings</td><td>{_score_bar(r.get('score_earnings',0),30,'#dc2626',60)}</td></tr>
                </table>
              </td>
              <td>₹{entry}<br><small style="color:#16a34a">Entry</small></td>
              <td>₹{stop}<br><small style="color:#dc2626">Stop</small></td>
              <td>₹{r.get('sn_r1','—')} / ₹{r.get('sn_r2','—')} / ₹{r.get('sn_r3','—')}</td>
              <td><small style="color:#555;font-style:italic">{r.get('story','—')}</small></td>
            </tr>"""

        sector_html="".join(
            f'<span style="margin:4px;padding:4px 10px;border-radius:12px;background:#f3f4f6;font-size:13px">'
            f'{sec}: {v.get("label","—")}</span>'
            for sec,v in sector_trends.items()
        )
        html=f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><title>⚔️ Fortress Sniper v8.2 | {date_label}</title>
<style>body{{font-family:system-ui,sans-serif;margin:0;padding:20px;background:#f9fafb;color:#111}}
h1{{font-size:22px;margin:0 0 4px}}.meta{{color:#666;font-size:14px;margin-bottom:20px}}
.card{{background:#fff;border-radius:12px;border:1px solid #e5e7eb;padding:20px;margin-bottom:16px}}
table{{border-collapse:collapse;width:100%}}
th{{background:#f3f4f6;padding:10px 12px;text-align:left;font-size:13px;color:#555;border-bottom:1px solid #e5e7eb}}
td{{padding:10px;border-bottom:1px solid #f3f4f6;vertical-align:top;font-size:13px}}</style></head><body>
<h1>⚔️ Fortress Sniper v8.2 — Gap-Patched</h1>
<div class="meta">🕌 Halal · 9-node Bayes · 6-layer VPOC · t(df=4) MC · Student-t · {date_label}</div>
<div class="card"><b>🧠 Market Intelligence</b>
  <div style="background:#e0f2fe;border-radius:8px;padding:12px 16px;margin:12px 0">
    <b>{fii_data.get('label','—')}</b> &nbsp; {fii_data.get('detail','—')} &nbsp;
    <span style="background:#dbeafe;color:#1e40af;padding:2px 8px;border-radius:10px;font-size:11px">Score {fii_data.get('score',0)}/30</span>
  </div>
  <div>{sector_html}</div>
</div>
<div class="card"><b>🎯 Top {len(top5)} Halal Sniper Picks</b>
  <table style="margin-top:12px">
    <tr><th>#</th><th>Symbol</th><th>Score</th><th>Directive</th>
        <th>Score Breakdown</th><th>Entry</th><th>Stop</th><th>Targets</th><th>Story</th></tr>
    {rows}
  </table>
</div>
<div class="meta" style="margin-top:20px;text-align:center">
  Fortress Screener v8.2 · Gap-Patched · Halal · NSE EQ · Not financial advice
</div></body></html>"""
        HTML_OUTPUT_PATH.write_text(html, encoding="utf-8")
        log.info(f"HTML report saved: {HTML_OUTPUT_PATH}")
    except Exception as e:
        log.error(f"HTML save failed: {e}")


# ══════════════════════════════════════════════════════════════════════
# SECTION 25 — MAIN SCREENER LOOP (v8.2)
# ══════════════════════════════════════════════════════════════════════

def run_screener_v8():
    """
    v8.2 main entry point — all audit + gap fixes applied.
    FIX-AUDIT-21: bhavcopy retry loop reuses the shared NSE session
    (one warmup) instead of creating a new session per date attempt.
    """
    _init_db()
    date_str, date_label = get_last_trading_day()
    log.info(f"=== FORTRESS SNIPER v8.2 (GAP-PATCHED) | {date_label} ===")
    log.info(f"    FORCE_SHEETS={FORCE_SHEETS} | FORCE_YFINANCE={FORCE_YFINANCE}")
    log.info(f"    PRICE_CAP=₹{PRICE_CAP} | CB_FAIL_SAFE={CB_FAIL_SAFE} | MC_FAT_TAILS={MC_FAT_TAILS}(df={MC_FAT_TAILS_DF})")
    log.info(f"    SHARIAH_CACHE_TTL={SHARIAH_CACHE_TTL_DAYS}d")

    # Clear per-run caches
    global _SECTOR_LIVE_CACHE, _MACRO_REGIME_CACHE, _smallcap_index_cache, _NSE_SESSION_CACHE, _SHARIAH_UNIVERSE_CACHE
    _SECTOR_LIVE_CACHE    = {}
    _MACRO_REGIME_CACHE   = None
    _smallcap_index_cache = {}
    _NSE_SESSION_CACHE    = None
    _SHARIAH_UNIVERSE_CACHE = None

    # Load custom HALAL_LIST (Tab 7)
    global _HALAL_LIST_CUSTOM
    _HALAL_LIST_CUSTOM = _read_sheet_halal_list()
    if _HALAL_LIST_CUSTOM:
        log.info(f"Custom HALAL_LIST loaded: {len(_HALAL_LIST_CUSTOM)} symbols")
    else:
        log.info("Custom HALAL_LIST: not configured or empty")

    # SN-4 macro regime
    macro = _get_macro_regime()
    log.info(f"Macro: {macro['macro_state']} | VIX={macro['vix_val']:.1f}")

    # ── 1. BHAVCOPY DATA SOURCE ────────────────────────────────────────
    bhavcopy       = None
    using_fallback = False
    data_source    = "NSE"

    if FORCE_YFINANCE:
        log.info("FORCE_YFINANCE=true — skipping NSE + Sheets")
    elif FORCE_SHEETS:
        log.info("FORCE_SHEETS=true — skipping NSE")
        bhavcopy    = load_bhavcopy_from_sheets()
        data_source = "SHEETS"
    else:
        # FIX-AUDIT-21: initialise shared session ONCE before the retry loop.
        # Old code called download_bhavcopy() which internally called nse_session()
        # creating 2 warmup HTTP calls per date attempt (up to 6 × 2 = 12 calls).
        bhavcopy_sess = _get_shared_nse_session()
        for days_back in range(0, 6):
            try:
                d = datetime.today() - timedelta(days=days_back)
                while d.weekday() >= 5:
                    d -= timedelta(days=1)
                attempt_str = d.strftime("%d%m%Y")
                log.info(f"Trying NSE bhavcopy for {attempt_str}...")
                raw      = download_bhavcopy(attempt_str, sess=bhavcopy_sess)
                bhavcopy = clean_bhavcopy(raw)
                if not bhavcopy.empty:
                    date_str   = attempt_str
                    date_label = d.strftime("%Y-%m-%d")
                    log.info(f"✅ NSE bhavcopy loaded: {len(bhavcopy)} EQ records")
                    data_source = "NSE"
                    break
            except Exception as e:
                log.warning(f"NSE bhavcopy {attempt_str}: {e}")
                time.sleep(1)

    # ── 2. SHEETS FALLBACK ─────────────────────────────────────────────
    if (bhavcopy is None or bhavcopy.empty) and not FORCE_YFINANCE:
        log.warning("NSE bhavcopy unavailable — trying Google Sheets Bhavcopy tab...")
        bhavcopy    = load_bhavcopy_from_sheets()
        data_source = "SHEETS"
        if not bhavcopy.empty:
            log.info(f"✅ Bhavcopy from Sheets: {len(bhavcopy)} records")

    # ── 3. YFINANCE LAST RESORT ───────────────────────────────────────
    if bhavcopy is None or bhavcopy.empty:
        log.warning("="*60)
        log.warning("⚠️ DEGRADED MODE — NSE + Sheets unavailable")
        log.warning("   Falling back to yfinance ~150 symbol watchlist")
        log.warning("="*60)
        bhavcopy       = build_yfinance_universe()
        using_fallback = True
        data_source    = "YFINANCE"
        if bhavcopy.empty:
            log.error("❌ All data sources failed. Aborting.")
            return []

    # ── Pre-filter ─────────────────────────────────────────────────────
    _volume_available = bhavcopy["volume"].sum() > 0

    # FIX FORT-HIGH-02: Volume=0 aborts run (no illiquid fallback)
    if not _volume_available:
        log.error("CRITICAL: Volume=0 across all rows — NSE data quality failure. Aborting run.")
        return []

    candidates = bhavcopy[
        (bhavcopy["turnover_lakhs"] >= CFG["turnover_lakhs"]) &
        (bhavcopy["close"] >= 50) &
        (bhavcopy["close"] <= PRICE_CAP)
    ].copy()

    log.info(f"After liquidity + price (≤₹{PRICE_CAP}) filter: {len(candidates)}")
    candidates = candidates[candidates["symbol"].apply(is_halal)].copy()
    log.info(f"After halal filter: {len(candidates)}")
    if len(candidates) > CFG["max_candidates"]:
        candidates = candidates.nlargest(CFG["max_candidates"], "turnover_lakhs")
        log.info(f"Terminal Governor: capped to {CFG['max_candidates']}")

    # ── Intelligence data ──────────────────────────────────────────────
    log.info("Fetching FII/DII ...")
    fii_data     = fetch_fii_dii()
    log.info("Fetching insider trades ...")
    insider_map  = fetch_insider_trades(days_back=30)
    log.info("Fetching corporate filings ...")
    filings      = fetch_recent_filings(days_back=14)
    log.info("Fetching earnings calendar ...")
    earnings_cal = fetch_earnings_calendar()

    # ── Main scoring loop ──────────────────────────────────────────────
    # Shared NSE session for all history fetches (already warmed above)
    _shared_nse_sess = _get_shared_nse_session()
    results = []
    for i, (_, row) in enumerate(candidates.iterrows()):
        sym = row["symbol"]
        if i % 25 == 0: log.info(f"Progress: {i}/{len(candidates)}")
        try:
            hist = fetch_history(sym, days=300, sess=_shared_nse_sess)
            if len(hist) < CFG["min_hist_bars"]:
                log.debug(f"{sym}: only {len(hist)} bars — skipped"); continue
            r = assemble_result_v8(sym, row, hist, fii_data, insider_map,
                                    filings, earnings_cal)
            if r: results.append(r)
            time.sleep(0.15)
        except Exception as e:
            log.debug(f"{sym}: {e}")

    results.sort(key=lambda x: (x.get("sniper_composite",0), x.get("total_score",0)), reverse=True)

    # ── v8.2 bucket picker — global sector cap applied first ──────────
    MAX_PER_SECTOR       = 2
    sector_counts_global: dict = {}
    globally_capped: list      = []
    for r in results:
        sec   = r["sector"]
        count = sector_counts_global.get(sec, 0)
        if count < MAX_PER_SECTOR:
            globally_capped.append(r)
            sector_counts_global[sec] = count+1

    mid_picks   = [r for r in globally_capped if 200 <= r["close"] <= PRICE_CAP]
    small_picks = [r for r in globally_capped if  50 <= r["close"] < 200]

    top5 = mid_picks[:MID_CAP_PICKS] + small_picks[:SMALL_CAP_PICKS]

    seen_syms: set = set()
    top5_deduped   = []
    for r in top5:
        if r["symbol"] not in seen_syms:
            top5_deduped.append(r); seen_syms.add(r["symbol"])
    top5 = top5_deduped

    log.info(f"=== TOP {len(top5)} PICKS | {len(results)} total passed ===")
    for r in top5:
        vol_note = "" if r.get("volume_reliable",True) else " [NO-VOL]"
        log.info(f"  {r['symbol']:12s} | {r.get('total_score',0):3d}/{MAX_SCORE} "
                 f"| {r.get('rank','—'):15s} | Sniper {r.get('sniper_composite',0)}/100"
                 f"{vol_note} | {r.get('sniper_directive','—')[:40]}")

    if using_fallback:
        fii_data["_fallback_note"] = "⚠️ NSE+Sheets unavailable — yfinance fallback"

    if PAPER_MODE and top5:
        log.info("\n=== PAPER MODE COMPARISON ===")
        log.info(f"{'Symbol':<12} {'Live':>6} {'Paper':>6} {'Delta':>6} {'Signal'}")
        log.info("-"*45)
        for r in top5:
            live=r.get("total_score",0); paper=r.get("paper_total",0); delta=live-paper
            signal="✅ aligned" if abs(delta)<=20 else "⚠️ moderate" if abs(delta)<=40 else "🔴 review"
            log.info(f"  {r['symbol']:<12} {live:>6} {paper:>6} {delta:>+6} {signal}")

    # ── Outputs ───────────────────────────────────────────────────────
    log.info("Saving Excel report ...")
    save_excel(top5, results, date_label, fii_data)

    log.info("Fetching sector trends ...")
    sector_trends = get_sector_trends()

    log.info("Saving HTML report ...")
    save_html_report(top5, date_label, fii_data, sector_trends)

    log.info("Pushing to Google Sheets (Screener tab) ...")
    push_to_gsheets(top5, date_label)

    log.info("Sending Telegram ...")
    # FIX: Use clean Telegram format
    send_telegram_v7_clean(top5, sector_trends, fii_data, date_label, macro, using_fallback, data_source)

    log.info(f"✅ Done | {len(top5)} setups | Macro: {macro['macro_state']} | "
             f"VIX: {macro['vix_val']:.1f} | Data: {data_source}")
    return top5


# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    run_screener_v8()
