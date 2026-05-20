#!/usr/bin/env python3
"""
SME IPO SNIPER – Halal IPO Screener with Time‑to‑Close Dynamics
Reuses halal_ai_screen() from sniper_unified_v5_4.py.
"""

import os
import sys
import re
import json
import time
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup

# Import your existing halal engine (ensure the file is in the same directory)
from sniper_unified_v5_4 import (
    halal_ai_screen,
    _tg_post,
    _db_conn,
    _init_db,
    get_halal_universe,
    secrets,
    TELEGRAM_TOKEN,
    TELEGRAM_CHAT_ID,
    ACCOUNT_EQUITY,
    ACCOUNT_RISK_PCT,
    VERSION
)

# Configure logging (same format as sniper)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

VERSION_IPO = "IPO-SNIPER-v1.0"

# Constants
IPO_DB_PATH = Path("outputs/ipo_cache.db")   # separate DB for IPO results
MAX_SCORE = 100
# Scoring weights (additive, not multiplicative)
W_GMP = 0.30
W_SUB = 0.45
W_SIZE = 0.10
W_HALAL = 0.15

# Time‑to‑close factor: start of week = lower weight on subscription,
# last day = full weight. This models how subscription numbers evolve.
TIME_FACTOR_START = 0.40   # first day weight for subscription
TIME_FACTOR_END   = 1.00   # last day weight

# Static fallback dataset (create a CSV with at least columns: Symbol, IssueSizeCr, PriceBandLower, PriceBandUpper, LotSize, CloseDate, GMP, SubscriptionTimes)
FALLBACK_CSV = Path("data/ipo_fallback.csv")

# ----------------------------------------------------------------------
# 1. Source‑specific scraping functions
# ----------------------------------------------------------------------

def scrape_ipowatch() -> pd.DataFrame:
    """Scrape ipowatch.in for SME IPOs."""
    url = "https://ipowatch.in/upcoming-sme-ipo/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Try different possible table classes
        table = soup.find("table", class_="wp-block-table")
        if not table:
            table = soup.find("table", class_="tablepress")
        if not table:
            table = soup.find("table")
        if not table:
            log.warning("No table found on ipowatch.in")
            return pd.DataFrame()

        # Extract headers
        headers_row = table.find("tr")
        if not headers_row:
            return pd.DataFrame()
        headers = [th.get_text(strip=True) for th in headers_row.find_all(["th", "td"])]
        # Expected headers: "Company Name", "Price Band", "Lot Size", "Issue Size", "GMP", "Close Date"
        # Map to standard names
        col_map = {}
        for i, h in enumerate(headers):
            h_lower = h.lower()
            if "company" in h_lower or "name" in h_lower:
                col_map["Symbol"] = i
            elif "price" in h_lower:
                col_map["PriceBand"] = i
            elif "lot" in h_lower:
                col_map["LotSize"] = i
            elif "issue" in h_lower and "size" in h_lower:
                col_map["IssueSize"] = i
            elif "gmp" in h_lower:
                col_map["GMP"] = i
            elif "close" in h_lower or "end" in h_lower:
                col_map["CloseDate"] = i

        if "Symbol" not in col_map:
            log.warning("Could not identify company name column on ipowatch.in")
            return pd.DataFrame()

        rows = table.find_all("tr")[1:]  # skip header row
        data = []
        today = datetime.today().date()

        for row in rows:
            cols = row.find_all(["td", "th"])
            if len(cols) < max(col_map.values(), default=5):
                continue

            symbol = cols[col_map["Symbol"]].get_text(strip=True)
            if not symbol or symbol.lower() == "company name":
                continue

            # Price band
            price_band_text = cols[col_map.get("PriceBand", 1)].get_text(strip=True) if "PriceBand" in col_map else ""
            price_lower = price_upper = 0
            if "-" in price_band_text:
                parts = price_band_text.split("-")
                try:
                    price_lower = float(parts[0].strip())
                    price_upper = float(parts[1].strip())
                except:
                    pass
            elif price_band_text.replace(".", "").isdigit():
                price_lower = price_upper = float(price_band_text)

            # Issue size (in crore)
            issue_text = cols[col_map.get("IssueSize", 2)].get_text(strip=True) if "IssueSize" in col_map else ""
            issue_size = 0.0
            match = re.search(r"[\d,.]+", issue_text)
            if match:
                issue_size = float(match.group().replace(",", ""))
                if "cr" not in issue_text.lower() and "crore" not in issue_text.lower():
                    issue_size = issue_size / 100  # assume lakh if no crore indicator? fallback safe

            # GMP (as percentage of upper price band)
            gmp_text = cols[col_map.get("GMP", 3)].get_text(strip=True) if "GMP" in col_map else ""
            gmp = 0.0
            gmp_match = re.search(r"[\d,.]+", gmp_text)
            if gmp_match:
                gmp_raw = float(gmp_match.group().replace(",", ""))
                # If GMP is absolute (₹), convert to % of upper price band
                if "₹" in gmp_text or "Rs" in gmp_text:
                    if price_upper > 0:
                        gmp = gmp_raw / price_upper
                    else:
                        gmp = min(0.50, gmp_raw / 100)  # fallback assume premium %?
                else:
                    gmp = gmp_raw / 100.0   # already percentage
            gmp = min(0.50, gmp)  # cap at 50%

            # Lot size
            lot_text = cols[col_map.get("LotSize", 2)].get_text(strip=True) if "LotSize" in col_map else ""
            lot_size = 0
            lot_match = re.search(r"\d+", lot_text)
            if lot_match:
                lot_size = int(lot_match.group())

            # Subscription (not always present on ipowatch for SME – use 0)
            sub_times = 0.0

            # Close date
            close_text = cols[col_map.get("CloseDate", 5)].get_text(strip=True) if "CloseDate" in col_map else ""
            close_date = today + timedelta(days=5)
            try:
                # Try multiple date formats
                for fmt in ("%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
                    try:
                        close_date = datetime.strptime(close_text, fmt).date()
                        break
                    except:
                        continue
            except:
                pass

            days_to_close = (close_date - today).days

            data.append({
                "Symbol": symbol,
                "IssueSizeCr": issue_size,
                "PriceBandLower": price_lower,
                "PriceBandUpper": price_upper,
                "LotSize": lot_size,
                "GMP": gmp,
                "SubscriptionTimes": sub_times,
                "CloseDate": close_date.strftime("%Y-%m-%d"),
                "DaysToClose": days_to_close,
                "Source": "ipowatch"
            })

        df = pd.DataFrame(data)
        if not df.empty:
            log.info(f"✅ Fetched {len(df)} IPOs from ipowatch.in")
        return df

    except Exception as e:
        log.warning(f"ipowatch.in scrape failed: {e}")
        return pd.DataFrame()


def scrape_chittorgarh() -> pd.DataFrame:
    """Scrape chittorgarh.com for SME IPOs."""
    url = "https://www.chittorgarh.com/ipo/upcoming_sme_ipo.asp"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table", class_="table")
        if not table:
            log.warning("No table found on chittorgarh.com")
            return pd.DataFrame()

        rows = table.find_all("tr")
        if len(rows) < 2:
            return pd.DataFrame()

        # Find header row (usually first <tr> with <th>)
        header_row = None
        for row in rows:
            if row.find("th"):
                header_row = row
                break
        if not header_row:
            header_row = rows[0]
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]

        col_map = {}
        for i, h in enumerate(headers):
            if "name" in h:
                col_map["Symbol"] = i
            elif "price" in h:
                col_map["PriceBand"] = i
            elif "lot" in h:
                col_map["LotSize"] = i
            elif "size" in h:
                col_map["IssueSize"] = i
            elif "gmp" in h:
                col_map["GMP"] = i
            elif "sub" in h or "subscription" in h:
                col_map["Subscription"] = i
            elif "close" in h or "end" in h:
                col_map["CloseDate"] = i

        data = []
        today = datetime.today().date()
        for row in rows[1:]:
            cols = row.find_all("td")
            if len(cols) < 6:
                continue

            symbol = cols[col_map.get("Symbol", 0)].get_text(strip=True)
            if not symbol:
                continue

            # Price band
            price_text = cols[col_map.get("PriceBand", 1)].get_text(strip=True) if "PriceBand" in col_map else ""
            price_lower = price_upper = 0
            if "-" in price_text:
                parts = price_text.split("-")
                try:
                    price_lower = float(parts[0].strip())
                    price_upper = float(parts[1].strip())
                except:
                    pass
            else:
                price_match = re.search(r"[\d,.]+", price_text)
                if price_match:
                    price_lower = price_upper = float(price_match.group().replace(",", ""))

            # Issue size
            issue_text = cols[col_map.get("IssueSize", 2)].get_text(strip=True) if "IssueSize" in col_map else ""
            issue_size = 0.0
            match = re.search(r"[\d,.]+", issue_text)
            if match:
                issue_size = float(match.group().replace(",", ""))
                if "cr" not in issue_text.lower():
                    issue_size = issue_size / 100

            # GMP (as %)
            gmp_text = cols[col_map.get("GMP", 3)].get_text(strip=True) if "GMP" in col_map else ""
            gmp = 0.0
            gmp_match = re.search(r"[\d,.]+", gmp_text)
            if gmp_match:
                gmp = float(gmp_match.group().replace(",", "")) / 100.0
            gmp = min(0.50, gmp)

            # Subscription times
            sub_text = cols[col_map.get("Subscription", 4)].get_text(strip=True) if "Subscription" in col_map else "0"
            sub_times = 0.0
            sub_match = re.search(r"[\d,.]+", sub_text)
            if sub_match:
                sub_times = float(sub_match.group().replace(",", ""))

            # Lot size
            lot_text = cols[col_map.get("LotSize", 2)].get_text(strip=True) if "LotSize" in col_map else ""
            lot_size = 0
            lot_match = re.search(r"\d+", lot_text)
            if lot_match:
                lot_size = int(lot_match.group())

            # Close date
            close_text = cols[col_map.get("CloseDate", 5)].get_text(strip=True) if "CloseDate" in col_map else ""
            close_date = today + timedelta(days=5)
            try:
                for fmt in ("%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
                    try:
                        close_date = datetime.strptime(close_text, fmt).date()
                        break
                    except:
                        continue
            except:
                pass
            days_to_close = (close_date - today).days

            data.append({
                "Symbol": symbol,
                "IssueSizeCr": issue_size,
                "PriceBandLower": price_lower,
                "PriceBandUpper": price_upper,
                "LotSize": lot_size,
                "GMP": gmp,
                "SubscriptionTimes": sub_times,
                "CloseDate": close_date.strftime("%Y-%m-%d"),
                "DaysToClose": days_to_close,
                "Source": "chittorgarh"
            })

        df = pd.DataFrame(data)
        if not df.empty:
            log.info(f"✅ Fetched {len(df)} IPOs from chittorgarh.com")
        return df

    except Exception as e:
        log.warning(f"chittorgarh.com scrape failed: {e}")
        return pd.DataFrame()


def scrape_investorgain() -> pd.DataFrame:
    """Scrape investorgain.com for SME IPOs."""
    url = "https://investorgain.com/ipo/upcoming-ipo"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        # Investorgain often uses <div> with class 'table-responsive' and <table class='table'>
        table = soup.find("table", class_="table")
        if not table:
            log.warning("No table found on investorgain.com")
            return pd.DataFrame()

        rows = table.find_all("tr")
        if len(rows) < 2:
            return pd.DataFrame()

        # Header is first row
        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all("th")]
        col_map = {}
        for i, h in enumerate(headers):
            if "name" in h:
                col_map["Symbol"] = i
            elif "price" in h:
                col_map["PriceBand"] = i
            elif "lot" in h:
                col_map["LotSize"] = i
            elif "size" in h:
                col_map["IssueSize"] = i
            elif "gmp" in h:
                col_map["GMP"] = i
            elif "subscription" in h:
                col_map["Subscription"] = i
            elif "close" in h or "end" in h:
                col_map["CloseDate"] = i

        data = []
        today = datetime.today().date()
        for row in rows[1:]:
            cols = row.find_all("td")
            if len(cols) < 6:
                continue

            symbol = cols[col_map.get("Symbol", 0)].get_text(strip=True)
            if not symbol:
                continue

            price_text = cols[col_map.get("PriceBand", 1)].get_text(strip=True) if "PriceBand" in col_map else ""
            price_lower = price_upper = 0
            if "-" in price_text:
                parts = price_text.split("-")
                try:
                    price_lower = float(parts[0].strip())
                    price_upper = float(parts[1].strip())
                except:
                    pass
            else:
                price_match = re.search(r"[\d,.]+", price_text)
                if price_match:
                    price_lower = price_upper = float(price_match.group().replace(",", ""))

            issue_text = cols[col_map.get("IssueSize", 2)].get_text(strip=True) if "IssueSize" in col_map else ""
            issue_size = 0.0
            match = re.search(r"[\d,.]+", issue_text)
            if match:
                issue_size = float(match.group().replace(",", ""))
                if "cr" not in issue_text.lower():
                    issue_size = issue_size / 100

            gmp_text = cols[col_map.get("GMP", 3)].get_text(strip=True) if "GMP" in col_map else ""
            gmp = 0.0
            gmp_match = re.search(r"[\d,.]+", gmp_text)
            if gmp_match:
                gmp = float(gmp_match.group().replace(",", "")) / 100.0
            gmp = min(0.50, gmp)

            sub_text = cols[col_map.get("Subscription", 4)].get_text(strip=True) if "Subscription" in col_map else "0"
            sub_times = 0.0
            sub_match = re.search(r"[\d,.]+", sub_text)
            if sub_match:
                sub_times = float(sub_match.group().replace(",", ""))

            lot_text = cols[col_map.get("LotSize", 2)].get_text(strip=True) if "LotSize" in col_map else ""
            lot_size = 0
            lot_match = re.search(r"\d+", lot_text)
            if lot_match:
                lot_size = int(lot_match.group())

            close_text = cols[col_map.get("CloseDate", 5)].get_text(strip=True) if "CloseDate" in col_map else ""
            close_date = today + timedelta(days=5)
            try:
                for fmt in ("%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
                    try:
                        close_date = datetime.strptime(close_text, fmt).date()
                        break
                    except:
                        continue
            except:
                pass
            days_to_close = (close_date - today).days

            data.append({
                "Symbol": symbol,
                "IssueSizeCr": issue_size,
                "PriceBandLower": price_lower,
                "PriceBandUpper": price_upper,
                "LotSize": lot_size,
                "GMP": gmp,
                "SubscriptionTimes": sub_times,
                "CloseDate": close_date.strftime("%Y-%m-%d"),
                "DaysToClose": days_to_close,
                "Source": "investorgain"
            })

        df = pd.DataFrame(data)
        if not df.empty:
            log.info(f"✅ Fetched {len(df)} IPOs from investorgain.com")
        return df

    except Exception as e:
        log.warning(f"investorgain.com scrape failed: {e}")
        return pd.DataFrame()


def fetch_ipo_calendar() -> pd.DataFrame:
    """Try multiple sources sequentially. Return first non-empty DataFrame."""
    sources = [
        ("ipowatch", scrape_ipowatch),
        ("chittorgarh", scrape_chittorgarh),
        ("investorgain", scrape_investorgain)
    ]
    for name, func in sources:
        try:
            df = func()
            if not df.empty:
                log.info(f"Using IPO data from {name}")
                return df
        except Exception as e:
            log.warning(f"Source {name} failed: {e}")

    # Fallback to static CSV
    if FALLBACK_CSV.exists():
        df = pd.read_csv(FALLBACK_CSV)
        # Ensure required columns exist
        required_cols = ["Symbol", "IssueSizeCr", "PriceBandLower", "PriceBandUpper", "LotSize", "GMP", "SubscriptionTimes", "CloseDate"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            log.error(f"Fallback CSV missing columns: {missing}")
            return pd.DataFrame()
        today = datetime.today().date()
        if "DaysToClose" not in df.columns:
            df["DaysToClose"] = df["CloseDate"].apply(
                lambda x: (datetime.strptime(x, "%Y-%m-%d").date() - today).days
            )
        log.info(f"⚠️ Using static fallback CSV: {len(df)} IPOs")
        return df

    log.error("No IPO data source available")
    return pd.DataFrame()


# ----------------------------------------------------------------------
# 2. Scoring engine with time‑dependent subscription weight (unchanged)
# ----------------------------------------------------------------------

def compute_ipo_score(row: pd.Series) -> Dict:
    """Return dict with score (0-100), components, and verdict."""
    # Time factor: linear interpolation between start and end based on DaysToClose
    days = max(0, row.get("DaysToClose", 5))
    # Normalise: max days = 7 (typical IPO week). If days > 7, factor = end.
    max_days = 7
    if days >= max_days:
        time_factor = TIME_FACTOR_END
    else:
        # Interpolate: factor = start + (end - start) * (1 - days/max_days)
        time_factor = TIME_FACTOR_START + (TIME_FACTOR_END - TIME_FACTOR_START) * (1 - days / max_days)
    time_factor = max(TIME_FACTOR_START, min(TIME_FACTOR_END, time_factor))

    # 1. GMP score (0-30)
    gmp = max(0.0, min(0.50, row.get("GMP", 0.0)))   # cap GMP at 50%
    score_gmp = gmp * 100 * W_GMP   # e.g., 20% GMP -> 20 * 0.30 = 6 pts

    # 2. Subscription score (0-45) – weighted by time factor
    # Raw subscription: cap at 50x (beyond that gives no extra)
    sub_raw = min(50.0, row.get("SubscriptionTimes", 0.0))
    # Convert to 0-1 scale: 0x = 0, 25x = 0.75, 50x = 1.0
    sub_norm = sub_raw / 50.0
    score_sub = sub_norm * MAX_SCORE * W_SUB * time_factor

    # 3. Issue size score (0-10) – smaller is better
    size = row.get("IssueSizeCr", 100.0)
    if size <= 20:
        score_size = 10
    elif size <= 50:
        score_size = 8
    elif size <= 100:
        score_size = 6
    elif size <= 250:
        score_size = 4
    else:
        score_size = 2
    score_size = score_size * (W_SIZE / 0.10)   # normalise (max 10 → 10% weight)

    # 4. Halal score (0-15) – will be filled later
    # We'll call halal_ai_screen separately and merge
    halal_score = 0.0  # placeholder

    raw_score = score_gmp + score_sub + score_size + halal_score
    final_score = min(MAX_SCORE, max(0, int(round(raw_score))))

    # Verdict (same flags as sniper)
    if final_score >= 75:
        verdict = "WORTH YOUR TIME"
        flag_emoji = "🟢"
    elif final_score >= 55:
        verdict = "MAYBE"
        flag_emoji = "🔵"
    else:
        verdict = "SKIP"
        flag_emoji = "⚪"

    return {
        "score": final_score,
        "score_gmp": round(score_gmp, 1),
        "score_sub": round(score_sub, 1),
        "score_size": round(score_size, 1),
        "time_factor": round(time_factor, 3),
        "verdict": verdict,
        "flag_emoji": flag_emoji,
        "gmp_pct": round(gmp * 100, 1),
        "sub_times": sub_raw,
    }


# ----------------------------------------------------------------------
# 3. Halal enrichment (reuse your existing engine)
# ----------------------------------------------------------------------

def enrich_halal(df: pd.DataFrame) -> pd.DataFrame:
    """Add halal_score and halal_tier for each IPO."""
    scores = []
    tiers = []
    for _, row in df.iterrows():
        sym = row["Symbol"]
        # Build a minimal business description from the symbol (or look up later)
        # For now, use sector = "DIVERSIFIED"
        sector = "DIVERSIFIED"
        business_desc = f"SME IPO company {sym}"
        halal = halal_ai_screen(sym, sector=sector, business_desc=business_desc)
        scores.append(halal.get("score", 0))
        tiers.append(halal.get("tier", "UNKNOWN"))
    df["HalalScore"] = scores
    df["HalalTier"] = tiers
    return df


def adjust_score_with_halal(df: pd.DataFrame) -> pd.DataFrame:
    """Incorporate HalalScore (0-100) into the final IPO score (15% weight)."""
    # Normalise halal score to 0-15 range
    df["HalalContribution"] = (df["HalalScore"] / 100) * MAX_SCORE * W_HALAL
    df["FinalScore"] = (
        df["score_gmp"] + df["score_sub"] + df["score_size"] + df["HalalContribution"]
    )
    df["FinalScore"] = df["FinalScore"].clip(0, MAX_SCORE).astype(int)
    # Update verdict based on final score
    df["Verdict"] = df["FinalScore"].apply(
        lambda x: "WORTH YOUR TIME" if x >= 75 else ("MAYBE" if x >= 55 else "SKIP")
    )
    return df


# ----------------------------------------------------------------------
# 4. Database persistence (separate DB for IPO results)
# ----------------------------------------------------------------------

def init_ipo_db():
    """Create IPO-specific tables."""
    IPO_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db_conn(write=True) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ipo_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                symbol TEXT,
                issue_size_cr REAL,
                price_lower REAL,
                price_upper REAL,
                lot_size INTEGER,
                gmp_pct REAL,
                sub_times REAL,
                close_date TEXT,
                days_to_close INTEGER,
                halal_score INTEGER,
                halal_tier TEXT,
                score_gmp REAL,
                score_sub REAL,
                score_size REAL,
                halal_contribution REAL,
                final_score INTEGER,
                verdict TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_date, symbol)
            )
        """)
    log.info("IPO DB initialised")


def save_ipo_results(df: pd.DataFrame, date_label: str):
    """Store daily IPO analysis."""
    with _db_conn(write=True) as con:
        for _, row in df.iterrows():
            con.execute("""
                INSERT OR REPLACE INTO ipo_analysis
                (run_date, symbol, issue_size_cr, price_lower, price_upper,
                 lot_size, gmp_pct, sub_times, close_date, days_to_close,
                 halal_score, halal_tier, score_gmp, score_sub, score_size,
                 halal_contribution, final_score, verdict)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                date_label, row["Symbol"], row["IssueSizeCr"],
                row["PriceBandLower"], row["PriceBandUpper"],
                row.get("LotSize", 0), row["gmp_pct"], row["SubscriptionTimes"],
                row["CloseDate"], row["DaysToClose"],
                row["HalalScore"], row["HalalTier"],
                row["score_gmp"], row["score_sub"], row["score_size"],
                row["HalalContribution"], row["FinalScore"], row["Verdict"]
            ))


# ----------------------------------------------------------------------
# 5. Telegram report (same style as sniper)
# ----------------------------------------------------------------------

def send_ipo_telegram(df: pd.DataFrame, macro_state: str, vix: float, date_label: str):
    """Send a clean IPO report card."""
    if df.empty:
        _tg_post(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                 f"⚔️ IPO SNIPER | {date_label} | {macro_state} | VIX {vix:.1f}\n\n🤲 No active SME IPOs today.")
        return

    # Sort by final score descending
    df_sorted = df.sort_values("FinalScore", ascending=False)
    lines = [
        f"⚔️ IPO SNIPER {VERSION_IPO} | {date_label} | Macro: {macro_state} | VIX {vix:.1f}",
        "🕌 Halal | Manual execution only",
        "─────────────────────────────────────"
    ]

    for i, (_, row) in enumerate(df_sorted.iterrows(), 1):
        sym = row["Symbol"]
        close_date = row["CloseDate"]
        days_left = row["DaysToClose"]
        price_band = f"₹{row['PriceBandLower']:.0f}–{row['PriceBandUpper']:.0f}"
        gmp = row["gmp_pct"]
        sub = row["sub_times"]
        score = row["FinalScore"]
        verdict = row["Verdict"]
        halal_tier = row["HalalTier"]
        halal_score = row["HalalScore"]

        # Extra note if subscription is still low but closing soon
        note = ""
        if days_left <= 2 and sub < 5 and gmp > 15:
            note = "\n   💡 Subscription may spike on final day – monitor."
        elif days_left <= 1:
            note = "\n   ⏰ Last day – consider applying if fundamentals strong."

        card = (
            f"#{i} {sym} – {verdict} ({score}/100)\n"
            f"   Price: {price_band} | Lot: {row.get('LotSize', '?')} shares\n"
            f"   Issue size: ₹{row['IssueSizeCr']:.0f}Cr | GMP: {gmp:.1f}%\n"
            f"   Subscription: {sub:.1f}x | Closes: {close_date} ({days_left} days left)\n"
            f"   Halal: {halal_tier} ({halal_score}/100)\n"
            f"   Score breakdown: GMP {row['score_gmp']:.0f} + Sub {row['score_sub']:.0f} + Size {row['score_size']:.0f} + Halal {row['HalalContribution']:.0f}{note}"
        )
        lines.append(card)
        lines.append("")

    lines.append("─────────────────────────────────────")
    lines.append("ℹ️ Reply: TAKEN SYMBOL | SKIP SYMBOL")
    lines.append(f"🤲 Bismillah – trade only what you understand")
    full_msg = "\n".join(lines)
    # Split if needed (reuse _split_msg from sniper)
    from sniper_unified_v5_4 import _split_msg
    for chunk in _split_msg(full_msg, 4000):
        _tg_post(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, chunk)


# ----------------------------------------------------------------------
# 6. Main run function
# ----------------------------------------------------------------------

def run_ipo_screener():
    """Main entry point for GitHub Actions."""
    # Initialise DB (reuse same init if you want, but we create IPO tables)
    _init_db()          # ensures main DB exists (for halal cache)
    init_ipo_db()

    # Fetch macro regime from your sniper (reuse function)
    from sniper_unified_v5_4 import fetch_macro_regime, _get_macro
    macro = _get_macro()
    macro_state = macro.get("macro_state", "CHOP")
    vix = macro.get("vix_val", 18.0)

    # Stop if market is in crash mode
    if macro_state in ("MASSACRE", "PANIC"):
        log.warning(f"Macro {macro_state} – skipping IPO analysis")
        send_ipo_telegram(pd.DataFrame(), macro_state, vix, datetime.today().strftime("%Y-%m-%d"))
        return

    # Fetch IPO data
    df = fetch_ipo_calendar()
    if df.empty:
        send_ipo_telegram(pd.DataFrame(), macro_state, vix, datetime.today().strftime("%Y-%m-%d"))
        return

    # Compute base scores (without halal)
    scores = df.apply(compute_ipo_score, axis=1, result_type="expand")
    for col in scores.columns:
        df[col] = scores[col]

    # Enrich with halal (runs LLM if needed – cached)
    df = enrich_halal(df)

    # Recompute final score including halal contribution
    df = adjust_score_with_halal(df)

    # Add close_date as string (already present)
    date_label = datetime.today().strftime("%Y-%m-%d")

    # Save to DB
    save_ipo_results(df, date_label)

    # Filter only IPOs with days left >= 0 (still open)
    df_active = df[df["DaysToClose"] >= 0].copy()

    # Send Telegram report
    send_ipo_telegram(df_active, macro_state, vix, date_label)

    log.info(f"IPO screener done – {len(df_active)} active IPOs evaluated")


if __name__ == "__main__":
    run_ipo_screener()
