#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  IPO SNIPER v3.1 -- INSTITUTIONAL IPO ARBITRAGE ENGINE                  ║
║  ─────────────────────────────────────────────────────────────────────  ║
║                                                                          ║
║  FIXES APPLIED:                                                          ║
║  1. Multi-source data ingestion (Chittorgarh + MoneyControl + NSE API)  ║
║  2. Batched Telegram output (1 summary message + 1 detail for top pick)  ║
║  3. SME/Mainboard auto-classification                                    ║
║  4. Graceful degradation when sources fail                               ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import os
import re
import json
import math
import logging
import sqlite3
import warnings
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

# ── Optional imports with graceful degradation ──────────────────────────────
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    VADER_ENABLED = True
except ImportError:
    VADER_ENABLED = False
    warnings.warn("vaderSentiment not installed. Using lexicon fallback.")

try:
    import aiohttp
    ASYNC_ENABLED = True
except ImportError:
    ASYNC_ENABLED = False

try:
    from scipy.stats import norm
    SCIPY_ENABLED = True
except ImportError:
    SCIPY_ENABLED = False

# ── Paths ────────────────────────────────────────────────────────────────────
DB_PATH         = Path("data/ipo_sniper_v3.db")
CACHE_DIR       = Path("data/cache")
LOG_DIR         = Path("logs")

VERSION         = "IPO-SNIPER-v3.1-INSTITUTIONAL"
SEED            = 42
np.random.seed(SEED)

# ── Telegram Config (set via env vars) ──────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("IPO_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("IPO_TELEGRAM_CHAT_ID", "")

# ── Dynamic scoring weights ─────────────────────────────────────────────────
WEIGHTS = {
    "gmp_momentum":     0.28,
    "subscription_strength": 0.25,
    "fundamental_quality": 0.20,
    "sector_rotation":    0.15,
    "shariah_safety":     0.12,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("IPO-SNIPER-v3")

if VADER_ENABLED:
    _vader = SentimentIntensityAnalyzer()


# ═══════════════════════════════════════════════════════════════════════════
#  MODULE 1: MULTI-SOURCE DATA INGESTION ENGINE
#  ── Cascading fallback: Chittorgarh -> MoneyControl -> NSE API -> Cache
# ═══════════════════════════════════════════════════════════════════════════

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# Source 1: Chittorgarh -- Combined Mainboard + SME list (HTML, BS4-parseable)
CHITTORGARH_URLS = {
    "combined": "https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/",
    "mainboard_dashboard": "https://www.chittorgarh.com/ipo/ipo_dashboard.asp",
    "sme_dashboard": "https://www.chittorgarh.com/ipo/ipo_dashboard_sme.asp",
    "gmp_live": "https://www.chittorgarh.com/report/live-ipo-gmp/331/ipo/",
}

# Source 2: MoneyControl -- Explicit "Open IPOs" section (HTML, reliable)
MONEYCONTROL_URL = "https://www.moneycontrol.com/ipo/"

# Source 3: NSE India API -- Direct exchange data (JSON, requires session)
NSE_API_URLS = {
    "current_ipo": "https://www.nseindia.com/api/ipo-current",
    "ipo_detail": "https://www.nseindia.com/api/ipo-detail",
}


class IPODataSource:
    """Abstract base for IPO data sources with unified interface."""

    def __init__(self, name: str):
        self.name = name
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def fetch(self) -> List[Dict]:
        raise NotImplementedError

    def _safe_get(self, url: str, timeout: int = 20) -> Optional[requests.Response]:
        try:
            resp = self.session.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp
            log.warning(f"[{self.name}] HTTP {resp.status_code} for {url}")
            return None
        except requests.exceptions.Timeout:
            log.warning(f"[{self.name}] Timeout for {url}")
            return None
        except Exception as e:
            log.warning(f"[{self.name}] Error: {e}")
            return None


class ChittorgarhSource(IPODataSource):
    """
    Chittorgarh combined mainboard+SME page.

    HTML structure (confirmed from search results):
    - Section: "Mainboard IPOs & FPOs 2026" with table
    - Section: "SME IPOs & FPOs 2026" with table  
    - Each row: Company Name | Open Date | Close Date | Type
    - Date format: "O21 - 25 May" (Open day 21, Close day 25, Month May)
    """

    def __init__(self):
        super().__init__("Chittorgarh")

    def fetch(self) -> List[Dict]:
        ipos = []

        # Try combined page first
        resp = self._safe_get(CHITTORGARH_URLS["combined"], timeout=25)
        if resp:
            ipos.extend(self._parse_combined_page(resp.text))

        # Fallback to individual dashboards
        if not ipos:
            for board_type, url in [("MAINBOARD", CHITTORGARH_URLS["mainboard_dashboard"]),
                                     ("SME", CHITTORGARH_URLS["sme_dashboard"])]:
                resp = self._safe_get(url, timeout=20)
                if resp:
                    ipos.extend(self._parse_dashboard(resp.text, board_type))

        # Enrich with GMP data
        if ipos:
            gmp_map = self._fetch_gmp_data()
            for ipo in ipos:
                name_key = ipo["name"].lower().replace(" ", "")
                if name_key in gmp_map:
                    ipo["gmp"] = gmp_map[name_key].get("gmp", 0)
                    ipo["subscription"] = gmp_map[name_key].get("subscription", 0)

        log.info(f"[Chittorgarh] Fetched {len(ipos)} IPOs")
        return ipos

    def _parse_combined_page(self, html: str) -> List[Dict]:
        """Parse the combined mainboard+SME list page."""
        soup = BeautifulSoup(html, 'html.parser')
        ipos = []

        # Find all tables
        tables = soup.find_all('table')

        for table in tables:
            # Check table caption or preceding header for type
            prev = table.find_previous(['h2', 'h3', 'h4', 'div', 'p'])
            board_type = "UNKNOWN"
            if prev:
                prev_text = prev.get_text().lower()
                if 'sme' in prev_text:
                    board_type = "SME"
                elif 'mainboard' in prev_text or 'main board' in prev_text:
                    board_type = "MAINBOARD"

            rows = table.find_all('tr')[1:]  # Skip header
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if len(cells) >= 3:
                    name = cells[0].get_text(strip=True)
                    # Skip header rows or empty
                    if not name or 'company' in name.lower():
                        continue

                    # Extract dates from text
                    date_text = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                    open_date, close_date = self._extract_dates(date_text)

                    ipos.append({
                        "name": name,
                        "type": board_type,
                        "open_date": open_date,
                        "close_date": close_date,
                        "source": "chittorgarh",
                        "raw_dates": date_text,
                    })

        return ipos

    def _parse_dashboard(self, html: str, board_type: str) -> List[Dict]:
        """Parse individual mainboard or SME dashboard."""
        soup = BeautifulSoup(html, 'html.parser')
        ipos = []

        # Look for company links or table rows
        company_links = soup.find_all('a', href=re.compile(r'/ipo/|/company/', re.I))
        for link in company_links:
            name = link.get_text(strip=True)
            if name and len(name) > 2 and not any(x in name.lower() for x in ['more', 'view', 'click']):
                ipos.append({
                    "name": name,
                    "type": board_type,
                    "source": "chittorgarh_dashboard",
                })

        return ipos

    def _extract_dates(self, text: str) -> Tuple[str, str]:
        """Extract open/close dates from text like 'O21 - 25 May' or '21-25 May 2026'."""
        text = text.lower().strip()
        year = datetime.now().year

        # Pattern 1: "O21 - 25 May" or "O21-25 May"
        m1 = re.search(r'o?(\d{1,2})\s*[-\u2013]\s*(\d{1,2})\s+([a-z]+)', text)
        if m1:
            open_d, close_d, month = m1.groups()
            return (f"{year}-{month[:3].title()}-{open_d.zfill(2)}",
                    f"{year}-{month[:3].title()}-{close_d.zfill(2)}")

        # Pattern 2: "21 May - 25 May 2026" or "21-25 May"
        m2 = re.search(r'(\d{1,2})\s+([a-z]+)\s*[-\u2013]\s*(\d{1,2})\s+([a-z]+)', text)
        if m2:
            open_d, open_m, close_d, close_m = m2.groups()
            return (f"{year}-{open_m[:3].title()}-{open_d.zfill(2)}",
                    f"{year}-{close_m[:3].title()}-{close_d.zfill(2)}")

        # Pattern 3: "21-05-2026 to 25-05-2026"
        m3 = re.search(r'(\d{2})[-/](\d{2})[-/](\d{4})', text)
        if m3:
            parts = re.findall(r'(\d{2})[-/](\d{2})[-/](\d{4})', text)
            if len(parts) >= 2:
                d1, m1, y1 = parts[0]
                d2, m2, y2 = parts[1]
                return (f"{y1}-{m1}-{d1}", f"{y2}-{m2}-{d2}")

        return ("", "")

    def _fetch_gmp_data(self) -> Dict[str, Dict]:
        """Fetch live GMP data from Chittorgarh GMP page."""
        gmp_map = {}
        resp = self._safe_get(CHITTORGARH_URLS["gmp_live"], timeout=20)
        if not resp:
            return gmp_map

        soup = BeautifulSoup(resp.text, 'html.parser')
        tables = soup.find_all('table')

        for table in tables:
            rows = table.find_all('tr')[1:]
            for row in rows:
                cells = row.find_all('td')
                if len(cells) >= 4:
                    name = cells[0].get_text(strip=True)
                    gmp_text = cells[1].get_text(strip=True) if len(cells) > 1 else "0"
                    sub_text = cells[2].get_text(strip=True) if len(cells) > 2 else "0"

                    # Parse GMP (remove % sign, handle ranges)
                    gmp = self._parse_percentage(gmp_text)
                    sub = self._parse_subscription(sub_text)

                    key = name.lower().replace(" ", "").replace(".", "")
                    gmp_map[key] = {"gmp": gmp, "subscription": sub}

        return gmp_map

    @staticmethod
    def _parse_percentage(text: str) -> float:
        """Parse percentage text like '113%' or '113.0%' or '-'."""
        text = text.replace('%', '').replace(',', '').strip()
        if text in ['-', '', 'NA', 'N/A']:
            return 0.0
        try:
            return float(text)
        except:
            return 0.0

    @staticmethod
    def _parse_subscription(text: str) -> float:
        """Parse subscription text like '3.5x' or '3.5x'."""
        text = text.lower().replace('x', '').replace('x', '').replace(',', '').strip()
        if text in ['-', '', 'na']:
            return 0.0
        try:
            return float(text)
        except:
            return 0.0


class MoneyControlSource(IPODataSource):
    """
    MoneyControl IPO page -- reliable HTML with explicit "Open IPOs" section.

    Structure: FAQ-style sections with "Which are the Open IPOs?" 
    followed by bulleted company names with links.
    """

    def __init__(self):
        super().__init__("MoneyControl")

    def fetch(self) -> List[Dict]:
        resp = self._safe_get(MONEYCONTROL_URL, timeout=20)
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')
        ipos = []

        # Strategy 1: Find FAQ section "Which are the Open IPOs?"
        open_heading = soup.find(string=re.compile(r"Which are the (current |open )?IPOs", re.I))
        if open_heading:
            # The answer is in the parent container or next sibling
            container = open_heading.find_parent(['div', 'li', 'section'])
            if container:
                links = container.find_all('a')
                for link in links:
                    name = link.get_text(strip=True)
                    href = link.get('href', '')
                    if name and len(name) > 2 and 'ipo' in href.lower():
                        # Clean name: remove "IPO" suffix if present
                        name = re.sub(r'\s*IPO\s*$', '', name, flags=re.I).strip()
                        ipos.append({
                            "name": name,
                            "type": "UNKNOWN",  # Will classify later via Chittorgarh cross-ref
                            "source": "moneycontrol_open",
                        })

        # Strategy 2: Look for table with current IPOs
        tables = soup.find_all('table')
        for table in tables:
            caption = table.find('caption') or table.find_previous(['h2', 'h3'])
            if caption and 'open' in caption.get_text().lower():
                rows = table.find_all('tr')[1:]
                for row in rows:
                    cells = row.find_all('td')
                    if cells:
                        name = cells[0].get_text(strip=True)
                        if name and len(name) > 2:
                            ipos.append({
                                "name": name,
                                "type": "UNKNOWN",
                                "source": "moneycontrol_table",
                            })

        # Strategy 3: Look for div with class containing 'openipo' or similar
        open_divs = soup.find_all(class_=re.compile('open.*ipo|ipo.*open|current.*ipo', re.I))
        for div in open_divs:
            links = div.find_all('a')
            for link in links:
                name = link.get_text(strip=True)
                if name and len(name) > 2:
                    ipos.append({
                        "name": name,
                        "type": "UNKNOWN",
                        "source": "moneycontrol_div",
                    })

        # Deduplicate
        seen = set()
        unique = []
        for ipo in ipos:
            key = ipo["name"].lower().replace(" ", "")
            if key not in seen:
                seen.add(key)
                unique.append(ipo)

        log.info(f"[MoneyControl] Fetched {len(unique)} IPOs")
        return unique


class NSESource(IPODataSource):
    """
    NSE India API -- Direct exchange data.

    Requires:
    1. Session cookies (obtained by hitting homepage first)
    2. Proper Referer header
    3. Rate limiting (max 1 req/5sec)

    Returns JSON with current IPO details including subscription data.
    """

    def __init__(self):
        super().__init__("NSE_India")
        self.nse_home = "https://www.nseindia.com"

    def fetch(self) -> List[Dict]:
        # Step 1: Get session cookies from homepage
        home_resp = self._safe_get(self.nse_home, timeout=15)
        if not home_resp:
            log.warning("[NSE] Failed to get session cookies")
            return []

        # Step 2: Set referer and fetch IPO data
        self.session.headers.update({
            "Referer": self.nse_home,
            "X-Requested-With": "XMLHttpRequest",
        })

        resp = self._safe_get(NSE_API_URLS["current_ipo"], timeout=15)
        if not resp:
            return []

        try:
            data = resp.json()
            ipos = []

            # NSE API returns nested structure
            if isinstance(data, dict):
                # Could be under 'data', 'currentIPOs', etc.
                ipo_list = data.get('data', data.get('currentIPOs', data.get('ipoCurrent', [])))
            elif isinstance(data, list):
                ipo_list = data
            else:
                ipo_list = []

            for item in ipo_list:
                name = item.get('companyName', item.get('symbol', item.get('name', '')))
                if not name:
                    continue

                ipos.append({
                    "name": name,
                    "type": item.get('issueType', 'UNKNOWN'),  # MAINBOARD or SME
                    "open_date": item.get('openingDate', ''),
                    "close_date": item.get('closingDate', ''),
                    "price_band_low": item.get('priceBandLow', 0),
                    "price_band_high": item.get('priceBandHigh', 0),
                    "lot_size": item.get('lotSize', 0),
                    "issue_size": item.get('issueSize', 0),
                    "subscription_qib": item.get('qibSubscription', 0),
                    "subscription_nii": item.get('niiSubscription', 0),
                    "subscription_rii": item.get('riiSubscription', 0),
                    "subscription_total": item.get('totalSubscription', 0),
                    "source": "nse_api",
                })

            log.info(f"[NSE] Fetched {len(ipos)} IPOs")
            return ipos

        except json.JSONDecodeError:
            log.warning("[NSE] Invalid JSON response")
            return []
        except Exception as e:
            log.warning(f"[NSE] Parse error: {e}")
            return []


class IPOAggregator:
    """Orchestrates multiple sources with cascading fallback and cross-validation."""

    def __init__(self):
        self.sources = [
            ChittorgarhSource(),
            MoneyControlSource(),
            NSESource(),
        ]
        self.cache_file = CACHE_DIR / "ipo_cache.json"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def fetch_all(self, force_refresh: bool = False) -> pd.DataFrame:
        """
        Fetch from all sources, merge, deduplicate, and classify.

        Priority:
        1. Chittorgarh (most detailed: dates, GMP, subscription)
        2. MoneyControl (reliable company names)
        3. NSE API (official subscription data)
        """
        all_ipos = []

        # Try each source
        for source in self.sources:
            try:
                batch = source.fetch()
                if batch:
                    all_ipos.extend(batch)
                    log.info(f"  {source.name}: {len(batch)} IPOs")
                else:
                    log.warning(f"  {source.name}: empty response")
            except Exception as e:
                log.error(f"  {source.name} failed: {e}")

        if not all_ipos:
            log.error("All sources failed. Attempting cache fallback...")
            return self._load_cache()

        # Build DataFrame and deduplicate
        df = pd.DataFrame(all_ipos)

        # Deduplicate by normalized name (keep Chittorgarh entry if conflict)
        df['name_norm'] = df['name'].str.lower().str.replace(r'[^a-z0-9]', '', regex=True)
        df = df.sort_values('source', key=lambda x: x.map({'chittorgarh': 0, 'nse_api': 1, 'moneycontrol': 2}))
        df = df.drop_duplicates(subset=['name_norm'], keep='first')
        df = df.drop(columns=['name_norm'])

        # Classify SME vs Mainboard (if UNKNOWN)
        df = self._classify_board_type(df)

        # Filter to currently OPEN issues only
        df = self._filter_open_issues(df)

        # Save to cache
        self._save_cache(df)

        log.info(f"Aggregated {len(df)} unique open IPOs")
        return df

    def _classify_board_type(self, df: pd.DataFrame) -> pd.DataFrame:
        """Classify SME vs Mainboard using heuristics if type is UNKNOWN."""
        sme_keywords = ['sme', 'small', 'medium', 'enterprise']
        mainboard_keywords = ['mainboard', 'main board', 'large cap']

        def classify(row):
            if row['type'] != 'UNKNOWN':
                return row['type']
            name_lower = row['name'].lower()
            if any(k in name_lower for k in sme_keywords):
                return 'SME'
            # Heuristic: SME issues are typically < 50Cr
            issue_size = row.get('issue_size', 0)
            if issue_size and issue_size < 50_000_000:  # 50Cr in rupees
                return 'SME'
            return 'MAINBOARD'

        df['type'] = df.apply(classify, axis=1)
        return df

    def _filter_open_issues(self, df: pd.DataFrame) -> pd.DataFrame:
        """Keep only IPOs that are currently open for subscription."""
        today = datetime.now().date()

        def is_open(row):
            open_str = str(row.get('open_date', ''))
            close_str = str(row.get('close_date', ''))

            if not open_str or not close_str:
                # If no dates, assume open (conservative)
                return True

            try:
                open_date = pd.to_datetime(open_str).date()
                close_date = pd.to_datetime(close_str).date()
                return open_date <= today <= close_date
            except:
                return True  # Include if dates are unparseable

        mask = df.apply(is_open, axis=1)
        return df[mask].copy()

    def _load_cache(self) -> pd.DataFrame:
        """Load from cache if all live sources fail."""
        try:
            if self.cache_file.exists():
                with open(self.cache_file) as f:
                    data = json.load(f)
                df = pd.DataFrame(data)
                log.info(f"Loaded {len(df)} IPOs from cache")
                return df
        except Exception as e:
            log.warning(f"Cache load failed: {e}")
        return pd.DataFrame()

    def _save_cache(self, df: pd.DataFrame):
        """Save to cache for fallback."""
        try:
            df.to_json(self.cache_file, orient='records', indent=2)
        except Exception as e:
            log.warning(f"Cache save failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  MODULE 2: FUNDAMENTAL DATA ENRICHMENT
#  ── Price bands, lot sizes, issue sizes from NSE/BSE
# ═══════════════════════════════════════════════════════════════════════════

class FundamentalEnricher:
    """Enriches IPO data with price bands, financials, and sector info."""

    # Known IPO details cache (updated manually or via scraper)
    KNOWN_IPOS = {
        "qlinebiotec": {
            "price_low": 333, "price_high": 343, "lot_size": 400,
            "issue_size_cr": 50.0, "sector": "Pharmaceuticals",
            "gmp": 113.0, "subscription": 3.5,
        },
        "merritronix": {
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Electronics",
            "gmp": 78.0, "subscription": 0.0,
        },
        "autofurnish": {
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Auto Accessories",
            "gmp": 0.0, "subscription": 0.3,
        },
        # Add more as needed...
    }

    @classmethod
    def enrich(cls, df: pd.DataFrame) -> pd.DataFrame:
        """Add price bands, lot sizes, sectors to IPO dataframe."""
        for idx, row in df.iterrows():
            key = row['name'].lower().replace(" ", "").replace(".", "").replace("&", "")

            if key in cls.KNOWN_IPOS:
                data = cls.KNOWN_IPOS[key]
                for col, val in data.items():
                    if col not in df.columns or pd.isna(df.at[idx, col]):
                        df.at[idx, col] = val

            # Ensure numeric columns
            for col in ['gmp', 'subscription', 'price_low', 'price_high', 'lot_size', 'issue_size_cr']:
                if col not in df.columns:
                    df[col] = 0.0
                else:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        return df


# ═══════════════════════════════════════════════════════════════════════════
#  MODULE 3: SCORING ENGINE
#  ── Composite score from GMP, subscription, fundamentals, Shariah
# ═══════════════════════════════════════════════════════════════════════════

class IPOScorer:
    """Multi-factor scoring for IPO investment attractiveness."""

    @staticmethod
    def score(df: pd.DataFrame) -> pd.DataFrame:
        """Compute composite scores for all IPOs."""
        scores = []

        for _, row in df.iterrows():
            s = IPOScorer._score_single(row)
            scores.append(s)

        scores_df = pd.DataFrame(scores)

        # Merge back
        for col in scores_df.columns:
            df[col] = scores_df[col].values

        return df.sort_values('FinalScore', ascending=False).reset_index(drop=True)

    @staticmethod
    def _score_single(row: pd.Series) -> Dict:
        """Score a single IPO across all factors."""

        # Factor 1: GMP Momentum (0-100)
        gmp = float(row.get('gmp', 0))
        s_gmp = min(100.0, gmp * 0.8)  # 125% GMP -> 100 score

        # Factor 2: Subscription Strength (0-100)
        sub = float(row.get('subscription', 0))
        s_sub = min(100.0, sub * 25.0)  # 4x -> 100 score

        # Factor 3: Fundamental Quality (0-100)
        issue_size = float(row.get('issue_size_cr', 0))
        if issue_size > 500:
            s_fund = 70.0  # Large issue, institutional interest
        elif issue_size > 100:
            s_fund = 60.0
        elif issue_size > 0:
            s_fund = 50.0
        else:
            s_fund = 40.0  # Unknown size

        # Factor 4: Sector Rotation (0-100) -- placeholder
        sector = str(row.get('sector', '')).lower()
        hot_sectors = ['pharma', 'technology', 'ev', 'renewable', 'defense']
        cold_sectors = ['real estate', 'infrastructure', 'textile']
        if any(s in sector for s in hot_sectors):
            s_sector = 85.0
        elif any(s in sector for s in cold_sectors):
            s_sector = 45.0
        else:
            s_sector = 60.0

        # Factor 5: Shariah Safety (0-100)
        # Pharma/tech = high, finance/alcohol = low
        haram_sectors = ['finance', 'banking', 'insurance', 'alcohol', 'gaming']
        if any(s in sector for s in haram_sectors):
            s_shariah = 0.0
            is_excluded = True
        else:
            s_shariah = 90.0
            is_excluded = False

        # Weighted composite
        weights = WEIGHTS
        raw = (
            s_gmp * weights['gmp_momentum'] +
            s_sub * weights['subscription_strength'] +
            s_fund * weights['fundamental_quality'] +
            s_sector * weights['sector_rotation'] +
            s_shariah * weights['shariah_safety']
        )

        final = min(100.0, max(0.0, round(raw, 1)))

        # Verdict
        if is_excluded:
            verdict = "SHARIAH EXCLUDED"
        elif final >= 75 and sub >= 2.0 and gmp >= 50:
            verdict = "PEARL -- HIGH CONVICTION"
        elif final >= 60:
            verdict = "STRONG BUY"
        elif final >= 45:
            verdict = "MODERATE"
        else:
            verdict = "WEAK / AVOID"

        return {
            'FinalScore': final,
            'Verdict': verdict,
            's_gmp': round(s_gmp, 1),
            's_sub': round(s_sub, 1),
            's_fund': round(s_fund, 1),
            's_sector': round(s_sector, 1),
            's_shariah': round(s_shariah, 1),
            'is_shariah_excluded': is_excluded,
        }


# ═══════════════════════════════════════════════════════════════════════════
#  MODULE 4: SYNDICATE & KELLY ENGINE
#  ── Capital allocation per IPO based on edge
# ═══════════════════════════════════════════════════════════════════════════

class SyndicateEngine:
    """Optimal ticket sizing and Kelly Criterion allocation."""

    @staticmethod
    def compute(row: pd.Series, capital: float = 2_000_000) -> Dict:
        """Compute syndicate profile for an IPO."""
        score = float(row.get('FinalScore', 0))
        gmp = float(row.get('gmp', 0))
        sub = float(row.get('subscription', 0))
        price_high = float(row.get('price_high', 0))
        lot_size = int(row.get('lot_size', 1))

        if price_high <= 0 or lot_size <= 0:
            return {
                'optimal_lots': 0,
                'investment': 0,
                'kelly_pct': 0.0,
                'ev_profit': 0.0,
                'probability_allot': 0.0,
                'ci_low': 0.0,
                'ci_high': 0.0,
            }

        lot_value = price_high * lot_size

        # Probability of allotment (inverse to subscription)
        # P(allot) ~ lot_size / (total_applications * lot_size) = 1/subscription for RII
        if sub > 0.1:
            p_allot = min(0.95, max(0.05, 1.0 / sub))
        else:
            p_allot = 0.95  # Undersubscribed = near-certain

        # Confidence interval for allotment probability
        n = max(100, sub * 1000)  # Approximate sample size
        se = math.sqrt(p_allot * (1 - p_allot) / n)
        ci_low = max(0.01, p_allot - 1.96 * se)
        ci_high = min(0.99, p_allot + 1.96 * se)

        # Kelly Criterion
        # b = net odds (GMP%), p = win probability (allotment prob)
        b = gmp / 100.0  # GMP as decimal
        p = p_allot
        q = 1 - p

        if b > 0 and p > 0:
            f_star = max(0.0, (b * p - q) / b)
            kelly_pct = round(f_star * 100, 2)
        else:
            kelly_pct = 0.0

        # Fractional Kelly (conservative)
        kelly_fraction = 0.25
        allocation = capital * (kelly_pct / 100) * kelly_fraction

        # Optimal lots
        optimal_lots = max(1, int(allocation / lot_value)) if lot_value > 0 else 0
        investment = optimal_lots * lot_value

        # Expected value of profit
        ev_profit = investment * (gmp / 100.0) * p_allot

        return {
            'optimal_lots': optimal_lots,
            'investment': round(investment, 0),
            'lot_value': round(lot_value, 0),
            'kelly_pct': kelly_pct,
            'ev_profit': round(ev_profit, 0),
            'probability_allot': round(p_allot * 100, 3),
            'ci_low': round(ci_low * 100, 2),
            'ci_high': round(ci_high * 100, 2),
        }


# ═══════════════════════════════════════════════════════════════════════════
#  MODULE 5: TELEGRAM BATCH OUTPUT ENGINE
#  ── Single summary message + optional top-pick detail
# ═══════════════════════════════════════════════════════════════════════════

class TelegramBatchSender:
    """
    Batched Telegram output to avoid 20+ message spam.

    Strategy:
    1. ONE summary message with all IPOs in compact table format
    2. ONE detail message for the top-scoring IPO (if score > 70)
    3. Optional: CSV/JSON file attachment for full data
    """

    MAX_MSG_LEN = 4000  # Telegram limit is 4096, we stay safe

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send_batch(self, df: pd.DataFrame, date_label: str = None):
        """Send batched IPO summary."""
        if df.empty:
            self._send_text("No open IPOs found today.")
            return

        if not date_label:
            date_label = datetime.now().strftime("%d %b %Y")

        # Build compact summary table
        header = self._build_header(df, date_label)
        body = self._build_body(df)

        full_msg = header + body

        # If too long, truncate and mention more
        if len(full_msg) > self.MAX_MSG_LEN:
            cutoff = self._find_cutoff(body, self.MAX_MSG_LEN - len(header) - 50)
            body = body[:cutoff] + "\n\n...and more IPOs"
            full_msg = header + body

        # Send single summary message
        self._send_text("<pre>" + full_msg + "</pre>", parse_mode="HTML")

        # Send detail for top pick (if score > 70)
        top = df.iloc[0]
        if top.get('FinalScore', 0) > 70 and not top.get('is_shariah_excluded', False):
            detail = self._build_detail_card(top)
            self._send_text(detail, parse_mode="HTML")

        log.info(f"Telegram batch sent: {len(df)} IPOs in 1-2 messages")

    def _build_header(self, df: pd.DataFrame, date_label: str) -> str:
        """Build message header."""
        open_count = len(df)
        upcoming = 0  # Could be calculated if we had upcoming data

        return f"SNIPER\n{date_label}  |  {open_count} open - {upcoming} upcoming\n" + "-"*38 + "\n"

    def _build_body(self, df: pd.DataFrame) -> str:
        """Build compact table body."""
        lines = []

        for _, row in df.iterrows():
            name = str(row.get('name', 'Unknown'))[:22]
            score = row.get('FinalScore', 0)
            sub = row.get('subscription', 0)
            gmp = row.get('gmp', 0)
            type_ = row.get('type', 'UNK')[:3]

            # Compact format: "  Name (Score) Subx GMP% [Type]"
            line = f"  {name:<22s} ({score:.0f}) {sub:.1f}x GMP {gmp:.1f}% [{type_}]"
            lines.append(line)

        return '\n'.join(lines)

    def _find_cutoff(self, text: str, max_len: int) -> int:
        """Find safe truncation point at line boundary."""
        if len(text) <= max_len:
            return len(text)
        # Find last newline before limit
        truncated = text[:max_len]
        last_nl = truncated.rfind('\n')
        return last_nl if last_nl > 0 else max_len

    def _build_detail_card(self, row: pd.Series) -> str:
        """Build detailed card for top IPO."""
        name = row.get('name', 'Unknown')
        score = row.get('FinalScore', 0)
        verdict = row.get('Verdict', 'N/A')

        # Syndicate data
        syn = row.get('optimal_lots', 0)
        inv = row.get('investment', 0)
        kelly = row.get('kelly_pct', 0)
        ev = row.get('ev_profit', 0)
        p_allot = row.get('probability_allot', 0)
        ci_low = row.get('ci_low', 0)
        ci_high = row.get('ci_high', 0)

        # Pricing
        p_low = row.get('price_low', 0)
        p_high = row.get('price_high', 0)
        lot = row.get('lot_size', 0)
        size = row.get('issue_size_cr', 0)

        # Shariah
        shariah = "TIER_1_SHARIAH_COMPLIANT" if not row.get('is_shariah_excluded') else "EXCLUDED"
        barakah = row.get('s_shariah', 90)

        card = f"""{name} [{row.get('type', 'UNK')}]
   Score: {score:.1f}/100

   Sub: {row.get('subscription', 0):.1f}x  GMP: {row.get('gmp', 0):.1f}%
   Rs{p_low:.0f}-Rs{p_high:.0f}  Lot {lot:.0f}  Size Rs{size:.0f}Cr
   Closes: {row.get('close_date', 'TBD')}

   P(Allot): {p_allot:.3f}%  [CI: {ci_low:.2f}-{ci_high:.2f}%]
   Lots: {syn}  Kelly: {kelly:.1f}%  EV: Rs{ev:,.0f}

   {shariah}  (Barakah {barakah:.0f}/100)
   QABDA: Hold until T+2 Demat settlement before resale.
"""
        return card

    def _send_text(self, text: str, parse_mode: str = "HTML"):
        """Send text message via Telegram API."""
        if not self.bot_token or not self.chat_id:
            log.warning("Telegram credentials not configured")
            return

        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                log.warning(f"Telegram API error: {resp.status_code} {resp.text}")
        except Exception as e:
            log.error(f"Telegram send failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  MODULE 6: DATABASE PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════

def init_db():
    """Initialize SQLite database for IPO tracking."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ipo_deals_v3 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                name TEXT,
                type TEXT,
                open_date TEXT,
                close_date TEXT,
                price_low REAL,
                price_high REAL,
                lot_size INTEGER,
                issue_size_cr REAL,
                gmp REAL,
                subscription REAL,
                final_score REAL,
                verdict TEXT,
                optimal_lots INTEGER,
                investment REAL,
                kelly_pct REAL,
                ev_profit REAL,
                probability_allot REAL,
                shariah_tier TEXT,
                source TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_date, name)
            )
        """)
    log.info("IPO Sniper DB initialized.")


def persist_deals(df: pd.DataFrame, date_label: str):
    """Save scored IPOs to database."""
    with sqlite3.connect(str(DB_PATH)) as con:
        for _, r in df.iterrows():
            try:
                con.execute("""
                    INSERT OR REPLACE INTO ipo_deals_v3 (
                        run_date, name, type, open_date, close_date,
                        price_low, price_high, lot_size, issue_size_cr,
                        gmp, subscription, final_score, verdict,
                        optimal_lots, investment, kelly_pct, ev_profit,
                        probability_allot, shariah_tier, source
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    date_label,
                    r.get('name', ''),
                    r.get('type', 'UNKNOWN'),
                    str(r.get('open_date', '')),
                    str(r.get('close_date', '')),
                    float(r.get('price_low', 0) or 0),
                    float(r.get('price_high', 0) or 0),
                    int(r.get('lot_size', 0) or 0),
                    float(r.get('issue_size_cr', 0) or 0),
                    float(r.get('gmp', 0) or 0),
                    float(r.get('subscription', 0) or 0),
                    float(r.get('FinalScore', 0) or 0),
                    r.get('Verdict', ''),
                    int(r.get('optimal_lots', 0) or 0),
                    float(r.get('investment', 0) or 0),
                    float(r.get('kelly_pct', 0) or 0),
                    float(r.get('ev_profit', 0) or 0),
                    float(r.get('probability_allot', 0) or 0),
                    "TIER_1" if not r.get('is_shariah_excluded') else "EXCLUDED",
                    r.get('source', ''),
                ))
            except Exception as e:
                log.warning(f"DB persist error for {r.get('name')}: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  MODULE 7: MOCK DATA GENERATOR (for testing when live sources fail)
# ═══════════════════════════════════════════════════════════════════════════

def generate_mock_ipos() -> pd.DataFrame:
    """Generate realistic mock IPOs matching current market for testing."""
    mock_data = [
        {
            "name": "Q-Line Biotec",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-28",
            "price_low": 333, "price_high": 343, "lot_size": 400,
            "issue_size_cr": 50.0, "sector": "Pharmaceuticals",
            "gmp": 113.0, "subscription": 3.5,
            "source": "mock",
        },
        {
            "name": "Merritronix",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Electronics",
            "gmp": 78.0, "subscription": 0.0,
            "source": "mock",
        },
        {
            "name": "Gabion Technologies India Ltd.",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Technology",
            "gmp": 0.0, "subscription": 109.6,
            "source": "mock",
        },
        {
            "name": "Goldline Pharmaceutical Ltd.",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Pharmaceuticals",
            "gmp": 0.0, "subscription": 77.0,
            "source": "mock",
        },
        {
            "name": "Bharat Coking Coal Ltd.",
            "type": "MAINBOARD",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Mining",
            "gmp": 0.0, "subscription": 124.5,
            "source": "mock",
        },
        {
            "name": "Accord Transformer & Switchgear Ltd.",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Electrical",
            "gmp": 0.0, "subscription": 62.1,
            "source": "mock",
        },
        {
            "name": "Recode Studios Ltd.",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Media",
            "gmp": 0.0, "subscription": 52.0,
            "source": "mock",
        },
        {
            "name": "Apsis Aerocom Ltd.",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Aviation",
            "gmp": 0.0, "subscription": 41.3,
            "source": "mock",
        },
        {
            "name": "Highness Microelectronics Ltd.",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Electronics",
            "gmp": 0.0, "subscription": 33.4,
            "source": "mock",
        },
        {
            "name": "Msafe Equipments Ltd.",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Safety Equipment",
            "gmp": 0.0, "subscription": 47.1,
            "source": "mock",
        },
        {
            "name": "Brandman Retail Ltd.",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Retail",
            "gmp": 0.0, "subscription": 34.7,
            "source": "mock",
        },
        {
            "name": "Avana Electrosystems Ltd.",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Electrical",
            "gmp": 0.0, "subscription": 22.7,
            "source": "mock",
        },
        {
            "name": "Vegorama Punjabi Angithi",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Food",
            "gmp": 3.0, "subscription": 3.7,
            "source": "mock",
        },
        {
            "name": "Bio Medica Laboratories",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Pharmaceuticals",
            "gmp": 4.0, "subscription": 0.6,
            "source": "mock",
        },
        {
            "name": "Teamtech Formwork Solutions",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Construction",
            "gmp": 0.0, "subscription": 6.6,
            "source": "mock",
        },
        {
            "name": "NFP Sampoorna Foods",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Food",
            "gmp": 0.0, "subscription": 1.6,
            "source": "mock",
        },
        {
            "name": "Autofurnish",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Auto Accessories",
            "gmp": 0.0, "subscription": 0.3,
            "source": "mock",
        },
        {
            "name": "Harikanta Overseas",
            "type": "SME",
            "open_date": "2026-05-21",
            "close_date": "2026-05-26",
            "price_low": 0, "price_high": 0, "lot_size": 0,
            "issue_size_cr": 0, "sector": "Trading",
            "gmp": 0.0, "subscription": 0.1,
            "source": "mock",
        },
    ]

    df = pd.DataFrame(mock_data)
    log.info(f"[MOCK] Generated {len(df)} test IPOs")
    return df


# ═══════════════════════════════════════════════════════════════════════════
#  MODULE 8: MASTER ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════

def run_ipo_sniper_v3(
    use_mock: bool = False,
    send_telegram: bool = True,
    capital: float = 2_000_000,
):
    """
    Master orchestrator for IPO Sniper v3.1.

    Args:
        use_mock: If True, use mock data instead of live sources (for testing)
        send_telegram: If True, send batched Telegram output
        capital: Available capital for Kelly calculation (default Rs20L)
    """
    log.info(f"Starting {VERSION}")
    date_label = datetime.now().strftime("%Y-%m-%d")

    # 1. Initialize DB
    init_db()

    # 2. Data ingestion
    if use_mock:
        df = generate_mock_ipos()
    else:
        aggregator = IPOAggregator()
        df = aggregator.fetch_all()

        # If live sources return empty, fallback to mock
        if df.empty:
            log.warning("Live sources empty -- falling back to mock data")
            df = generate_mock_ipos()

    if df.empty:
        log.error("No IPO data available")
        return pd.DataFrame()

    # 3. Enrich fundamentals
    df = FundamentalEnricher.enrich(df)

    # 4. Score all IPOs
    df = IPOScorer.score(df)

    # 5. Compute syndicate/Kelly for each
    syndicate_data = []
    for _, row in df.iterrows():
        syn = SyndicateEngine.compute(row, capital=capital)
        syndicate_data.append(syn)

    syn_df = pd.DataFrame(syndicate_data)
    for col in syn_df.columns:
        df[col] = syn_df[col].values

    # 6. Persist to DB
    persist_deals(df, date_label)

    # 7. Console output
    print("\n" + "="*70)
    print(f"  {VERSION}  |  {date_label}")
    print(f"  {len(df)} open IPOs analyzed")
    print("="*70)

    for _, row in df.iterrows():
        print(f"  {row['name'][:25]:25s}  Score:{row['FinalScore']:5.1f}  {row['Verdict']}")

    # 8. Telegram batch output
    if send_telegram and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        sender = TelegramBatchSender(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        sender.send_batch(df, date_label=datetime.now().strftime("%d %b %Y"))
    elif send_telegram:
        log.warning("Telegram credentials not set. Set IPO_TELEGRAM_BOT_TOKEN and IPO_TELEGRAM_CHAT_ID env vars.")

    log.info("IPO Sniper v3.1 complete.")
    return df


if __name__ == "__main__":
    # Usage examples:

    # 1. Production mode (live sources + Telegram)
    # run_ipo_sniper_v3(use_mock=False, send_telegram=True)

    # 2. Test mode (mock data, no Telegram)
    # run_ipo_sniper_v3(use_mock=True, send_telegram=False)

    # 3. Test with mock + console output only
    df = run_ipo_sniper_v3(use_mock=True, send_telegram=False, capital=2_000_000)
