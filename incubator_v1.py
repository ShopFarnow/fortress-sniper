#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║   PROJECT FORTRESS — INCUBATOR v4.0 (THE INSIDER SYNDICATE)                ║
║   Bismillah — In the name of Allah, the Most Gracious, the Most Merciful   ║
║                                                                              ║
║   ARCHITECTURE PIVOT: Weinstein 200MA replaced by Insider/Filing LLM Audit ║
║   NSE BYPASS: curl_cffi TLS impersonation (inherited from fortress_fetcher)║
║                                                                              ║
║   THE INSIDER FUNNEL:                                                        ║
║   1. The Rubble Gate : Price is near 52W low (cheap) + Sponge Volume (buy) ║
║   2. The Data Heist  : Fetch SAST Insider Trades & Corporate Filings       ║
║   3. The Insider LLM : Audits filings for Stealth Capex & Promoter Buying  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, json, re, time, random, logging
from datetime import datetime, timedelta
import pandas as pd

# The NSE Bypass Armor
from curl_cffi import requests as cffi_requests
import requests # standard fallback

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("incubator_v4")

# CONFIG
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
OPENAI_MINI_MODEL  = os.getenv("OPENAI_MINI_MODEL", "gpt-4o-mini")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
GOOGLE_SHEET_ID    = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_CREDS_JSON  = os.getenv("GOOGLE_CREDS_JSON", "")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — NSE BYPASS (CURL_CFFI)
# ══════════════════════════════════════════════════════════════════════════════

_NSE_SESSION = None

def _get_nse_session():
    """Bypasses NSE Cloudflare using TLS browser impersonation."""
    global _NSE_SESSION
    if _NSE_SESSION: return _NSE_SESSION
    
    log.info("Booting curl_cffi Chrome impersonation for NSE bypass...")
    sess = cffi_requests.Session(impersonate="chrome110")
    sess.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.nseindia.com/",
    })
    
    try:
        r = sess.get("https://www.nseindia.com", timeout=15)
        log.info(f"NSE Handshake: HTTP {r.status_code}")
        time.sleep(1)
        r2 = sess.get("https://www.nseindia.com/api/allIndices", timeout=15)
        log.info(f"NSE API Unlock: HTTP {r2.status_code}")
        _NSE_SESSION = sess
    except Exception as e:
        log.error(f"NSE Bypass Failed: {e}")
    return sess

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — THE MATH GATES (Rubble & Sponge)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_weekly_history(symbol: str, weeks=52) -> pd.DataFrame:
    try:
        import yfinance as yf
        df = yf.download(f"{symbol}.NS", period="2y", interval="1wk", progress=False)
        if not df.empty:
            df = df.reset_index()
            df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
            df = df.rename(columns={"datetime":"date"})
            return df.tail(weeks).reset_index(drop=True)
    except: pass
    return pd.DataFrame()

def check_rubble_and_sponge(weekly: pd.DataFrame, current_price: float) -> tuple[bool, dict]:
    """
    Finds stocks ignored by the public (near 52w low) but quietly bought by whales (Sponge).
    """
    if len(weekly) < 20: return False, {}
    
    high_52w = float(weekly['high'].max())
    low_52w = float(weekly['low'].min())
    
    # Must be at least 25% down from highs (The Rubble)
    if current_price > high_52w * 0.75:
        return False, {"reason": "Price too close to highs (Not Rubble)"}
        
    # Sponge Volume Analysis (Institutions buying the dip)
    close_w = weekly["close"].values
    vol_w   = weekly["volume"].values
    avg_vol = float(vol_w[-20:].mean())
    
    if avg_vol == 0: return False, {}
    
    red_mask   = close_w[1:] < close_w[:-1]
    green_mask = close_w[1:] >= close_w[:-1]
    
    dry_up_weeks = int((vol_w[1:][red_mask] < avg_vol * 0.60).sum())
    sponge_weeks = int((vol_w[1:][green_mask] > avg_vol * 1.50).sum())
    
    if dry_up_weeks >= 1 and sponge_weeks >= 1:
        return True, {"sponge_weeks": sponge_weeks, "dry_up_weeks": dry_up_weeks, "discount": round((1 - current_price/high_52w)*100, 1)}
        
    return False, {"reason": "No institutional sponge volume detected"}

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — THE DATA HEIST (NSE Insider & Filings via cffi)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_insider_and_filings(symbol: str):
    sess = _get_nse_session()
    filings_text = "No recent filings."
    insider_text = "No insider trades."
    
    if not sess: return filings_text, insider_text

    # 1. Fetch Corporate Announcements
    try:
        url = f"https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={symbol}"
        r = sess.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            filings = []
            for item in data[:5]: # Last 5 filings
                filings.append(f"Date: {item.get('an_dt', '')} | Subject: {item.get('subject', '')} | Detail: {item.get('desc', '')}")
            if filings: filings_text = "\n".join(filings)
    except Exception as e: log.debug(f"Filings fetch error for {symbol}: {e}")

    # 2. Fetch Insider Trading (SAST)
    try:
        url = f"https://www.nseindia.com/api/corporates-pit?index=equities&symbol={symbol}"
        r = sess.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", [])
            trades = []
            for item in data[:5]:
                if "Buy" in str(item.get("acqMode", "")) or "Market Purchase" in str(item.get("acqMode", "")):
                    trades.append(f"Person: {item.get('personName','')} | Bought: {item.get('secAcq','')} shares | Value: {item.get('secVal','')} | Mode: {item.get('acqMode','')}")
            if trades: insider_text = "\n".join(trades)
    except Exception as e: log.debug(f"Insider fetch error for {symbol}: {e}")

    return filings_text, insider_text

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — THE INSIDER LLM AUDIT
# ══════════════════════════════════════════════════════════════════════════════

def insider_friend_audit(symbol: str, filings: str, insiders: str) -> dict:
    if not OPENAI_API_KEY: return {"is_pearl": False, "reason": "No API Key"}
    
    prompt = f"""You are an insider friend at a top quantitative hedge fund. 
I am looking at {symbol}. It is trading near its 52-week low, ignored by the public, but our volume models show institutions are quietly buying it.

Here is the raw data extracted from the National Stock Exchange for {symbol}:

RECENT CORPORATE FILINGS:
{filings}

RECENT INSIDER PROMOTER BUYING:
{insiders}

Task: Determine if there is a "Stealth Catalyst" here 3 months before the breakout.
Look for:
1. Promoters/Founders buying their own stock from the open market.
2. Capacity expansion, new factories, land acquisition, or massive order wins in the filings.

Respond ONLY in this JSON format:
{{
  "stealth_catalyst_found": true/false,
  "insider_buying_found": true/false,
  "insider_summary": "1 sentence explanation of what you found",
  "confidence_score": 1 to 100
}}"""

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": OPENAI_MINI_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
            timeout=20
        )
        raw = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(re.sub(r"```json|```", "", raw).strip())
        return parsed
    except Exception as e:
        log.error(f"LLM Audit failed for {symbol}: {e}")
        return {"stealth_catalyst_found": False, "insider_buying_found": False, "insider_summary": "LLM Error", "confidence_score": 0}

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — MAIN SYNDICATE LOOP
# ══════════════════════════════════════════════════════════════════════════════

def _send_tg(text: str):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})

def run():
    log.info("Booting INSIDER SYNDICATE v4.0...")
    
    # 1. Fetch Universe (YFinance fallback for speed in this example)
    import yfinance as yf
    nifty500 = ["MANINFRA.NS", "CHAMBLFERT.NS", "PRICOLLTD.NS", "ZOMATO.NS", "RELIANCE.NS"] # Replace with full DB load
    
    survivors = []
    
    # PHASE 1: The Math Sweep (Fast)
    for sym in nifty500:
        clean_sym = sym.replace(".NS", "")
        weekly = fetch_weekly_history(clean_sym)
        if weekly.empty: continue
        
        current_price = float(weekly['close'].iloc[-1])
        is_rubble, stats = check_rubble_and_sponge(weekly, current_price)
        
        if is_rubble:
            log.info(f"🔎 {clean_sym} passed Rubble/Sponge gate. Discount: {stats['discount']}%")
            survivors.append({"symbol": clean_sym, "price": current_price, "stats": stats})
            
    log.info(f"Phase 1 Complete. {len(survivors)} stocks primed for Insider Audit.")
    
    # PHASE 2: The Heist & Insider Audit (Deep)
    pearls = []
    for s in survivors:
        sym = s['symbol']
        log.info(f"🕵️ Heisting NSE data for {sym}...")
        
        filings, insiders = fetch_insider_and_filings(sym)
        audit = insider_friend_audit(sym, filings, insiders)
        
        # Insider Friend Gate: Must have a stealth catalyst OR actual insider buying
        if audit.get("stealth_catalyst_found") or audit.get("insider_buying_found"):
            if audit.get("confidence_score", 0) > 60:
                s['audit'] = audit
                pearls.append(s)
                log.info(f"💎 PEARL FOUND: {sym} | {audit['insider_summary']}")

    # PHASE 3: Telegram Dispatch
    if pearls:
        lines = ["🕴️ <b>THE INSIDER SYNDICATE (v4.0)</b>", "<i>Stealth Pearls Identified 3 Months Early</i>\n"]
        for p in pearls:
            lines.append(f"💎 <b>{p['symbol']}</b> (₹{p['price']})")
            lines.append(f"📉 Discount: {p['stats']['discount']}% from 52W High")
            lines.append(f"🧽 Whales: {p['stats']['sponge_weeks']} Sponge Weeks")
            lines.append(f"🤫 <b>Insider intel:</b> {p['audit']['insider_summary']}\n")
        _send_tg("\n".join(lines))
    else:
        _send_tg("🕴️ <b>THE INSIDER SYNDICATE</b>\nNo stealth insider action detected today. We wait in the shadows.")

if __name__ == "__main__":
    run()
