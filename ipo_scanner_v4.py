#!/usr/bin/env python3
"""
IPO Telegram Alert — Open IPOs Only with Institutional-Grade Strategy
════════════════════════════════════════════════════════════════════════════════
Architecture
  • CircuitBreaker per source (skip after 2 failures, pipeline never crashes)
  • Tenacity retry + jittered exponential back-off on every HTTP call
  • RapidFuzz token-sort deduplication across all sources
  • Year-aware multi-format date parser (ranges, partial, ISO)
  • Playwright stealth mode for GMP sources (replaces broken cloudscraper)
  • Telegram HTML mode (replaces broken MarkdownV2)

Sources
  A  Chittorgarh   – Playwright stealth (SSL bypass)
  B  Investorgain  – Playwright stealth (GMP data)
  C  NSE India     – 2-step cookie warmup + API + Playwright fallback
  D  Screener.in   – Playwright domcontentloaded
  E  Groww         – Playwright + XHR intercept
  F  IndiaTrade    – Playwright fallback

Strategy Signals (per OPEN IPO)
  ✅ BUY     → strong GMP (≥20%), low lot cost, QIB-favoured, Kelly > 0.3
  ⚠️  NEUTRAL → mixed/weak signals or no GMP data
  ❌ AVOID   → negative/zero GMP, overpriced lot, Kelly ≤ 0

Quant Models
  • GMP%       = (GMP / issue_price) × 100   — momentum proxy
  • Kelly f    = (p·b − q) / b               — optimal bet size
                 p = win prob (from GMP%), b = payoff ratio, q = 1−p
  • Monte Carlo = 10,000 simulations of listing-day gain/loss distribution
                  outputs P(profit), E[gain], P(loss>10%)
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import sys
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

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ipo_alert")


# ══════════════════════════════════════════════════════════════════════════════
# 1. DOMAIN MODELS
# ══════════════════════════════════════════════════════════════════════════════

class IPOStatus(str, Enum):
    OPEN     = "Open"
    UPCOMING = "Upcoming"
    CLOSED   = "Closed"
    LISTED   = "Listed"
    UNKNOWN  = "Unknown"


class BuySignal(str, Enum):
    BUY     = "BUY"
    NEUTRAL = "NEUTRAL"
    AVOID   = "AVOID"


@dataclass
class QuantMetrics:
    gmp_pct:        Optional[float] = None   # GMP as % of issue price
    kelly_f:        Optional[float] = None   # Kelly fraction (−1 to 1)
    mc_prob_profit: Optional[float] = None   # Monte Carlo P(listing gain > 0)
    mc_expected_gain: Optional[float] = None # E[gain %] from MC
    mc_prob_loss10: Optional[float] = None   # P(loss > 10%) from MC
    lot_cost:       Optional[float] = None   # total lot cost in ₹
    days_to_close:  Optional[int]   = None


@dataclass
class IPORecord:
    name:           str
    sources:        list[str]       = field(default_factory=list)
    open_date:      Optional[str]   = None
    close_date:     Optional[str]   = None
    listing_date:   Optional[str]   = None
    issue_price:    Optional[str]   = None
    lot_size:       Optional[str]   = None
    gmp:            Optional[str]   = None
    allotment_date: Optional[str]   = None
    listing_price:  Optional[str]   = None
    status:         IPOStatus       = IPOStatus.UNKNOWN
    signal:         BuySignal       = BuySignal.NEUTRAL
    signal_reason:  str             = ""
    quant:          QuantMetrics    = field(default_factory=QuantMetrics)
    _norm_key:      str             = field(default="", repr=False)

    def merge(self, other: "IPORecord") -> None:
        for src in other.sources:
            if src not in self.sources:
                self.sources.append(src)
        for attr in ("open_date", "close_date", "listing_date",
                     "issue_price", "lot_size", "gmp",
                     "allotment_date", "listing_price"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_norm_key", None)
        d["status"] = self.status.value
        d["signal"] = self.signal.value
        return d


# ══════════════════════════════════════════════════════════════════════════════
# 2. DATE PARSER
# ══════════════════════════════════════════════════════════════════════════════

_DATE_FORMATS = [
    "%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d-%m-%Y",
    "%d/%m/%Y", "%d %b", "%d %B", "%b %d %Y", "%B %d %Y", "%b %d, %Y",
]
_RANGE_RE = re.compile(
    r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?", re.IGNORECASE
)


def parse_date(raw: str | None) -> Optional[datetime]:
    if not raw:
        return None
    raw = raw.strip()
    if raw.lower() in ("tba", "to be announced", "n/a", "-", ""):
        return None
    m = _RANGE_RE.search(raw)
    if m:
        day, month_str = int(m.group(1)), m.group(3)
        year = int(m.group(4)) if m.group(4) else _infer_year(month_str, day)
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(f"{day} {month_str} {year}", fmt)
            except ValueError:
                continue
        return None
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=_infer_year(dt.strftime("%b"), dt.day))
            return dt
        except ValueError:
            continue
    return None


def _infer_year(month_str: str, day: int) -> int:
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


# ══════════════════════════════════════════════════════════════════════════════
# 3. STATUS COMPUTER
# ══════════════════════════════════════════════════════════════════════════════

def compute_status(rec: IPORecord, today: Optional[datetime] = None) -> IPOStatus:
    if today is None:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    open_dt    = parse_date(rec.open_date)
    close_dt   = parse_date(rec.close_date)
    listing_dt = parse_date(rec.listing_date)
    if listing_dt and listing_dt < today:
        return IPOStatus.LISTED
    if close_dt and close_dt < today:
        return IPOStatus.CLOSED
    if open_dt and open_dt <= today and (not close_dt or close_dt >= today):
        return IPOStatus.OPEN
    if open_dt and open_dt > today:
        return IPOStatus.UPCOMING
    if listing_dt and listing_dt > today:
        return IPOStatus.UPCOMING
    _no_dates = not open_dt and not close_dt and not listing_dt
    if _no_dates and rec.listing_price:
        return IPOStatus.LISTED
    if _no_dates and rec.issue_price and rec.issue_price.strip("₹ -") and not rec.gmp:
        return IPOStatus.LISTED
    return IPOStatus.UNKNOWN


# ══════════════════════════════════════════════════════════════════════════════
# 4. QUANT ENGINE  — GMP%, Kelly Criterion, Monte Carlo
# ══════════════════════════════════════════════════════════════════════════════

def _parse_numeric(s: str | None) -> Optional[float]:
    if not s:
        return None
    # strip currency, brackets, percent, take first number
    clean = re.sub(r"[₹%,\s]", "", s.split("(")[0].split("/")[0])
    # handle negative GMP like "-25" or "−25"
    clean = clean.replace("−", "-")
    m = re.search(r"-?\d+\.?\d*", clean)
    try:
        return float(m.group()) if m else None
    except (ValueError, AttributeError):
        return None


def _parse_price_range(price_str: str | None) -> tuple[Optional[float], Optional[float]]:
    """Return (low, high) from '₹326 - ₹343' or '₹52' etc."""
    if not price_str:
        return None, None
    nums = re.findall(r"[\d,]+\.?\d*", price_str.replace(",", ""))
    vals = [float(n) for n in nums if n]
    if not vals:
        return None, None
    return vals[0], vals[-1]


def monte_carlo(
    issue_price: float,
    gmp: float,
    n_simulations: int = 10_000,
    seed: int = 42,
) -> tuple[float, float, float]:
    """
    Simulate listing-day price distribution.

    Model:
      listing_price = issue_price + GMP + noise
      noise ~ Normal(0, σ)  where σ = max(issue_price * 0.08, abs(gmp) * 0.5)
      (captures listing-day volatility around GMP anchor)

    Returns: (P(profit), E[gain%], P(loss > 10%))
    """
    random.seed(seed)
    sigma = max(issue_price * 0.08, abs(gmp) * 0.5, 1.0)
    profits = 0
    gains: list[float] = []
    loss10 = 0
    for _ in range(n_simulations):
        noise = random.gauss(0, sigma)
        listing = issue_price + gmp + noise
        gain_pct = (listing - issue_price) / issue_price * 100
        gains.append(gain_pct)
        if gain_pct > 0:
            profits += 1
        if gain_pct < -10:
            loss10 += 1
    return (
        profits / n_simulations,
        sum(gains) / len(gains),
        loss10 / n_simulations,
    )


def kelly_criterion(
    prob_win: float,
    payoff_ratio: float,   # b = expected gain / expected loss (odds)
) -> float:
    """
    Kelly fraction f* = (p·b − q) / b
    Clamped to [−1, 1].  Positive = bet, negative = fade.
    """
    if payoff_ratio <= 0:
        return -1.0
    q = 1.0 - prob_win
    f = (prob_win * payoff_ratio - q) / payoff_ratio
    return max(-1.0, min(1.0, f))


def compute_quant(rec: IPORecord) -> QuantMetrics:
    q = QuantMetrics()

    _, price_hi = _parse_price_range(rec.issue_price)
    price_lo, _ = _parse_price_range(rec.issue_price)
    issue_price  = price_hi or price_lo  # use upper band (allotment price)

    gmp_val = _parse_numeric(rec.gmp)
    lot_val = _parse_numeric(rec.lot_size)

    # Lot cost
    if lot_val and issue_price:
        q.lot_cost = lot_val * issue_price

    # GMP %
    if gmp_val is not None and issue_price and issue_price > 0:
        q.gmp_pct = (gmp_val / issue_price) * 100

    # Days to close
    close_dt = parse_date(rec.close_date)
    if close_dt:
        q.days_to_close = (
            close_dt - datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        ).days

    # Monte Carlo + Kelly (only if we have GMP + issue price)
    if gmp_val is not None and issue_price and issue_price > 0:
        p_profit, e_gain, p_loss10 = monte_carlo(issue_price, gmp_val)
        q.mc_prob_profit  = p_profit
        q.mc_expected_gain = e_gain
        q.mc_prob_loss10  = p_loss10

        # Kelly: payoff = expected gain / expected loss
        # Simplified: use GMP% as expected gain if positive
        if gmp_val > 0 and q.gmp_pct:
            payoff_ratio = q.gmp_pct / max(10.0, 100 - q.gmp_pct)  # gain% / loss%
            q.kelly_f = kelly_criterion(p_profit, payoff_ratio)
        else:
            q.kelly_f = kelly_criterion(p_profit, 1.0)

    return q


# ══════════════════════════════════════════════════════════════════════════════
# 5. BUY / AVOID STRATEGY ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def compute_signal(rec: IPORecord) -> tuple[BuySignal, str, QuantMetrics]:
    q = compute_quant(rec)
    reasons: list[str] = []
    pos = 0
    neg = 0

    # ── GMP % momentum ───────────────────────────────────────────────────────
    if q.gmp_pct is not None:
        gmp_val = _parse_numeric(rec.gmp) or 0
        if q.gmp_pct >= 30:
            reasons.append(f"GMP {q.gmp_pct:.1f}% — very strong demand")
            pos += 3
        elif q.gmp_pct >= 15:
            reasons.append(f"GMP {q.gmp_pct:.1f}% — good listing expected")
            pos += 2
        elif q.gmp_pct >= 5:
            reasons.append(f"GMP {q.gmp_pct:.1f}% — mild premium")
            pos += 1
        elif q.gmp_pct >= 0:
            reasons.append(f"GMP {q.gmp_pct:.1f}% — flat/weak demand")
            neg += 1
        else:
            reasons.append(f"GMP {q.gmp_pct:.1f}% — negative, avoid")
            neg += 3
    else:
        reasons.append("GMP unavailable — estimate only")

    # ── Monte Carlo ───────────────────────────────────────────────────────────
    if q.mc_prob_profit is not None:
        pp = q.mc_prob_profit * 100
        eg = q.mc_expected_gain or 0
        pl = (q.mc_prob_loss10 or 0) * 100
        reasons.append(
            f"MC(10k sims): P(profit)={pp:.0f}%  E[gain]={eg:+.1f}%  P(loss>10%)={pl:.0f}%"
        )
        if pp >= 70:
            pos += 2
        elif pp >= 50:
            pos += 1
        elif pp < 40:
            neg += 2

    # ── Kelly Criterion ───────────────────────────────────────────────────────
    if q.kelly_f is not None:
        k = q.kelly_f
        if k >= 0.4:
            reasons.append(f"Kelly f={k:.2f} → size up (strong edge)")
            pos += 2
        elif k >= 0.15:
            reasons.append(f"Kelly f={k:.2f} → small position (modest edge)")
            pos += 1
        elif k >= 0:
            reasons.append(f"Kelly f={k:.2f} → minimal edge, caution")
        else:
            reasons.append(f"Kelly f={k:.2f} → negative edge, avoid")
            neg += 2

    # ── Price band spread ─────────────────────────────────────────────────────
    if rec.issue_price:
        lo, hi = _parse_price_range(rec.issue_price)
        if lo and hi and lo > 0:
            spread = ((hi - lo) / lo) * 100
            if spread <= 3:
                reasons.append(f"Tight band ₹{lo:.0f}–₹{hi:.0f} ({spread:.1f}%) — confident pricing")
                pos += 1
            elif spread > 8:
                reasons.append(f"Wide band ₹{lo:.0f}–₹{hi:.0f} ({spread:.1f}%) — uncertain pricing")
                neg += 1

    # ── Lot cost (accessibility) ──────────────────────────────────────────────
    if q.lot_cost:
        if q.lot_cost <= 15_000:
            reasons.append(f"Lot cost ≈₹{q.lot_cost:,.0f} — retail friendly")
            pos += 1
        elif q.lot_cost >= 3_00_000:
            reasons.append(f"Lot cost ≈₹{q.lot_cost:,.0f} — HNI bracket, low allotment odds")
            neg += 1

    # ── Closing urgency ───────────────────────────────────────────────────────
    if q.days_to_close is not None:
        if q.days_to_close == 0:
            reasons.append("Closes TODAY — last chance to apply")
        elif q.days_to_close == 1:
            reasons.append("Closes tomorrow")
        elif q.days_to_close > 4:
            reasons.append(f"{q.days_to_close} days left — no urgency")

    # ── Verdict ───────────────────────────────────────────────────────────────
    if pos >= 4 and pos > neg + 1:
        signal = BuySignal.BUY
    elif neg >= 3 and neg > pos + 1:
        signal = BuySignal.AVOID
    else:
        signal = BuySignal.NEUTRAL

    return signal, " | ".join(reasons), q


# ══════════════════════════════════════════════════════════════════════════════
# 6. NAME NORMALISER & DEDUPLICATOR
# ══════════════════════════════════════════════════════════════════════════════

_NOISE_RE = re.compile(
    r"\b(limited|ltd|pvt|private|public|co\.?|inc|corp"
    r"|sme\s*ipo|\(sme\s*ipo\)|\(sme\)|sme"
    r"|india|ventures?|enterprise[s]?|solutions?|services?|technologies?|tech)\b",
    re.IGNORECASE,
)
_FUZZY_THRESHOLD = 88


def normalise_name(name: str) -> str:
    n = name.lower().strip()
    n = _NOISE_RE.sub(" ", n)
    n = re.sub(r"[^\w\s]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _dates_conflict(a: IPORecord, b: IPORecord) -> bool:
    if not a.open_date or not b.open_date:
        return False
    oa, ob = parse_date(a.open_date), parse_date(b.open_date)
    return bool(oa and ob and abs((oa - ob).days) > 3)


def _same_ipo(a: IPORecord, b: IPORecord) -> bool:
    if not a._norm_key or not b._norm_key:
        return False
    if a._norm_key == b._norm_key:
        return not _dates_conflict(a, b)
    la, lb = len(a._norm_key), len(b._norm_key)
    if la < 10 or lb < 10 or min(la, lb) / max(la, lb) < 0.75:
        return False
    da = {t for t in a._norm_key.split() if t.isdigit()}
    db = {t for t in b._norm_key.split() if t.isdigit()}
    if da and db and da != db:
        return False
    return fuzz.token_sort_ratio(a._norm_key, b._norm_key) >= _FUZZY_THRESHOLD \
        and not _dates_conflict(a, b)


def _field_count(rec: IPORecord) -> int:
    return sum(1 for f in (rec.open_date, rec.close_date, rec.listing_date,
                           rec.issue_price, rec.lot_size, rec.gmp, rec.listing_price) if f)


def deduplicate(records: list[IPORecord]) -> list[IPORecord]:
    seen: dict[tuple, IPORecord] = {}
    for rec in records:
        key = (rec.sources[0] if rec.sources else "?", rec._norm_key)
        if key not in seen or _field_count(rec) > _field_count(seen[key]):
            seen[key] = rec
    merged: list[IPORecord] = []
    for rec in seen.values():
        placed = False
        for ex in merged:
            if _same_ipo(ex, rec):
                ex.merge(rec)
                placed = True
                break
        if not placed:
            merged.append(rec)
    return merged


# ══════════════════════════════════════════════════════════════════════════════
# 7. HTTP / PLAYWRIGHT HELPERS
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
JSON_HEADERS = {**BASE_HEADERS, "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest"}

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-IN','en']});
window.chrome = {runtime: {}};
"""


def _headers() -> dict:
    return {**BASE_HEADERS, "User-Agent": random.choice(_USER_AGENTS)}


@retry(
    retry=retry_if_exception_type((requests.RequestException,)),
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


def _pw_get_html(url: str, wait_selector: str = "table",
                 wait_ms: int = 5000, intercept_patterns: list[str] | None = None
                 ) -> tuple[str, list[dict]]:
    """
    Launch a stealth Playwright browser, return (html, captured_json_responses).
    intercept_patterns: list of URL substrings to capture as JSON.
    """
    from playwright.sync_api import sync_playwright
    captured: list[dict] = []

    def _on_response(response):
        if intercept_patterns:
            url_lower = response.url.lower()
            if any(p in url_lower for p in intercept_patterns):
                ct = response.headers.get("content-type", "")
                if "json" in ct and response.status == 200:
                    try:
                        captured.append(response.json())
                    except Exception:
                        pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled",
                  "--ignore-certificate-errors"],   # fixes Chittorgarh SSL
        )
        ctx = browser.new_context(
            user_agent=random.choice(_USER_AGENTS),
            locale="en-IN",
            viewport={"width": 1366, "height": 768},
            ignore_https_errors=True,              # fixes SSL cert issues
        )
        page = ctx.new_page()
        page.add_init_script(_STEALTH_JS)
        if intercept_patterns:
            page.on("response", _on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=10_000)
                except Exception:
                    pass
            page.wait_for_timeout(wait_ms)
        except Exception as e:
            log.debug(f"  PW nav warning ({url[:40]}…): {e}")
        html = page.content()
        browser.close()
    return html, captured


# ══════════════════════════════════════════════════════════════════════════════
# 8. CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CircuitBreaker:
    name:         str
    max_failures: int  = 2
    _failures:    int  = field(default=0, init=False)
    _open:        bool = field(default=False, init=False)

    def call(self, fn: Callable) -> list[IPORecord]:
        if self._open:
            log.warning(f"  ⚡ Circuit OPEN – skipping {self.name}")
            return []
        try:
            result = fn()
            self._failures = 0
            return result
        except Exception as exc:
            self._failures += 1
            log.warning(f"  ✗ {self.name} failure #{self._failures}: {exc}")
            if self._failures >= self.max_failures:
                self._open = True
                log.error(f"  ⚡ Circuit TRIPPED – {self.name} skipped")
            return []


# ══════════════════════════════════════════════════════════════════════════════
# 9. RAW ROW → IPORecord
# ══════════════════════════════════════════════════════════════════════════════

_PURE_PRICE_RE = re.compile(r"^[₹\s]*[\d,]+\.?\d*\s*$")


def _is_price_string(s: str | None) -> bool:
    return bool(s and _PURE_PRICE_RE.match(s.strip().replace(",", "")))


def _clean_name(raw: str) -> str:
    if not raw:
        return ""
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.sub(r"\s*\([^)]*\)\s*$", "", raw, flags=re.DOTALL).strip()
    raw = re.sub(r"\d{1,2}\s*[-–]\s*\d{1,2}\s+[A-Za-z]+(\s+\d{4})?$", "", raw).strip()
    return raw


def _clean_price(raw: str | None) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip().lstrip("₹Rs. ")
    return f"₹{raw}" if raw else None


def _make_record(source: str, name: str, **kwargs) -> Optional[IPORecord]:
    name = _clean_name(name)
    if not name or len(name) < 3 or _is_price_string(name):
        return None
    rec             = IPORecord(name=name, sources=[source])
    rec.open_date   = kwargs.get("open_date") or None
    rec.close_date  = kwargs.get("close_date") or None
    rec.issue_price = _clean_price(kwargs.get("issue_price"))
    rec.lot_size    = kwargs.get("lot_size") or None
    rec.gmp         = kwargs.get("gmp") or None
    rec._norm_key   = normalise_name(name)
    raw_listing     = kwargs.get("listing_date") or None
    if raw_listing:
        if _is_price_string(raw_listing):
            rec.listing_price = raw_listing
        elif parse_date(raw_listing) is not None:
            rec.listing_date = raw_listing
    return rec


# ══════════════════════════════════════════════════════════════════════════════
# 10. GENERIC TABLE PARSER
# ══════════════════════════════════════════════════════════════════════════════

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
            if ("company" in h or "name" in h or "ipo" in h) and "name" not in col:
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
            elif ("listing date" in h or ("list" in h and "date" in h)) and "listing" not in col:
                col["listing"] = i
            elif ("listing price" in h or "list price" in h) and "lprice" not in col:
                col["lprice"]  = i
        col.setdefault("name", 0)
        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cells or len(cells) <= col["name"]:
                continue
            def _c(k):
                idx = col.get(k, -1)
                return cells[idx] if 0 <= idx < len(cells) else None
            rec = _make_record(
                source, _c("name") or "",
                open_date=_c("open"), close_date=_c("close"),
                issue_price=_c("price"), lot_size=_c("lot"),
                gmp=_c("gmp"), listing_date=_c("listing") or _c("lprice"),
            )
            if rec:
                records.append(rec)
    return records


# ══════════════════════════════════════════════════════════════════════════════
# 11. SOURCE PARSERS
# ══════════════════════════════════════════════════════════════════════════════

# ── A: Chittorgarh (Playwright stealth — bypasses SSL issue + anti-bot) ──────

def fetch_chittorgarh() -> list[IPORecord]:
    log.info("━━ A: Chittorgarh (Playwright stealth) ━━")
    html, _ = _pw_get_html(
        "https://www.chittorgarh.com/ipo/ipo_dashboard.asp",
        wait_selector="table", wait_ms=3000,
    )
    soup = BeautifulSoup(html, "lxml")

    # Find table with IPO-like header
    target = None
    for tbl in soup.find_all("table"):
        hdr_text = (tbl.find("tr") or tbl).get_text().lower()
        if any(kw in hdr_text for kw in ("company name", "ipo name", "open date", "issue price")):
            target = tbl
            break
    if not target:
        tables = [t for t in soup.find_all("table") if len(t.find_all("tr")) > 2]
        target = tables[0] if tables else None
    if not target:
        log.warning("  Chittorgarh: no table found")
        return []

    hdr_row = target.find("tr")
    headers = [th.get_text(strip=True).lower() for th in hdr_row.find_all(["th", "td"])]
    col: dict[str, int] = {}
    for i, h in enumerate(headers):
        if ("company" in h or "name" in h) and "name" not in col: col["name"]  = i
        elif "open" in h and "open" not in col:                     col["open"]  = i
        elif "close" in h and "close" not in col:                   col["close"] = i
        elif "price" in h and "price" not in col:                   col["price"] = i
        elif "lot" in h and "lot" not in col:                       col["lot"]   = i
        elif "gmp" in h and "gmp" not in col:                       col["gmp"]   = i
    col.setdefault("name", 0)

    records: list[IPORecord] = []
    for row in target.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) <= col["name"]: continue
        def _c(k):
            idx = col.get(k, -1)
            return cells[idx] if 0 <= idx < len(cells) else None
        rec = _make_record(
            "Chittorgarh", _c("name") or "",
            open_date=_c("open"), close_date=_c("close"),
            issue_price=_c("price"), lot_size=_c("lot"), gmp=_c("gmp"),
        )
        if rec:
            records.append(rec)

    log.info(f"  ✓ {len(records)} records")
    return records


# ── B: Investorgain (Playwright stealth — GMP source) ────────────────────────

def fetch_investorgain() -> list[IPORecord]:
    log.info("━━ B: Investorgain (Playwright stealth) ━━")
    html, _ = _pw_get_html(
        "https://investorgain.com/report/live-ipo-gmp/331/",
        wait_selector="table", wait_ms=4000,
    )
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id=re.compile(r"ipo", re.I)) or soup.find("table")
    if not table:
        log.warning("  Investorgain: no table found")
        return []

    headers = [re.sub(r"\s+", " ", th.get_text()).strip().lower()
               for th in table.find_all("th")]
    records: list[IPORecord] = []
    for row in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if not cells or "no data" in cells[0].lower():
            continue
        kwargs: dict = {}
        for i, h in enumerate(headers):
            if i >= len(cells): break
            if "open" in h:   kwargs["open_date"]   = cells[i]
            elif "close" in h: kwargs["close_date"]  = cells[i]
            elif "gmp" in h:   kwargs["gmp"]         = cells[i]
            elif "price" in h: kwargs["issue_price"] = cells[i]
        rec = _make_record("Investorgain", cells[0], **kwargs)
        if rec:
            records.append(rec)

    log.info(f"  ✓ {len(records)} records")
    return records


# ── C: NSE India (cookie warmup + API + Playwright fallback) ─────────────────

def fetch_nse() -> list[IPORecord]:
    log.info("━━ C: NSE India ━━")
    session = requests.Session()
    ua = random.choice(_USER_AGENTS)
    warmup_h = {**BASE_HEADERS, "User-Agent": ua,
                "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1"}
    records: list[IPORecord] = []
    try:
        session.get("https://www.nseindia.com", headers=warmup_h, timeout=15)
        time.sleep(1.5)
        session.get("https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
                    headers={**warmup_h, "Referer": "https://www.nseindia.com",
                             "Sec-Fetch-Site": "same-origin"}, timeout=15)
        time.sleep(1.5)
        api_h = {**JSON_HEADERS, "User-Agent": ua,
                 "Referer": "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
                 "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                 "Sec-Fetch-Site": "same-origin"}
        for ep in ["https://www.nseindia.com/api/ipo-current-allotment",
                   "https://www.nseindia.com/api/getIpoData?category=ipo"]:
            try:
                r = session.get(ep, headers=api_h, timeout=12)
                if r.status_code == 200 and r.text.strip():
                    records = _parse_nse_json(r.json())
                    if records:
                        log.info(f"  ✓ NSE: {len(records)} records")
                        return records
            except Exception as e:
                log.debug(f"  NSE endpoint: {e}")
    except Exception as e:
        log.warning(f"  NSE HTTP: {e}")

    # Playwright fallback
    try:
        html, captured = _pw_get_html(
            "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
            wait_selector=None, wait_ms=6000,
            intercept_patterns=["nseindia.com/api"],
        )
        for body in captured:
            records.extend(_parse_nse_json(body))
        if not records:
            records = _parse_tables(BeautifulSoup(html, "lxml"), "NSE")
    except Exception as e:
        log.warning(f"  NSE Playwright: {e}")

    log.info(f"  ✓ NSE: {len(records)} records")
    return records


def _parse_nse_json(data) -> list[IPORecord]:
    records: list[IPORecord] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict): continue
            rec = _make_record(
                "NSE",
                name        = item.get("companyName", item.get("symbol", "")),
                open_date   = item.get("openDate", item.get("bidStartDate", "")),
                close_date  = item.get("closeDate", item.get("bidEndDate", "")),
                issue_price = item.get("issuePrice", item.get("price", "")),
                listing_date= item.get("listingDate", ""),
            )
            if rec: records.append(rec)
    elif isinstance(data, dict):
        for key in ["data", "ipoData", "upcomingIPO", "currentIPO", "allIpo"]:
            if key in data:
                return _parse_nse_json(data[key])
    return records


# ── D: Screener.in ───────────────────────────────────────────────────────────

def fetch_screener() -> list[IPORecord]:
    log.info("━━ D: Screener.in ━━")
    html, _ = _pw_get_html("https://www.screener.in/ipo/recent/",
                           wait_selector="table", wait_ms=3000)
    records = _parse_tables(BeautifulSoup(html, "lxml"), "Screener")
    log.info(f"  ✓ {len(records)} records")
    return records


# ── E: Groww (XHR intercept) ─────────────────────────────────────────────────

def fetch_groww() -> list[IPORecord]:
    log.info("━━ E: Groww ━━")
    html, captured = _pw_get_html(
        "https://groww.in/ipo", wait_selector=None, wait_ms=8000,
        intercept_patterns=["/ipos", "/ipo/detail", "charter/v3", "ipo/list"],
    )
    records: list[IPORecord] = []
    for body in captured:
        records.extend(_parse_groww_json(body))
    if not records:
        records = _parse_tables(BeautifulSoup(html, "lxml"), "Groww")
    log.info(f"  ✓ {len(records)} records")
    return records


def _parse_groww_json(data) -> list[IPORecord]:
    out: list[IPORecord] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict): continue
            if not any(k in item for k in ("ipoName", "companyName", "name")): continue
            rec = _make_record(
                "Groww",
                name        = item.get("ipoName") or item.get("companyName") or item.get("name", ""),
                open_date   = item.get("openDate") or item.get("startDate"),
                close_date  = item.get("closeDate") or item.get("endDate"),
                issue_price = item.get("issuePrice") or item.get("priceRange"),
                lot_size    = str(item["lotSize"]) if item.get("lotSize") else item.get("minOrderQty"),
                gmp         = item.get("gmp") or item.get("greyMarketPremium"),
                listing_date= item.get("listingDate"),
            )
            if rec: out.append(rec)
    elif isinstance(data, dict):
        for key in ("data", "ipos", "ipoList", "upcoming", "open", "result", "items"):
            if key in data:
                out.extend(_parse_groww_json(data[key]))
    return out


# ── F: IndiaTrade ─────────────────────────────────────────────────────────────

def fetch_indiatrade() -> list[IPORecord]:
    log.info("━━ F: IndiaTrade ━━")
    html, _ = _pw_get_html("https://ipo.indiratrade.com/Home",
                           wait_selector="table", wait_ms=5000)
    records = _parse_tables(BeautifulSoup(html, "lxml"), "IndiaTrade")
    log.info(f"  ✓ {len(records)} records")
    return records


# ══════════════════════════════════════════════════════════════════════════════
# 12. PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

SOURCE_REGISTRY: list[tuple[str, Callable]] = [
    ("Chittorgarh",  fetch_chittorgarh),
    ("Investorgain", fetch_investorgain),
    ("NSE",          fetch_nse),
    ("Screener",     fetch_screener),
    ("Groww",        fetch_groww),
    ("IndiaTrade",   fetch_indiatrade),
]


def run_pipeline(today: datetime | None = None) -> list[IPORecord]:
    breakers = {name: CircuitBreaker(name) for name, _ in SOURCE_REGISTRY}
    all_raw: list[IPORecord] = []
    for name, fn in SOURCE_REGISTRY:
        records = breakers[name].call(fn)
        log.info(f"  └─ {name}: {len(records)} raw records")
        all_raw.extend(records)

    log.info(f"Total raw: {len(all_raw)}  →  deduplicating…")
    merged = deduplicate(all_raw)
    log.info(f"After dedup: {len(merged)} unique IPOs")

    if today is None:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for rec in merged:
        rec.status = compute_status(rec, today)

    open_ipos = [r for r in merged if r.status == IPOStatus.OPEN]
    log.info(f"Currently OPEN: {len(open_ipos)}")

    for rec in open_ipos:
        rec.signal, rec.signal_reason, rec.quant = compute_signal(rec)

    _order = {BuySignal.BUY: 0, BuySignal.NEUTRAL: 1, BuySignal.AVOID: 2}
    open_ipos.sort(key=lambda r: _order.get(r.signal, 9))
    return open_ipos


# ══════════════════════════════════════════════════════════════════════════════
# 13. TELEGRAM SENDER  (HTML mode — no escaping nightmares)
# ══════════════════════════════════════════════════════════════════════════════

_SIGNAL_BADGE = {
    BuySignal.BUY:     "✅ <b>BUY</b>",
    BuySignal.NEUTRAL: "⚠️ <b>NEUTRAL</b>",
    BuySignal.AVOID:   "❌ <b>AVOID</b>",
}


def _h(s: str) -> str:
    """Escape string for Telegram HTML."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_telegram_message(records: list[IPORecord]) -> str:
    now_str = datetime.now().strftime("%d %b %Y %H:%M")
    lines = [
        f"📊 <b>OPEN IPOs — {now_str}</b>",
        f"<i>{len(records)} IPO(s) currently accepting subscriptions</i>",
        "",
    ]
    if not records:
        lines.append("⚠️ No open IPOs found right now.")
        return "\n".join(lines)

    for rec in records:
        badge = _SIGNAL_BADGE.get(rec.signal, "⚪ <b>NEUTRAL</b>")
        q     = rec.quant

        lines.append(f"{badge}  <b>{_h(rec.name)}</b>")

        details: list[str] = []
        if rec.issue_price:
            details.append(f"💰 {_h(rec.issue_price)}")
        if rec.open_date and rec.close_date:
            details.append(f"📅 {_h(rec.open_date)} → {_h(rec.close_date)}")
        elif rec.close_date:
            details.append(f"📅 Closes {_h(rec.close_date)}")
        if rec.lot_size:
            details.append(f"📦 Lot {_h(rec.lot_size)}")
        if rec.gmp:
            details.append(f"📈 GMP {_h(rec.gmp)}")
        if rec.listing_date:
            details.append(f"🗓 Lists {_h(rec.listing_date)}")
        if details:
            lines.append("  " + "  |  ".join(details))

        # Quant block
        q_parts: list[str] = []
        if q.gmp_pct is not None:
            q_parts.append(f"GMP% {q.gmp_pct:+.1f}%")
        if q.kelly_f is not None:
            q_parts.append(f"Kelly {q.kelly_f:.2f}")
        if q.mc_prob_profit is not None:
            q_parts.append(f"MC P(profit) {q.mc_prob_profit*100:.0f}%")
        if q.mc_expected_gain is not None:
            q_parts.append(f"E[gain] {q.mc_expected_gain:+.1f}%")
        if q.lot_cost:
            q_parts.append(f"Lot ≈₹{q.lot_cost:,.0f}")
        if q_parts:
            lines.append(f"  <code>{_h(' | '.join(q_parts))}</code>")

        # Signal reason (split by | for readability)
        reasons = rec.signal_reason.split(" | ")
        for r in reasons:
            lines.append(f"  • <i>{_h(r.strip())}</i>")

        lines.append(f"  [<i>{_h(', '.join(rec.sources))}</i>]")
        lines.append("")

    lines.append("<i>Signals: GMP% + Kelly Criterion + Monte Carlo (10k sims)</i>")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Telegram HTML has a 4096 char limit per message — split if needed
    chunks = _split_message(text, 4000)
    success = True
    for chunk in chunks:
        payload = {
            "chat_id":                  chat_id,
            "text":                     chunk,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
            log.info(f"  ✅ Telegram chunk sent (msg_id={r.json().get('result',{}).get('message_id')})")
        except Exception as e:
            log.error(f"  ❌ Telegram send failed: {e}")
            log.error(f"  Response body: {r.text[:300] if 'r' in dir() else 'n/a'}")
            success = False
    return success


def _split_message(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


# ══════════════════════════════════════════════════════════════════════════════
# 14. CONSOLE OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

def print_results(records: list[IPORecord]) -> None:
    if not records:
        print("\n⚠️  No OPEN IPOs found.\n")
        return
    print(f"\n{'═'*72}")
    print(f"  OPEN IPOs — {datetime.now().strftime('%d %b %Y %H:%M')}  ({len(records)} open)")
    print(f"{'═'*72}")
    for rec in records:
        icon = {"BUY": "✅", "NEUTRAL": "⚠️ ", "AVOID": "❌"}.get(rec.signal.value, "⚪")
        print(f"\n  {icon} {rec.signal.value}  •  {rec.name}")
        q = rec.quant
        quant_line = "  ".join(filter(None, [
            f"GMP%={q.gmp_pct:+.1f}%" if q.gmp_pct is not None else None,
            f"Kelly={q.kelly_f:.2f}"  if q.kelly_f is not None else None,
            f"MC_P={q.mc_prob_profit*100:.0f}%" if q.mc_prob_profit is not None else None,
            f"E[g]={q.mc_expected_gain:+.1f}%" if q.mc_expected_gain is not None else None,
            f"Lot≈₹{q.lot_cost:,.0f}" if q.lot_cost else None,
        ]))
        if quant_line:
            print(f"    [{quant_line}]")
        date_str = ""
        if rec.open_date and rec.close_date:
            date_str = f"{rec.open_date} → {rec.close_date}  "
        extras = (f"{rec.issue_price or ''}  Lot:{rec.lot_size or '?'}  "
                  f"GMP:{rec.gmp or 'n/a'}  Lists:{rec.listing_date or '?'}")
        print(f"    {date_str}{extras}")
        for reason in rec.signal_reason.split(" | "):
            print(f"    • {reason.strip()}")
        print(f"    [{', '.join(rec.sources)}]")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# 15. ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="IPO Telegram Alert — Open IPOs + Quant Strategy")
    parser.add_argument("--token",   default=None, help="Telegram Bot Token")
    parser.add_argument("--chat",    default=None, help="Telegram Chat/Channel ID")
    parser.add_argument("--dry-run", action="store_true", help="Print without sending")
    parser.add_argument("--json",    default="open_ipos.json", help="JSON output path")
    args = parser.parse_args()

    token   = args.token   or os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = args.chat    or os.environ.get("TELEGRAM_CHAT_ID")

    if not args.dry_run and (not token or not chat_id):
        log.error(
            "❌ Credentials missing.\n"
            "   CLI:     --token TOKEN --chat CHAT_ID\n"
            "   Env:     TELEGRAM_TOKEN + TELEGRAM_CHAT_ID"
        )
        sys.exit(1)

    log.info("🚀 Starting IPO pipeline…")
    open_ipos = run_pipeline()
    print_results(open_ipos)

    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump([r.to_dict() for r in open_ipos], fh, indent=2, ensure_ascii=False)
    log.info(f"  💾 JSON saved → {args.json}")

    msg = _build_telegram_message(open_ipos)

    if args.dry_run:
        print("\n── DRY RUN — Telegram HTML preview ─────────────────────────────────")
        print(msg)
        print("─────────────────────────────────────────────────────────────────────\n")
    else:
        log.info("📤 Sending to Telegram…")
        send_telegram(token, chat_id, msg)


if __name__ == "__main__":
    main()
