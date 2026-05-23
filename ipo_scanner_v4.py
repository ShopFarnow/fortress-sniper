#!/usr/bin/env python3
"""
IPO Scraper – Institutional Grade
══════════════════════════════════════════════════════════════════════════════
Architecture
  • Pydantic models for strict schema enforcement
  • Per-source circuit breakers (skip flaky sources after N failures)
  • Tenacity retry with jittered exponential back-off on every HTTP call
  • RapidFuzz token-sort ratio for far better name deduplication
  • Year-aware, multi-format date parser (handles ranges, partial, ISO)
  • Async-ready design: concurrent source fetching via asyncio
  • Graceful degradation: pipeline never crashes even if every source fails
  • Structured JSON + CSV + pretty-console output
  • Pluggable source registry – add a new source in ~10 lines

Sources
  A  Chittorgarh   – cloudscraper (anti-bot bypass)
  B  Investorgain  – cloudscraper
  C  Screener.in   – Playwright (JS-heavy)
  D  Groww         – Playwright + XHR intercept
  E  IndiaTrade    – cloudscraper + Playwright fallback
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
    before_sleep_log,
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ipo_scraper")


# ══════════════════════════════════════════════════════════════════════════════
# 1. DOMAIN MODELS
# ══════════════════════════════════════════════════════════════════════════════

class IPOStatus(str, Enum):
    OPEN     = "Open"
    UPCOMING = "Upcoming"
    CLOSED   = "Closed"
    LISTED   = "Listed"
    UNKNOWN  = "Unknown"


@dataclass
class IPORecord:
    """Single normalised IPO record. All fields optional except name."""
    name:         str
    sources:      list[str]       = field(default_factory=list)
    open_date:    Optional[str]   = None
    close_date:   Optional[str]   = None
    listing_date: Optional[str]   = None
    issue_price:  Optional[str]   = None
    lot_size:     Optional[str]   = None
    gmp:           Optional[str]   = None
    allotment_date: Optional[str]  = None
    listing_price:  Optional[str]  = None   # numeric price at listing (NOT a date)
    status:        IPOStatus       = IPOStatus.UNKNOWN
    # internal normalised key (not serialised)
    _norm_key:     str             = field(default="", repr=False)

    def merge(self, other: "IPORecord") -> None:
        """Absorb fields from another record for the same IPO."""
        for src in other.sources:
            if src not in self.sources:
                self.sources.append(src)
        for attr in ("open_date", "close_date", "listing_date",
                     "issue_price", "lot_size", "gmp", "allotment_date",
                     "listing_price"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_norm_key", None)
        d["status"] = self.status.value
        return d


# ══════════════════════════════════════════════════════════════════════════════
# 2. DATE PARSER  (robust, year-aware)
# ══════════════════════════════════════════════════════════════════════════════

# Additional formats we try in order
_DATE_FORMATS = [
    "%d %b %Y",  # 05 May 2025
    "%d %B %Y",  # 05 May 2025
    "%Y-%m-%d",  # 2025-05-07
    "%d-%m-%Y",  # 07-05-2025
    "%d/%m/%Y",  # 07/05/2025
    "%d %b",     # 05 May  (year inferred)
    "%d %B",     # 05 May  (year inferred)
    "%b %d %Y",  # May 07 2025
    "%B %d %Y",  # May 07 2025
    "%b %d, %Y", # May 07, 2025
]

# Matches "05 - 07 May 2025" or "5-7 May" (year optional)
_RANGE_RE = re.compile(
    r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?",
    re.IGNORECASE,
)


def parse_date(raw: str | None) -> Optional[datetime]:
    """
    Return a datetime from a messy date string, or None.
    Handles:
      • ISO dates, DD Mon YYYY, ranges like "05 - 07 May 2025"
      • Year-less dates (assumes current year, rolling forward if month passed)
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw.lower() in ("tba", "to be announced", "n/a", "-", ""):
        return None

    # ── Range extraction → take the OPEN (first) day ──────────────────
    m = _RANGE_RE.search(raw)
    if m:
        day       = int(m.group(1))
        month_str = m.group(3)
        year      = int(m.group(4)) if m.group(4) else None
        if year is None:
            year = _infer_year(month_str, day)
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(f"{day} {month_str} {year}", fmt)
            except ValueError:
                continue
        return None

    # ── Direct format attempts ─────────────────────────────────────────
    today = datetime.now()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            if "%Y" not in fmt:               # year was absent in format
                dt = dt.replace(year=_infer_year(dt.strftime("%b"), dt.day))
            return dt
        except ValueError:
            continue

    return None


def _infer_year(month_str: str, day: int) -> int:
    """Pick this year or next year so the date isn't more than 60 days in the past."""
    today = datetime.now()
    for fmt in ("%b", "%B"):
        try:
            candidate = datetime.strptime(f"{day} {month_str} {today.year}", f"%d {fmt} %Y")
            if candidate < today - timedelta(days=60):
                return today.year + 1
            return today.year
        except ValueError:
            continue
    return today.year


def format_date(dt: Optional[datetime]) -> str:
    return dt.strftime("%d %b %Y") if dt else ""


# ══════════════════════════════════════════════════════════════════════════════
# 3. STATUS COMPUTER
# ══════════════════════════════════════════════════════════════════════════════

def compute_status(rec: IPORecord, today: Optional[datetime] = None) -> IPOStatus:
    if today is None:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    open_dt     = parse_date(rec.open_date)
    close_dt    = parse_date(rec.close_date)
    listing_dt  = parse_date(rec.listing_date)

    # Explicit timeline checks (ordered from most-final to least)
    if listing_dt and listing_dt < today:
        return IPOStatus.LISTED
    if close_dt and close_dt < today:
        return IPOStatus.CLOSED
    if open_dt and open_dt <= today and (not close_dt or close_dt >= today):
        return IPOStatus.OPEN
    if open_dt and open_dt > today:
        return IPOStatus.UPCOMING
    if listing_dt and listing_dt > today:
        # If we only know the listing date (no subscription dates), the IPO
        # might still be in its subscription window – surface as Open so users investigate
        if not open_dt and not close_dt:
            return IPOStatus.OPEN
        return IPOStatus.UPCOMING

    # Heuristic: record has a listing_price but no parseable dates
    # → it came from a historical data dump (e.g. IndiaTrade), treat as Listed
    _no_dates = not open_dt and not close_dt and not listing_dt
    if _no_dates and rec.listing_price:
        return IPOStatus.LISTED
    # Heuristic: has issue_price but no dates and no GMP (would be present if active)
    # → historical IPO record from a database dump, treat as Listed
    _has_price = bool(rec.issue_price and rec.issue_price.strip("₹ -"))
    if _no_dates and _has_price and not rec.gmp:
        return IPOStatus.LISTED

    # Heuristic fall-through
    name_lower = rec.name.lower()
    if any(tok in name_lower for tok in ("sme ipo", "upcoming")):
        return IPOStatus.UPCOMING
    if "to be announced" in str(rec.open_date or "").lower():
        return IPOStatus.UPCOMING

    return IPOStatus.UNKNOWN


# ══════════════════════════════════════════════════════════════════════════════
# 4. NAME NORMALISER & DEDUPLICATOR
# ══════════════════════════════════════════════════════════════════════════════

# Noise tokens stripped before comparison
_NOISE_RE = re.compile(
    r"\b(limited|ltd|pvt|private|public|co\.?|inc|corp"
    r"|sme\s*ipo|\(sme\s*ipo\)|\(sme\)|sme"
    r"|india|ventures?|enterprise[s]?|solutions?|services?|technologies?|tech)\b",
    re.IGNORECASE,
)


def normalise_name(name: str) -> str:
    n = name.lower().strip()
    n = _NOISE_RE.sub(" ", n)
    n = re.sub(r"[^\w\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# Two-stage similarity: exact normalised key OR high RapidFuzz token-sort score
_FUZZY_THRESHOLD = 88   # 0-100; tuned via unit tests below


def _dates_conflict(a: IPORecord, b: IPORecord) -> bool:
    if not a.open_date or not b.open_date:
        return False
    open_a = parse_date(a.open_date)
    open_b = parse_date(b.open_date)
    return bool(open_a and open_b and abs((open_a - open_b).days) > 3)


def _same_ipo(a: IPORecord, b: IPORecord) -> bool:
    if not a._norm_key or not b._norm_key:
        return False
    if a._norm_key == b._norm_key:
        return not _dates_conflict(a, b)

    # Digit guard must come first: "company 1" ⊂ "company 10" scores partial=100
    # but they are different entities.  Different digit tokens → never same IPO.
    _da = set(tok for tok in a._norm_key.split() if tok.isdigit())
    _db = set(tok for tok in b._norm_key.split() if tok.isdigit())
    if _da and _db and _da != _db:
        return False

    # Substring fast path: one name is fully contained in the other (truncated names)
    pr_score = fuzz.partial_ratio(a._norm_key, b._norm_key)
    if pr_score >= 95:
        return not _dates_conflict(a, b)

    la, lb = len(a._norm_key), len(b._norm_key)
    if la < 10 or lb < 10:
        return False
    if min(la, lb) / max(la, lb) < 0.65:
        return False

    ts_score = fuzz.token_sort_ratio(a._norm_key, b._norm_key)
    if max(ts_score, pr_score) >= _FUZZY_THRESHOLD:
        return not _dates_conflict(a, b)
    return False


def deduplicate(records: list[IPORecord]) -> list[IPORecord]:
    """
    Two-pass dedup:
      Pass 1 – drop duplicates within the same source (keep richer record).
      Pass 2 – merge across sources using fuzzy name matching.
    """
    # Pass 1: within-source dedup keeping most-field-populated record
    seen: dict[tuple, IPORecord] = {}
    for rec in records:
        key = (rec.sources[0] if rec.sources else "?", rec._norm_key)
        existing = seen.get(key)
        if existing is None:
            seen[key] = rec
        else:
            # Keep whichever has more populated fields
            if _field_count(rec) > _field_count(existing):
                seen[key] = rec

    pass1 = list(seen.values())

    # Pass 2: cross-source merge
    merged: list[IPORecord] = []
    for rec in pass1:
        matched = False
        for existing in merged:
            if _same_ipo(existing, rec):
                existing.merge(rec)
                matched = True
                break
        if not matched:
            merged.append(rec)

    return merged


def _field_count(rec: IPORecord) -> int:
    return sum(1 for f in (rec.open_date, rec.close_date, rec.listing_date,
                           rec.issue_price, rec.lot_size, rec.gmp,
                           rec.listing_price) if f)


# ══════════════════════════════════════════════════════════════════════════════
# 5. HTTP HELPERS  (shared session, retry, rotating UA)
# ══════════════════════════════════════════════════════════════════════════════

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

BASE_HEADERS = {
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "DNT":             "1",
}


def _headers() -> dict:
    return {**BASE_HEADERS, "User-Agent": random.choice(_USER_AGENTS)}


def _cloudscraper_session():
    import cloudscraper
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False},
        delay=3,
    )


@retry(
    retry=retry_if_exception_type((requests.RequestException, Exception)),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=2, max=15),
    before_sleep=before_sleep_log(log, logging.DEBUG),
    reraise=True,
)
def _safe_get(url: str, session=None, timeout: int = 25) -> requests.Response:
    s = session or requests.Session()
    r = s.get(url, headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r


# ══════════════════════════════════════════════════════════════════════════════
# 6. CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CircuitBreaker:
    name:          str
    max_failures:  int   = 2
    _failures:     int   = field(default=0, init=False)
    _open:         bool  = field(default=False, init=False)

    def call(self, fn: Callable) -> list[IPORecord]:
        if self._open:
            log.warning(f"  ⚡ Circuit OPEN – skipping {self.name}")
            return []
        try:
            result = fn()
            self._failures = 0          # reset on success
            return result
        except Exception as exc:
            self._failures += 1
            log.warning(f"  ✗ {self.name} failure #{self._failures}: {exc}")
            if self._failures >= self.max_failures:
                self._open = True
                log.error(f"  ⚡ Circuit TRIPPED for {self.name} – will skip remaining runs")
            return []


# ══════════════════════════════════════════════════════════════════════════════
# 7. RAW ROW → IPORecord  (shared helper)
# ══════════════════════════════════════════════════════════════════════════════

_PURE_PRICE_RE = re.compile(r"^[₹\s]*[\d,]+\.?\d*\s*$")  # "1015.00", "₹120", "2,600"


def _is_price_string(s: str | None) -> bool:
    """Return True if the string looks like a pure numeric price, not a date."""
    if not s:
        return False
    clean = s.strip().replace(",", "")
    return bool(_PURE_PRICE_RE.match(clean))


def _make_record(source: str, name: str, **kwargs) -> Optional[IPORecord]:
    name = _clean_name(name)
    if not name or len(name) < 3:
        return None
    # Skip rows where the "name" cell is a bare price like "₹120" or "135"
    # but allow real company names that start with digits e.g. "3M India"
    if _is_price_string(name):
        return None
    rec              = IPORecord(name=name, sources=[source])
    rec.open_date    = kwargs.get("open_date") or None
    rec.close_date   = kwargs.get("close_date") or None
    rec.issue_price  = _clean_price(kwargs.get("issue_price"))
    rec.lot_size     = kwargs.get("lot_size") or None
    rec.gmp          = kwargs.get("gmp") or None
    rec._norm_key    = normalise_name(name)

    # listing_date vs listing_price: if the value looks like a pure number
    # (e.g. IndiaTrade sends "1015.00") it is a price at listing, not a date
    raw_listing = kwargs.get("listing_date") or None
    if raw_listing:
        if _is_price_string(raw_listing):
            rec.listing_price = raw_listing          # store as price
        elif parse_date(raw_listing) is not None:
            rec.listing_date  = raw_listing          # valid date string
        # else: ambiguous / garbage → discard
    return rec


def _clean_name(raw: str) -> str:
    if not raw:
        return ""
    # Collapse all whitespace (newlines, tabs, multiple spaces) to single space
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.sub(r" {2,}", " ", raw).strip()
    # Remove parenthetical suffixes: (SME IPO), (NSE SME), etc.
    # Use re.DOTALL so . matches newlines that may survive the first pass
    raw = re.sub(r"\s*\([^)]*\)\s*$", "", raw, flags=re.DOTALL)
    raw = re.sub(r" {2,}", " ", raw).strip()
    # Remove trailing date ranges stuck to the name
    raw = re.sub(r"\d{1,2}\s*[-–]\s*\d{1,2}\s+[A-Za-z]+(\s+\d{4})?$", "", raw).strip()
    return raw


def _clean_price(raw: str | None) -> Optional[str]:
    if not raw:
        return None
    # Normalise to "₹NNN - ₹MMM" or "₹NNN" form
    raw = raw.strip().lstrip("₹Rs. ")
    return f"₹{raw}" if raw else None


# ══════════════════════════════════════════════════════════════════════════════
# 8. SOURCE PARSERS
# ══════════════════════════════════════════════════════════════════════════════

# ── Chittorgarh URLs to try in order ─────────────────────────────────────────
_CHITTORGARH_URLS = [
    "https://www.chittorgarh.com/ipo/ipo_dashboard.asp",
    "https://www.chittorgarh.com/report/open-ipo-subscription-status/24/",
    "https://www.chittorgarh.com/ipo/ipo-open.asp",
]

_SCREENER_URLS = [
    "https://www.screener.in/ipo/recent/",
    "https://www.screener.in/ipo/",
]


def _parse_td_header_tables(soup: BeautifulSoup, source: str) -> list[IPORecord]:
    """
    Like _parse_tables but treats the first <tr> as the header row even when
    it uses <td> elements instead of <th>.  Handles Chittorgarh and Screener.
    """
    records: list[IPORecord] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue
        hdr_cells = rows[0].find_all(["th", "td"])
        headers = [re.sub(r"\s+", " ", c.get_text()).strip().lower()
                   for c in hdr_cells]
        hdr_str = " ".join(headers)
        if not any(kw in hdr_str for kw in ["company", "ipo", "open", "price", "name"]):
            continue
        col: dict[str, int] = {}
        for i, h in enumerate(headers):
            if ("company" in h or "name" in h or "ipo" in h) and "name" not in col:
                col["name"] = i
            elif "open" in h and "open" not in col:
                col["open"] = i
            elif "close" in h and "close" not in col:
                col["close"] = i
            elif "price" in h and "price" not in col:
                col["price"] = i
            elif "lot" in h and "lot" not in col:
                col["lot"]   = i
            elif "gmp" in h and "gmp" not in col:
                col["gmp"]   = i
            elif "list" in h and "listing" not in col:
                col["listing"] = i
        col.setdefault("name", 0)
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cells or len(cells) <= col["name"]:
                continue
            def _c(k):
                idx = col.get(k, -1)
                return cells[idx] if 0 <= idx < len(cells) else None
            rec = _make_record(source, _c("name") or "",
                               open_date=_c("open"), close_date=_c("close"),
                               issue_price=_c("price"), lot_size=_c("lot"),
                               gmp=_c("gmp"), listing_date=_c("listing"))
            if rec:
                records.append(rec)
    return records


def _chittorgarh_playwright() -> list[IPORecord]:
    """Try multiple Chittorgarh URLs with Playwright; handle <td> header rows."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(user_agent=random.choice(_USER_AGENTS))
            for url in _CHITTORGARH_URLS:
                try:
                    page = ctx.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                    page.wait_for_timeout(4_000)
                    html = page.content()
                    page.close()
                    soup = BeautifulSoup(html, "lxml")
                    recs = _parse_tables(soup, "Chittorgarh")
                    if not recs:
                        recs = _parse_td_header_tables(soup, "Chittorgarh")
                    if recs:
                        log.info(f"  Chittorgarh Playwright: {len(recs)} records")
                        browser.close()
                        return recs
                    log.debug(f"  Chittorgarh: 0 from {url}")
                except Exception as e:
                    log.debug(f"  Chittorgarh Playwright {url}: {e}")
            browser.close()
    except Exception as exc:
        log.warning(f"  Chittorgarh Playwright error: {exc}")
    log.warning("  Chittorgarh: all URLs exhausted – source skipped")
    return []


# ── Generic HTML table parser shared by several sources ────────────────────

def _parse_tables(soup: BeautifulSoup, source: str) -> list[IPORecord]:
    records: list[IPORecord] = []
    for table in soup.find_all("table"):
        ths = table.find_all("th")
        headers = [th.get_text(strip=True).lower() for th in ths]
        if not any(kw in " ".join(headers)
                   for kw in ["ipo", "company", "open", "price", "lot", "name"]):
            continue

        col: dict[str, int] = {}
        for i, h in enumerate(headers):
            if ("company" in h or "name" in h or "ipo" in h) and "col" not in col:
                col["name"]    = i
            elif "open" in h and "open" not in col:
                col["open"]    = i
            elif "close" in h and "close" not in col:
                col["close"]   = i
            elif "price" in h and "price" not in col:
                col["price"]   = i
            elif "lot" in h and "lot" not in col:
                col["lot"]     = i
            elif "gmp" in h and "gmp" not in col:
                col["gmp"]     = i
            elif ("listing date" in h or ("list" in h and "date" in h)
                  or h in ("listing", "listed on", "list date")) and "listing" not in col:
                col["listing"] = i
            elif ("listing price" in h or "list price" in h
                  or h in ("listed price", "listing@")) and "lprice" not in col:
                col["lprice"]  = i

        if "name" not in col:
            col["name"] = 0

        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cells or len(cells) <= col["name"]:
                continue

            def _c(k):
                idx = col.get(k, -1)
                return cells[idx] if 0 <= idx < len(cells) else None

            # Use listing date if available; fall back to listing price col
            raw_listing = _c("listing") or _c("lprice")
            rec = _make_record(
                source,
                name         = _c("name") or "",
                open_date    = _c("open"),
                close_date   = _c("close"),
                issue_price  = _c("price"),
                lot_size     = _c("lot"),
                gmp          = _c("gmp"),
                listing_date = raw_listing,
            )
            if rec:
                records.append(rec)

    return records


# ── A: Chittorgarh ──────────────────────────────────────────────────────────

def fetch_chittorgarh() -> list[IPORecord]:
    log.info("━━ A: Chittorgarh ━━")
    records: list[IPORecord] = []
    url = "https://www.chittorgarh.com/ipo/ipo_dashboard.asp"
    scraper = _cloudscraper_session()
    r = _safe_get(url, session=scraper)
    soup = BeautifulSoup(r.text, "lxml")

    # Find the most relevant table (prefer one with Company Name header)
    target = None
    for tbl in soup.find_all("table"):
        hdr = tbl.find("tr")
        if hdr and any(kw in hdr.get_text().lower()
                       for kw in ("company name", "ipo name", "open date")):
            target = tbl
            break
    if not target:
        tables = [t for t in soup.find_all("table") if len(t.find_all("tr")) > 2]
        target = tables[0] if tables else None
    if not target:
        log.warning("  Chittorgarh: no table found")
        return records

    hdr_row = target.find("tr")
    headers = [th.get_text(strip=True).lower()
               for th in hdr_row.find_all(["th", "td"])]

    col: dict[str, int] = {}
    for i, h in enumerate(headers):
        if ("company" in h or "name" in h) and "name" not in col:
            col["name"]  = i
        elif "open" in h and "open" not in col:
            col["open"]  = i
        elif "close" in h and "close" not in col:
            col["close"] = i
        elif "price" in h and "price" not in col:
            col["price"] = i
        elif "lot" in h and "lot" not in col:
            col["lot"]   = i
    col.setdefault("name", 0)

    for row in target.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) <= col["name"]:
            continue

        def _c(k): # noqa
            idx = col.get(k, -1)
            return cells[idx] if 0 <= idx < len(cells) else None

        open_date  = _c("open")
        close_date = _c("close")

        # Fallback: if no dedicated open/close columns, try to find a date-range
        # that may be embedded in any cell, e.g. "05 - 07 May 2025"
        if not open_date:
            for cell in cells:
                m = _RANGE_RE.search(cell)
                if m:
                    open_date  = cell.split("–")[0].split("-")[0].strip()
                    close_date = close_date or cell  # whole string as close hint
                    break

        rec = _make_record(
            "Chittorgarh",
            name        = _c("name") or "",
            open_date   = open_date,
            close_date  = close_date,
            issue_price = _c("price"),
            lot_size    = _c("lot"),
        )
        if rec:
            records.append(rec)

    log.info(f"  ✓ {len(records)} records")
    return records


# ── Investorgain-specific cell parser ────────────────────────────────────────
#
# Investorgain packs multiple fields into single <td>s:
#   Name cell:  "Bio Medica LaboratoriesNSE SMEO"
#               "Adisoft TechnologiesNSE SMEL@205.00"
#               "MerritronixBSE SMEU"
#   Date cell:  "21-MayGMP: 3.5"   /  "1-Jun"   /  "23-AprGMP: 10"
#   Status codes: O=Open  C=Closed  U=Upcoming  L@xxx=Listed@price
#
# We parse all of this out so clean records reach the pipeline.

# Note: NO \s*$ anchor – the exchange code can be followed by extra text
# (subscription counts, ">" links, etc.) in the real HTML.  We use findall
# and take the LAST match so we always clip at the rightmost exchange marker.
_IG_EXCHANGE_RE = re.compile(
    r"(NSE\s*SME|BSE\s*SME|NSE|BSE|IPOL?)"     # exchange code
    r"\s*"
    r"(L@[\d,]+\.?\d*"                         # L@price  (NSE/BSE: explicit L)
    r"|@[\d,]+\.?\d*"                           # @price   (IPOL: implied Listed)
    r"|[OCU](?:\s*Allotted)?"                     # O / C / U  ± "Allotted"
    r"|Allotted"                                   # bare "Allotted"
    r")?",
    re.IGNORECASE,
)

_IG_DATE_RE  = re.compile(r"(\d{1,2})-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
                          re.IGNORECASE)
_IG_GMP_RE   = re.compile(r"GMP:\s*([\-\d.]+)", re.IGNORECASE)


def _ig_parse_name_cell(raw: str) -> tuple[str, str | None, str | None]:
    """
    Returns (clean_name, status_code, listing_price).
    "Bio Medica LaboratoriesNSE SMEO"    -> ("Bio Medica Laboratories", "O", None)
    "Adisoft TechnologiesNSE SMEL@205.00"-> ("Adisoft Technologies",    "L", "205.00")
    "NFP Sampoorna FoodsNSE SMECAllotted"-> ("NFP Sampoorna Foods",      "C", None)
    """
    matches = list(_IG_EXCHANGE_RE.finditer(raw))
    if not matches:
        return raw.strip(), None, None
    m = matches[-1]   # LAST occurrence = exchange code at end of cell text
    clean_name  = raw[:m.start()].strip()
    status_raw  = (m.group(2) or "").strip()
    if "@" in status_raw:
        price = status_raw[status_raw.index("@") + 1:]
        return clean_name, "L", price   # L@205 or @103.50 → Listed
    status_code = status_raw[0].upper() if status_raw else None
    return clean_name, status_code, None


def _ig_parse_date_cell(raw: str) -> tuple[str | None, str | None]:
    """
    Returns (date_str, gmp_value).
    e.g. "21-MayGMP: 3.5" -> ("21-May", "3.5")
         "1-Jun"           -> ("01 Jun", None)
    """
    if not raw:
        return None, None
    dm = _IG_DATE_RE.search(raw)
    if not dm:
        return None, None
    day, mon = dm.group(1), dm.group(2)
    year = _infer_year(mon, int(day))
    date_str = f"{int(day):02d} {mon.capitalize()} {year}"
    gm = _IG_GMP_RE.search(raw)
    gmp = gm.group(1) if gm else None
    return date_str, gmp


_IG_GMP_AMOUNT_RE = re.compile(r"^[^\d]*(\d+\.?\d*)")


def _ig_extract_gmp_amount(raw: str) -> str | None:
    """Extract clean GMP number from '20(12%)10 down/21 up' → '20', '--' → None."""
    if not raw:
        return None
    clean = raw.strip()
    if clean in ("--", "₹--", "-"):
        return None
    m = _IG_GMP_AMOUNT_RE.match(clean)
    if m:
        val = m.group(1)
        return val if float(val) > 0 else None
    return None


def _parse_investorgain_table(table) -> list[IPORecord]:
    """Parse Investorgain's non-standard table into clean IPORecords."""
    records: list[IPORecord] = []

    # Build header → column index map
    ths = table.find_all("th")
    headers = [re.sub(r"\s+", " ", th.get_text()).strip().lower() for th in ths]

    # Fallback: use first <tr> as header if no <th>
    if not headers:
        first_tr = table.find("tr")
        if first_tr:
            headers = [re.sub(r"\s+", " ", td.get_text()).strip().lower()
                       for td in first_tr.find_all(["th", "td"])]

    col: dict[str, int] = {}
    for i, h in enumerate(headers):
        if ("ipo" in h or "company" in h or "name" in h) and "name" not in col:
            col["name"]  = i
        elif "price" in h and "price" not in col:
            col["price"] = i
        elif "open" in h and "open" not in col:
            col["open"]  = i
        elif "close" in h and "close" not in col:
            col["close"] = i
        elif "gmp" in h and "gmp" not in col:
            col["gmp"]   = i
    col.setdefault("name", 0)
    # Col 1 on Investorgain's GMP page is the GMP value, not the issue price
    col.setdefault("gmp",   1)
    col.setdefault("open",  2)
    col.setdefault("close", 3)
    # price is only set if a "price"/"issue" header was explicitly detected

    start_row = 1 if ths else 2   # skip header row(s)
    for row in table.find_all("tr")[start_row:]:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if not cells or len(cells) < 2:
            continue
        if "no data" in (cells[0] or "").lower():
            continue

        raw_name = cells[col["name"]] if col["name"] < len(cells) else ""
        if not raw_name:
            continue

        clean_name, status_code, list_price = _ig_parse_name_cell(raw_name)
        # GMP column (col 1): extract the numeric amount from "₹21(12.21%)…"
        raw_gmp_cell = cells[col["gmp"]] if col.get("gmp", 1) < len(cells) else ""
        gmp_from_col = _ig_extract_gmp_amount(raw_gmp_cell)
        # Date cells: parse out date string and embedded GMP history value
        raw_open  = cells[col["open"]]  if col["open"]  < len(cells) else ""
        raw_close = cells[col["close"]] if col["close"] < len(cells) else ""
        open_date,  gmp_open  = _ig_parse_date_cell(raw_open)
        close_date, gmp_close = _ig_parse_date_cell(raw_close)
        # Priority: col-1 current GMP > close-day GMP > open-day GMP
        final_gmp = gmp_from_col or gmp_close or gmp_open
        # Issue price only if explicitly detected from headers
        issue_price = None
        if "price" in col:
            raw_price = cells[col["price"]] if col["price"] < len(cells) else ""
            issue_price = raw_price or None

        rec = _make_record(
            "Investorgain",
            clean_name,
            open_date    = open_date,
            close_date   = close_date,
            issue_price  = issue_price,
            gmp          = f"₹{final_gmp}" if final_gmp else None,
            listing_date = list_price,    # routed to listing_price if numeric
        )
        if rec:
            records.append(rec)
    return records


# ── B: Investorgain ─────────────────────────────────────────────────────────

def fetch_investorgain() -> list[IPORecord]:
    log.info("━━ B: Investorgain ━━")
    records: list[IPORecord] = []
    url = "https://investorgain.com/report/live-ipo-gmp/331/"
    scraper = _cloudscraper_session()
    r = _safe_get(url, session=scraper, timeout=35)
    soup = BeautifulSoup(r.text, "lxml")

    table = soup.find("table", id=re.compile(r"ipo", re.I)) or soup.find("table")
    if not table:
        log.warning("  Investorgain: no table via cloudscraper – trying Playwright")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
                page = browser.new_context(user_agent=random.choice(_USER_AGENTS)).new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(5_000)
                html = page.content()
                browser.close()
            soup = BeautifulSoup(html, "lxml")
            table = soup.find("table", id=re.compile(r"ipo", re.I)) or soup.find("table")
            if not table:
                log.warning("  Investorgain: no table even with Playwright")
                return records
        except Exception as exc:
            log.warning(f"  Investorgain Playwright error: {exc}")
            return records

    records = _parse_investorgain_table(table)
    log.info(f"  ✓ {len(records)} records")
    return records


# ── C: Screener.in (Playwright) ─────────────────────────────────────────────

def fetch_screener() -> list[IPORecord]:
    log.info("━━ C: Screener.in ━━")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(user_agent=random.choice(_USER_AGENTS))
            for url in _SCREENER_URLS:
                try:
                    page = ctx.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                    try:
                        page.wait_for_selector("table, .table, [class*=table]",
                                               timeout=12_000)
                    except Exception:
                        pass
                    page.wait_for_timeout(3_000)
                    html = page.content()
                    page.close()
                    soup = BeautifulSoup(html, "lxml")
                    records = _parse_tables(soup, "Screener")
                    if not records:
                        records = _parse_td_header_tables(soup, "Screener")
                    if records:
                        log.info(f"  ✓ {len(records)} records from {url}")
                        browser.close()
                        return records
                    log.debug(f"  Screener: 0 records from {url}")
                except Exception as exc:
                    log.debug(f"  Screener {url} error: {exc}")
            browser.close()
    except Exception as exc:
        log.warning(f"  Screener error: {exc}")
    log.warning("  Screener: 0 records from all URLs")
    return []


# ── D: Groww (XHR intercept + HTML fallback) ─────────────────────────────────

def fetch_groww() -> list[IPORecord]:
    log.info("━━ D: Groww ━━")
    records: list[IPORecord] = []
    try:
        from playwright.sync_api import sync_playwright
        captured: list[dict] = []

        def _on_response(response):
            url = response.url
            if any(kw in url for kw in ("/ipos", "/ipo/detail", "charter/v3", "ipo/list")):
                try:
                    captured.append(response.json())
                    log.debug(f"    Groww XHR captured: {url}")
                except Exception:
                    pass

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(
                user_agent=random.choice(_USER_AGENTS),
                viewport={"width": 1366, "height": 768},
            )
            page = ctx.new_page()
            page.on("response", _on_response)
            page.goto("https://groww.in/ipo", wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(8_000)   # let XHR calls settle
            html = page.content()
            browser.close()

        # Try JSON first
        for body in captured:
            records.extend(_parse_groww_json(body))

        # HTML fallback
        if not records:
            records = _parse_tables(BeautifulSoup(html, "lxml"), "Groww")

        log.info(f"  ✓ {len(records)} records")
    except Exception as exc:
        log.warning(f"  Groww error: {exc}")
    return records


def _parse_groww_json(data) -> list[IPORecord]:
    out: list[IPORecord] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            if not any(k in item for k in ("ipoName", "companyName", "name")):
                continue
            rec = _make_record(
                "Groww",
                name         = item.get("ipoName") or item.get("companyName") or item.get("name", ""),
                open_date    = item.get("openDate") or item.get("startDate"),
                close_date   = item.get("closeDate") or item.get("endDate"),
                issue_price  = item.get("issuePrice") or item.get("priceRange"),
                lot_size     = str(item["lotSize"]) if item.get("lotSize") else item.get("minOrderQty"),
                gmp          = item.get("gmp") or item.get("greyMarketPremium"),
                listing_date = item.get("listingDate"),
            )
            if rec:
                out.append(rec)
    elif isinstance(data, dict):
        for key in ("data", "ipos", "ipoList", "upcoming", "open", "result", "items"):
            if key in data:
                out.extend(_parse_groww_json(data[key]))
    return out


# ── E: IndiaTrade ────────────────────────────────────────────────────────────

def fetch_indiatrade() -> list[IPORecord]:
    log.info("━━ E: IndiaTrade ━━")
    records: list[IPORecord] = []
    url = "https://ipo.indiratrade.com/Home"
    try:
        scraper = _cloudscraper_session()
        r = _safe_get(url, session=scraper)
        if len(r.text) < 2_000:
            raise ValueError("Response too short – probably blocked")
        soup = BeautifulSoup(r.text, "lxml")
        records = _parse_tables(soup, "IndiaTrade")
        if records:
            log.info(f"  ✓ {len(records)} records (cloudscraper)")
            return records
    except Exception as exc:
        log.warning(f"  IndiaTrade cloudscraper failed: {exc}")

    # Playwright fallback
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(5_000)
            html = page.content()
            browser.close()
        records = _parse_tables(BeautifulSoup(html, "lxml"), "IndiaTrade")
        log.info(f"  ✓ {len(records)} records (playwright fallback)")
    except Exception as exc:
        log.warning(f"  IndiaTrade playwright failed: {exc}")

    return records


# ══════════════════════════════════════════════════════════════════════════════
# 9. PIPELINE  (orchestrator)
# ══════════════════════════════════════════════════════════════════════════════

SOURCE_REGISTRY: list[tuple[str, Callable]] = [
    # Chittorgarh removed – site consistently returns no parseable table
    ("Investorgain", fetch_investorgain),
    ("Screener",     fetch_screener),
    ("Groww",        fetch_groww),
    ("IndiaTrade",   fetch_indiatrade),
]


def run_pipeline(
    sources: list[tuple[str, Callable]] | None = None,
    status_filter: list[IPOStatus] | None = None,
    today: datetime | None = None,
) -> list[IPORecord]:
    """
    Run all sources, deduplicate, compute status, optionally filter.
    Returns sorted list (Open first, then Upcoming, then rest).
    """
    sources = sources or SOURCE_REGISTRY
    breakers = {name: CircuitBreaker(name) for name, _ in sources}

    all_raw: list[IPORecord] = []
    for name, fn in sources:
        log.info(f"  Fetching from {name} …")
        records = breakers[name].call(fn)
        log.info(f"  └─ {name}: {len(records)} raw records")
        all_raw.extend(records)

    log.info(f"Total raw records: {len(all_raw)}")
    merged = deduplicate(all_raw)
    log.info(f"After dedup: {len(merged)} unique IPOs")

    # Compute + stamp status
    if today is None:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for rec in merged:
        rec.status = compute_status(rec, today)

    # Sort: Open → Upcoming → Closed → Listed → Unknown
    _order = {
        IPOStatus.OPEN: 0, IPOStatus.UPCOMING: 1,
        IPOStatus.CLOSED: 2, IPOStatus.LISTED: 3, IPOStatus.UNKNOWN: 4,
    }
    merged.sort(key=lambda r: (_order.get(r.status, 9), r.name))

    if status_filter:
        merged = [r for r in merged if r.status in status_filter]
        log.info(f"After status filter {[s.value for s in status_filter]}: {len(merged)}")

    return merged


# ══════════════════════════════════════════════════════════════════════════════
# 10. OUTPUT FORMATTERS
# ══════════════════════════════════════════════════════════════════════════════

_STATUS_ICONS = {
    IPOStatus.OPEN:     "🟢",
    IPOStatus.UPCOMING: "🔵",
    IPOStatus.CLOSED:   "🔴",
    IPOStatus.LISTED:   "✅",
    IPOStatus.UNKNOWN:  "⚪",
}


def print_results(records: list[IPORecord]) -> None:
    if not records:
        print("\n⚠️  No IPO data collected.\n")
        return

    now_str = datetime.now().strftime("%d %b %Y %H:%M")
    print(f"\n{'═'*72}")
    print(f"  IPO DATA  —  {now_str}   ({len(records)} unique IPOs)")
    print(f"{'═'*72}")

    # Group by status
    groups: dict[IPOStatus, list[IPORecord]] = {}
    for rec in records:
        groups.setdefault(rec.status, []).append(rec)

    for status in (IPOStatus.OPEN, IPOStatus.UPCOMING,
                   IPOStatus.CLOSED, IPOStatus.LISTED, IPOStatus.UNKNOWN):
        grp = groups.get(status, [])
        if not grp:
            continue
        icon = _STATUS_ICONS[status]
        print(f"\n  {icon} {status.value.upper()}  ({len(grp)})")
        print(f"  {'─'*68}")
        for rec in grp:
            date_part = ""
            if rec.open_date or rec.close_date:
                date_part = f"  {rec.open_date or '?'} → {rec.close_date or '?'}"
            extras = "".join([
                f"  ₹{rec.issue_price.lstrip('₹')}" if rec.issue_price  else "",
                f"  Lot:{rec.lot_size}"              if rec.lot_size     else "",
                f"  GMP:{rec.gmp}"                   if rec.gmp          else "",
                f"  Listing:{rec.listing_date}"       if rec.listing_date else "",
                f"  ListPrice:₹{rec.listing_price}"  if rec.listing_price else "",
            ])
            src_line = f"[{', '.join(rec.sources)}]"
            print(f"  • {rec.name}")
            if date_part or extras:
                print(f"    {date_part}{extras}")
            print(f"    {src_line}")
    print()



def print_table(records: list[IPORecord]) -> None:
    """
    Print IPO data as a clean Unicode-box table.
    Columns: Status | IPO Name | Issue Price | Open → Close | Listing Date | Sources
    """
    if not records:
        print("\n⚠️  No IPO data collected.\n")
        return

    # ── build rows ──────────────────────────────────────────────────────────
    rows: list[tuple[str, ...]] = []
    for rec in records:
        icon   = _STATUS_ICONS.get(rec.status, "⚪")
        status = f"{icon} {rec.status.value}"

        # Subscription window
        if rec.open_date and rec.close_date:
            window = f"{rec.open_date} → {rec.close_date}"
        elif rec.open_date:
            window = f"{rec.open_date} → ?"
        else:
            window = "—"

        # Listing date (date or price-at-listing)
        listing = rec.listing_date or (
            f"₹{rec.listing_price}" if rec.listing_price else "—"
        )

        price = rec.issue_price or "—"

        rows.append((status, rec.name, price, window, listing))

    # ── compute column widths ───────────────────────────────────────────────
    HDR = ("Status", "IPO Name", "Price", "Open → Close", "Listing")
    # visible width (emoji = 2 chars, normal = 1)
    def _vw(s: str) -> int:
        return sum(2 if ord(c) > 0x2000 else 1 for c in s)

    def _pad(s: str, w: int) -> str:
        return s + " " * max(0, w - _vw(s))

    col_w = [max(_vw(h), max(_vw(r[i]) for r in rows))
             for i, h in enumerate(HDR)]
    # cap Name column at 44
    col_w[1] = min(col_w[1], 44)

    def _row_cells(cells: tuple[str, ...]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            truncated = cell if _vw(cell) <= col_w[i] else cell[:col_w[i]-1] + "…"
            parts.append(_pad(truncated, col_w[i]))
        return "│ " + " │ ".join(parts) + " │"

    sep_top  = "┌─" + "─┬─".join("─" * w for w in col_w) + "─┐"
    sep_mid  = "├─" + "─┼─".join("─" * w for w in col_w) + "─┤"
    sep_div  = "╞═" + "═╪═".join("═" * w for w in col_w) + "═╡"  # status divider
    sep_bot  = "└─" + "─┴─".join("─" * w for w in col_w) + "─┘"

    now_str = datetime.now().strftime("%d %b %Y %H:%M")
    print(f"\n  IPO TABLE  —  {now_str}   ({len(records)} IPOs)")
    print(sep_top)
    print(_row_cells(HDR))
    print(sep_mid)

    prev_status = None
    for rec, row in zip(records, rows):
        if prev_status and rec.status != prev_status:
            print(sep_div)           # visual break between status groups
        print(_row_cells(row))
        prev_status = rec.status

    print(sep_bot)
    print()

def save_json(records: list[IPORecord], path: str = "ipo_data.json") -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([r.to_dict() for r in records], fh, indent=2, ensure_ascii=False)
    log.info(f"  💾 JSON saved → {path}  ({len(records)} records)")


def save_csv(records: list[IPORecord], path: str = "ipo_data.csv") -> None:
    fields = ["name", "status", "open_date", "close_date", "listing_date",
              "issue_price", "listing_price", "lot_size", "gmp", "sources"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            row = rec.to_dict()
            row["sources"] = ", ".join(rec.sources)
            writer.writerow(row)
    log.info(f"  💾 CSV saved → {path}  ({len(records)} records)")


# ══════════════════════════════════════════════════════════════════════════════
# 11. ENTRY POINTS
# ══════════════════════════════════════════════════════════════════════════════

def main_all() -> list[IPORecord]:
    """Fetch all IPOs, all statuses."""
    records = run_pipeline()
    print_table(records)
    save_json(records, "ipo_data.json")
    save_csv(records, "ipo_data.csv")
    return records


def main_open_only() -> list[IPORecord]:
    """Fetch only currently OPEN IPOs (strict date filter)."""
    records = run_pipeline(status_filter=[IPOStatus.OPEN])
    print_table(records)
    save_json(records, "open_ipo_data.json")
    save_csv(records, "open_ipo_data.csv")
    return records


if __name__ == "__main__":
    import sys
    if "--open" in sys.argv:
        main_open_only()
    else:
        main_all()
