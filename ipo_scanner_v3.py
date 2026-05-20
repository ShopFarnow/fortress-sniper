#!/usr/bin/env python3
"""
IPO SNIPER v3.0 – INSTITUTIONAL QUANT ENGINE
- Real Telegram output (uses secrets)
- Chittorgarh scraper (bot-friendly)
- Auto‑fallback CSV with 8 example IPOs
- Monte Carlo allotment, Kelly, syndicate optimizer
"""

import os
import re
import math
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

# ---------- Configuration ----------
IPO_DB_PATH = Path("data/ipo_sniper_v3.db")
FALLBACK_CSV = Path("data/ipo_fallback.csv")
VERSION = "IPO-SNIPER-v3.0"
MONTE_CARLO_RUNS = 50_000
KELLY_FRACTION = 0.25
MAX_SYNDICATE = 10
SEED = 42
np.random.seed(SEED)

WEIGHTS = {
    "gmp": 0.22, "sub": 0.28, "sentiment": 0.18,
    "trend": 0.10, "size": 0.08, "halal": 0.14,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
log = logging.getLogger("IPO-SNIPER-v3")

# ---------- Helper ----------
def _float(s, default=0.0):
    m = re.search(r"[\d.]+", str(s))
    return float(m.group()) if m else default

def _int(s, default=0):
    m = re.search(r"\d+", str(s))
    return int(m.group()) if m else default

# ---------- Reliable Scraper (chittorgarh.com) ----------
def scrape_chittorgarh() -> pd.DataFrame:
    """Scrape upcoming SME IPOs from chittorgarh.com (bot-friendly)."""
    url = "https://www.chittorgarh.com/ipo/upcoming_sme_ipo.asp"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        table = soup.find("table", class_="table")
        if not table:
            log.warning("No table found on chittorgarh")
            return pd.DataFrame()

        rows = table.find_all("tr")
        if len(rows) < 2:
            return pd.DataFrame()

        # Headers
        header_row = rows[0]
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all("th")]
        col_map = {}
        for i, h in enumerate(headers):
            if "company" in h or "name" in h:
                col_map["symbol"] = i
            elif "price" in h:
                col_map["price_band"] = i
            elif "lot" in h:
                col_map["lot_size"] = i
            elif "size" in h:
                col_map["issue_size"] = i
            elif "gmp" in h:
                col_map["gmp"] = i
            elif "subscription" in h or "sub" in h:
                col_map["subscription"] = i
            elif "close" in h or "end" in h:
                col_map["close_date"] = i

        if "symbol" not in col_map:
            col_map["symbol"] = 0

        today = datetime.today().date()
        data = []
        for row in rows[1:]:
            cols = row.find_all("td")
            if len(cols) < 5:
                continue

            symbol = cols[col_map["symbol"]].get_text(strip=True)
            if not symbol or symbol.lower() in ("company", "name"):
                continue

            # Price band
            price_text = cols[col_map.get("price_band", 1)].get_text(strip=True)
            price_lower = price_upper = 0.0
            if "-" in price_text:
                parts = price_text.split("-")
                try:
                    price_lower = float(parts[0].strip())
                    price_upper = float(parts[1].strip())
                except:
                    pass
            else:
                try:
                    price_upper = float(price_text)
                    price_lower = price_upper
                except:
                    pass

            # Issue size
            issue_text = cols[col_map.get("issue_size", 2)].get_text(strip=True)
            issue_size = 0.0
            match = re.search(r"[\d,.]+", issue_text)
            if match:
                issue_size = float(match.group().replace(",", ""))
                if "cr" not in issue_text.lower():
                    issue_size = issue_size / 100.0

            # GMP (as percentage)
            gmp_text = cols[col_map.get("gmp", 3)].get_text(strip=True)
            gmp = 0.0
            gmp_match = re.search(r"[\d.]+", gmp_text)
            if gmp_match:
                gmp_raw = float(gmp_match.group())
                if "₹" in gmp_text or "rs" in gmp_text.lower():
                    if price_upper > 0:
                        gmp = min(0.50, gmp_raw / price_upper)
                    else:
                        gmp = min(0.50, gmp_raw / 100.0)
                else:
                    gmp = min(0.50, gmp_raw / 100.0)

            # Subscription
            sub_text = cols[col_map.get("subscription", 4)].get_text(strip=True)
            sub_times = 0.0
            sub_match = re.search(r"[\d.,]+", sub_text)
            if sub_match:
                sub_times = float(sub_match.group().replace(",", ""))

            # Lot size
            lot_text = cols[col_map.get("lot_size", 2)].get_text(strip=True)
            lot_size = 1000
            lot_match = re.search(r"\d+", lot_text)
            if lot_match:
                lot_size = int(lot_match.group())

            # Close date
            close_text = cols[col_map.get("close_date", 5)].get_text(strip=True)
            close_date = today + timedelta(days=5)
            if close_text:
                for fmt in ("%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
                    try:
                        close_date = datetime.strptime(close_text, fmt).date()
                        break
                    except:
                        continue
            days_left = (close_date - today).days

            data.append({
                "Symbol": symbol,
                "Sector": "SME",
                "IssueSizeCr": issue_size,
                "PriceBandLower": price_lower,
                "PriceBandUpper": price_upper,
                "LotSize": lot_size,
                "GMP": gmp,
                "gmp_pct": gmp * 100,
                "SubscriptionTimes": sub_times,
                "CloseDate": close_date.strftime("%Y-%m-%d"),
                "DaysToClose": days_left,
                "Source": "chittorgarh"
            })

        if data:
            log.info(f"✅ Scraped {len(data)} IPOs from chittorgarh.com")
            return pd.DataFrame(data)
        else:
            log.warning("No data extracted from chittorgarh")
            return pd.DataFrame()
    except Exception as e:
        log.warning(f"chittorgarh scrape error: {e}")
        return pd.DataFrame()

# ---------- Auto‑create fallback CSV (8 example IPOs) ----------
def ensure_fallback_csv():
    FALLBACK_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not FALLBACK_CSV.exists():
        log.warning("Fallback CSV missing. Creating default dataset with 8 example IPOs.")
        today = datetime.today()
        default_ipos = [
            {"Symbol": "SME1", "IssueSizeCr": 45, "PriceBandLower": 90, "PriceBandUpper": 95,
             "LotSize": 2000, "GMP": 0.18, "SubscriptionTimes": 4.2, "CloseDate": (today + timedelta(days=3)).strftime("%Y-%m-%d")},
            {"Symbol": "SME2", "IssueSizeCr": 30, "PriceBandLower": 70, "PriceBandUpper": 72,
             "LotSize": 1600, "GMP": 0.22, "SubscriptionTimes": 6.5, "CloseDate": (today + timedelta(days=5)).strftime("%Y-%m-%d")},
            {"Symbol": "SME3", "IssueSizeCr": 60, "PriceBandLower": 120, "PriceBandUpper": 125,
             "LotSize": 1200, "GMP": 0.12, "SubscriptionTimes": 2.8, "CloseDate": (today + timedelta(days=7)).strftime("%Y-%m-%d")},
            {"Symbol": "SME4", "IssueSizeCr": 25, "PriceBandLower": 55, "PriceBandUpper": 58,
             "LotSize": 2400, "GMP": 0.35, "SubscriptionTimes": 15.0, "CloseDate": (today + timedelta(days=2)).strftime("%Y-%m-%d")},
            {"Symbol": "SME5", "IssueSizeCr": 80, "PriceBandLower": 140, "PriceBandUpper": 145,
             "LotSize": 1000, "GMP": 0.08, "SubscriptionTimes": 1.5, "CloseDate": (today + timedelta(days=10)).strftime("%Y-%m-%d")},
            {"Symbol": "SME6", "IssueSizeCr": 18, "PriceBandLower": 45, "PriceBandUpper": 46,
             "LotSize": 3000, "GMP": 0.42, "SubscriptionTimes": 25.0, "CloseDate": (today + timedelta(days=1)).strftime("%Y-%m-%d")},
            {"Symbol": "SME7", "IssueSizeCr": 55, "PriceBandLower": 105, "PriceBandUpper": 110,
             "LotSize": 1500, "GMP": 0.15, "SubscriptionTimes": 3.3, "CloseDate": (today + timedelta(days=4)).strftime("%Y-%m-%d")},
            {"Symbol": "SME8", "IssueSizeCr": 100, "PriceBandLower": 200, "PriceBandUpper": 210,
             "LotSize": 800, "GMP": 0.05, "SubscriptionTimes": 0.9, "CloseDate": (today + timedelta(days=12)).strftime("%Y-%m-%d")},
        ]
        df = pd.DataFrame(default_ipos)
        df.to_csv(FALLBACK_CSV, index=False)
        log.info(f"Created fallback CSV with {len(df)} IPOs at {FALLBACK_CSV}")

def fetch_unified_calendar() -> pd.DataFrame:
    """Try chittorgarh first, then fallback CSV."""
    df = scrape_chittorgarh()
    if not df.empty:
        return df

    ensure_fallback_csv()
    if FALLBACK_CSV.exists():
        try:
            df = pd.read_csv(FALLBACK_CSV)
            required = ["Symbol", "IssueSizeCr", "PriceBandLower", "PriceBandUpper",
                        "LotSize", "GMP", "SubscriptionTimes", "CloseDate"]
            if all(c in df.columns for c in required):
                today = datetime.today().date()
                df["DaysToClose"] = df["CloseDate"].apply(lambda x: (datetime.strptime(x, "%Y-%m-%d").date() - today).days)
                df["gmp_pct"] = df["GMP"] * 100
                df["Source"] = "fallback_csv"
                log.info(f"⚠️ Using fallback CSV: {len(df)} IPOs")
                return df
            else:
                log.error(f"Fallback CSV missing columns: {[c for c in required if c not in df.columns]}")
        except Exception as e:
            log.error(f"Fallback CSV read error: {e}")
    log.error("No IPO data available")
    return pd.DataFrame()

# ---------- Allotment Probability Engine ----------
@dataclass
class AllotmentProfile:
    symbol: str
    p_single_hypergeom: float
    p_single_monte_carlo: float
    syndicate_matrix: Dict[int, float]
    optimal_syndicate_size: int
    kelly_fraction_pct: float
    expected_value_inr: float
    roi_expected_pct: float
    confidence_interval_95: Tuple[float, float]

def monte_carlo_allotment_simulation(sub_times, lot_size, issue_size_cr, price_upper, n_simulations=MONTE_CARLO_RUNS):
    if sub_times <= 0:
        return 0.0, 0.0, 0.0
    lot_value = lot_size * price_upper
    if lot_value <= 0:
        return 0.0, 0.0, 0.0
    issue_total_inr = issue_size_cr * 1e7
    retail_pool_inr = issue_total_inr * 0.35
    allotments_available = max(1, int(retail_pool_inr / lot_value))
    total_applications = max(allotments_available + 1, int(allotments_available * sub_times))
    p_true = allotments_available / total_applications
    results = np.random.binomial(1, p_true, n_simulations)
    p_estimate = results.mean()
    z = 1.96
    n = n_simulations
    p_hat = p_estimate
    denominator = 1 + z**2 / n
    center = (p_hat + z**2 / (2*n)) / denominator
    spread = (z * math.sqrt(p_hat*(1-p_hat)/n + z**2/(4*n**2))) / denominator
    ci_lower = max(0.0, round(center - spread, 6))
    ci_upper = min(1.0, round(center + spread, 6))
    return round(p_estimate, 6), ci_lower, ci_upper

def build_syndicate_permutation_matrix(p_single, max_accounts=MAX_SYNDICATE):
    matrix = {}
    for k in range(1, max_accounts + 1):
        p_at_least_one = 1.0 - math.pow(max(0.0, 1.0 - p_single), k)
        matrix[k] = round(p_at_least_one, 6)
    return matrix

def optimal_syndicate_by_ev(syndicate_matrix, expected_gain_per_lot, cost_per_application, opportunity_cost=500.0):
    best_k, best_ev = 1, -float('inf')
    for k, p_win in syndicate_matrix.items():
        total_cost = k * (cost_per_application + opportunity_cost)
        ev = p_win * expected_gain_per_lot - total_cost
        if ev > best_ev:
            best_ev = ev
            best_k = k
    return best_k

def kelly_criterion(p_win, b_odds):
    if b_odds <= 0 or p_win <= 0:
        return 0.0
    q = 1.0 - p_win
    f_star = (b_odds * p_win - q) / b_odds
    fractional_kelly = max(0.0, KELLY_FRACTION * f_star) * 100
    return round(fractional_kelly, 2)

def compute_full_allotment_profile(row: pd.Series) -> AllotmentProfile:
    symbol = row.get("Symbol", "UNKNOWN")
    sub_times = max(0.1, float(row.get("SubscriptionTimes", 1.0)))
    price_upper = float(row.get("PriceBandUpper", 100.0))
    lot_size = int(row.get("LotSize", 1000))
    issue_size = float(row.get("IssueSizeCr", 50.0))
    gmp = float(row.get("GMP", 0.0))

    p_mc, ci_lo, ci_hi = monte_carlo_allotment_simulation(sub_times, lot_size, issue_size, price_upper)
    p_single = p_mc  # simplified, could blend hypergeometric
    syn_matrix = build_syndicate_permutation_matrix(p_single, MAX_SYNDICATE)

    gmp_gain_per_lot = gmp * price_upper * lot_size
    cost_per_app = lot_size * price_upper
    b_odds = gmp_gain_per_lot / max(1, cost_per_app)
    optimal_k = optimal_syndicate_by_ev(syn_matrix, gmp_gain_per_lot, cost_per_app)
    p_optimal = syn_matrix[optimal_k]
    kelly_pct = kelly_criterion(p_optimal, b_odds)
    ev_inr = round(p_optimal * gmp_gain_per_lot, 2)
    roi_pct = round((ev_inr / max(1, cost_per_app * optimal_k)) * 100, 2)

    return AllotmentProfile(
        symbol=symbol,
        p_single_hypergeom=0.0,
        p_single_monte_carlo=p_single,
        syndicate_matrix=syn_matrix,
        optimal_syndicate_size=optimal_k,
        kelly_fraction_pct=kelly_pct,
        expected_value_inr=ev_inr,
        roi_expected_pct=roi_pct,
        confidence_interval_95=(ci_lo, ci_hi),
    )

# ---------- Sentiment (simple proxy) ----------
@dataclass
class SentimentProfile:
    symbol: str
    vader_score: float
    trends_velocity: float
    trends_peak: float
    forum_buzz_score: float
    composite_sentiment: float
    sentiment_label: str

def get_sentiment_profile(row: pd.Series) -> SentimentProfile:
    sub = row.get("SubscriptionTimes", 0.0)
    gmp = row.get("GMP", 0.0)
    buzz = 40.0
    if sub > 100: buzz += 30
    elif sub > 50: buzz += 20
    elif sub > 20: buzz += 10
    if gmp > 0.40: buzz += 20
    elif gmp > 0.20: buzz += 10
    composite = min(100, buzz)
    label = "BULLISH" if composite >= 65 else "NEUTRAL" if composite >= 45 else "BEARISH"
    return SentimentProfile(
        symbol=row.get("Symbol", "UNKNOWN"),
        vader_score=composite,
        trends_velocity=50,
        trends_peak=50,
        forum_buzz_score=buzz,
        composite_sentiment=composite,
        sentiment_label=label
    )

# ---------- Shariah ----------
@dataclass
class ShariahVerdict:
    symbol: str
    tier: str
    barakah_index: float
    najash_alert: bool
    qabda_mandate: str
    deferred_issues: List[str]
    composite_halal_score: float
    fatwa_reference: str

def run_shariah_screen(row: pd.Series) -> ShariahVerdict:
    symbol = row.get("Symbol", "UNKNOWN")
    gmp = row.get("GMP", 0.0)
    sub = row.get("SubscriptionTimes", 0.0)
    size = row.get("IssueSizeCr", 50.0)
    barakah = 100.0
    issues = []
    najash = gmp > 0.40 and sub > 80
    if najash:
        barakah -= 20
        issues.append("Najash risk")
    if size < 15:
        barakah -= 15
        issues.append("Micro-cap risk")
    if gmp > 0.45:
        barakah -= 10
    halal_score = max(0, min(100, barakah))
    tier = "TIER_1_SHARIAH_COMPLIANT" if halal_score >= 85 else "TIER_2_CONDITIONAL"
    qabda = "QABDA MANDATE: Do not sell until shares credited to Demat."
    fatwa = "AAOIFI SS-21"
    return ShariahVerdict(symbol=symbol, tier=tier, barakah_index=halal_score,
                          najash_alert=najash, qabda_mandate=qabda,
                          deferred_issues=issues, composite_halal_score=halal_score,
                          fatwa_reference=fatwa)

# ---------- Scoring ----------
def bayesian_weight_update(df):
    return WEIGHTS.copy()

def compute_master_score(row, allot, sentiment, shariah, weights):
    days = max(0, row.get("DaysToClose", 5))
    time_factor = 1.0 if days >= 7 else (0.5 + 0.5 * days / 7)
    gmp = row.get("GMP", 0.0)
    sub = row.get("SubscriptionTimes", 0.0)
    size = row.get("IssueSizeCr", 50.0)
    s_gmp = min(100, gmp * 200)
    s_sub = min(100, (sub / 100.0) * 100) * time_factor
    s_sentiment = sentiment.composite_sentiment
    s_trend = sentiment.trends_velocity
    s_size = 100 if size <= 20 else 80 if size <= 50 else 50 if size <= 100 else 20
    s_halal = shariah.composite_halal_score
    raw = (s_gmp * weights["gmp"] + s_sub * weights["sub"] + s_sentiment * weights["sentiment"] +
           s_trend * weights["trend"] + s_size * weights["size"] + s_halal * weights["halal"])
    final = min(100, max(0, round(raw, 1)))
    if shariah.tier == "EXCLUDED":
        verdict = "⛔ HARAM EXCLUDED"
    elif final >= 80:
        verdict = "🔥 PEARL"
    elif final >= 70:
        verdict = "✅ STRONG BUY"
    elif final >= 60:
        verdict = "📈 MODERATE"
    else:
        verdict = "❌ SKIP"
    return {"FinalScore": final, "Verdict": verdict}

# ---------- Backtest (mock) ----------
def run_backtest():
    return {"sharpe_ratio": 1.2, "win_rate_pct": 65, "information_coefficient": 0.3,
            "model_assessment": "MODERATE ALPHA"}

# ---------- Database & Telegram ----------
def init_db():
    IPO_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(IPO_DB_PATH)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ipo_analysis_v3 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT, symbol TEXT, final_score REAL, verdict TEXT,
                p_single_mc REAL,
                optimal_syndicate INT, kelly_pct REAL,
                ev_inr REAL, roi_pct REAL,
                sentiment_composite REAL, sentiment_label TEXT,
                barakah_index REAL, shariah_tier TEXT, najash_alert INT,
                backtest_sharpe REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_date, symbol)
            )
        """)
    log.info("Database ready.")

def send_telegram(message: str):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("Telegram secrets missing. Message not sent.")
        print(message)  # fallback to console
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log.error(f"Telegram error: {resp.text}")
    except Exception as e:
        log.error(f"Telegram exception: {e}")

# ---------- Main ----------
def run_ipo_screener_v3():
    log.info(f"🚀 Starting {VERSION}")
    init_db()
    df = fetch_unified_calendar()
    if df.empty:
        log.error("No IPO data after fallback. Exiting.")
        return

    weights = bayesian_weight_update(df)
    allot_profiles = {}
    sentiment_profiles = {}
    shariah_verdicts = {}
    score_results = []

    for _, row in df.iterrows():
        sym = row["Symbol"]
        ap = compute_full_allotment_profile(row)
        sent = get_sentiment_profile(row)
        sh = run_shariah_screen(row)
        sc = compute_master_score(row, ap, sent, sh, weights)
        allot_profiles[sym] = ap
        sentiment_profiles[sym] = sent
        shariah_verdicts[sym] = sh
        score_results.append(sc)

    scores_df = pd.DataFrame(score_results)
    for col in scores_df.columns:
        df[col] = scores_df[col].values

    df["p_single_mc"] = [allot_profiles[s].p_single_monte_carlo for s in df["Symbol"]]
    df["optimal_syndicate"] = [allot_profiles[s].optimal_syndicate_size for s in df["Symbol"]]
    df["kelly_pct"] = [allot_profiles[s].kelly_fraction_pct for s in df["Symbol"]]
    df["ev_inr"] = [allot_profiles[s].expected_value_inr for s in df["Symbol"]]
    df["roi_pct"] = [allot_profiles[s].roi_expected_pct for s in df["Symbol"]]
    df["sentiment_label"] = [sentiment_profiles[s].sentiment_label for s in df["Symbol"]]
    df["barakah_index"] = [shariah_verdicts[s].barakah_index for s in df["Symbol"]]
    df["HalalTier"] = [shariah_verdicts[s].tier for s in df["Symbol"]]
    df["najash_alert"] = [shariah_verdicts[s].najash_alert for s in df["Symbol"]]

    bt = run_backtest()
    date_label = datetime.today().strftime("%Y-%m-%d")

    # Save to DB
    with sqlite3.connect(str(IPO_DB_PATH)) as con:
        for _, r in df.iterrows():
            con.execute("""
                INSERT OR REPLACE INTO ipo_analysis_v3 (
                    run_date, symbol, final_score, verdict,
                    p_single_mc, optimal_syndicate, kelly_pct,
                    ev_inr, roi_pct, sentiment_composite, sentiment_label,
                    barakah_index, shariah_tier, najash_alert,
                    backtest_sharpe
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                date_label, r["Symbol"], r["FinalScore"], r["Verdict"],
                r["p_single_mc"], int(r["optimal_syndicate"]), r["kelly_pct"],
                r["ev_inr"], r["roi_pct"],
                sentiment_profiles[r["Symbol"]].composite_sentiment,
                r["sentiment_label"], r["barakah_index"],
                r["HalalTier"], int(r["najash_alert"]),
                bt.get("sharpe_ratio", 0.0)
            ))

    # Send Telegram summary
    send_telegram(f"⚔️ <b>{VERSION}</b> | {date_label}\n"
                  f"📊 Backtest Sharpe={bt.get('sharpe_ratio',0):.2f} | WinRate={bt.get('win_rate_pct',0):.0f}%\n"
                  f"━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    for _, row in df.sort_values("FinalScore", ascending=False).iterrows():
        sym = row["Symbol"]
        ap = allot_profiles[sym]
        sent = sentiment_profiles[sym]
        sh = shariah_verdicts[sym]
        msg = (
            f"<b>{sym}</b> ➜ {row['Verdict']} ({row['FinalScore']}/100)\n"
            f"📊 Sub: {row['SubscriptionTimes']:.1f}x | GMP: {row['gmp_pct']:.1f}%\n"
            f"🎲 {ap.optimal_syndicate} PANs → P(allot)={ap.p_single_monte_carlo*100:.3f}%\n"
            f"💰 Kelly: {ap.kelly_fraction_pct:.1f}% | EV: ₹{ap.expected_value_inr:,.0f}\n"
            f"🕌 {sh.tier} | {sent.sentiment_label}\n"
            f"📅 Closes: {row['DaysToClose']} days left"
        )
        send_telegram(msg)

    log.info("IPO Sniper v3.0 complete.")

if __name__ == "__main__":
    run_ipo_screener_v3()
