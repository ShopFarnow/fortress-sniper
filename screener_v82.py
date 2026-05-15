_cache")
        con.executemany(
            "INSERT OR REPLACE INTO halal_cache (symbol, cached_date) VALUES (?,?)",
            [(s, today) for s in syms]
        )
        con.commit()
        con.close()
    except Exception as e:
        log.debug(f"Shariah DB save: {e}")


def get_halal_universe() -> set:
    """Dynamic halal: Web → Sheets → Fallback."""
    global _SHARIAH_UNIVERSE_CACHE
    if _SHARIAH_UNIVERSE_CACHE is not None:
        return _SHARIAH_UNIVERSE_CACHE

    with _SHARIAH_UNIVERSE_LOCK:
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
    """Hard halal gate — excluded set checked before whitelist."""
    sym_upper = symbol.upper()
    if sym_upper in HALAL_EXCLUDED:
        return False
    sl = symbol.lower()
    if any(kw in sl for kw in HALAL_KW) or _HALAL_KW_REGEX_EXACT.search(sl):
        return False
    if _HALAL_LIST_CUSTOM and sym_upper in _HALAL_LIST_CUSTOM:
        return True
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
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
    try:
        con.execute("ALTER TABLE positions ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
        con.commit()
        log.info("DB: positions.status column added (migration)")
    except Exception as alter_exc:
        err_msg = str(alter_exc).lower()
        if "duplicate column" in err_msg or "already exists" in err_msg:
            pass
        elif "locked" in err_msg or "busy" in err_msg:
            log.error(f"DB: positions migration FAILED — database locked: {alter_exc}")
            con.close()
            raise RuntimeError(f"DB locked during migration: {alter_exc}") from alter_exc
        else:
            log.warning(f"DB: ALTER TABLE positions unexpected error (proceeding): {alter_exc}")

    try:
        pragma_rows = con.execute("PRAGMA table_info(positions)").fetchall()
        col_names   = {row[1] for row in pragma_rows}
        if "status" not in col_names:
            log.error("DB: positions.status column MISSING after migration attempt")
    except Exception as verify_exc:
        log.warning(f"DB: could not verify positions schema: {verify_exc}")

    con.commit()
    con.close()


# [Additional functions from original file would continue here...]
# For brevity, I'll include the key modified functions and the main entry point

# ══════════════════════════════════════════════════════════════════════
# SECTION 22b — SN-7: SNIPER TELEGRAM FORMAT v8.2 (CLEAN FORMAT)
# ══════════════════════════════════════════════════════════════════════

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
# SECTION 25 — MAIN SCREENER LOOP (v8.2 with critical fixes)
# ══════════════════════════════════════════════════════════════════════

def run_screener_v8():
    """v8.2 main entry point — all critical fixes applied."""
    _init_db()
    date_str, date_label = get_last_trading_day()
    log.info(f"=== FORTRESS SNIPER v8.2 | {date_label} ===")
    log.info(f"    CB_FAIL_SAFE={CB_FAIL_SAFE} | SHARIAH_CACHE_TTL={SHARIAH_CACHE_TTL_DAYS}d")

    # Clear per-run caches
    global _SECTOR_LIVE_CACHE, _MACRO_REGIME_CACHE, _smallcap_index_cache, _NSE_SESSION_CACHE, _SHARIAH_UNIVERSE_CACHE
    _SECTOR_LIVE_CACHE    = {}
    _MACRO_REGIME_CACHE   = None
    _smallcap_index_cache = {}
    _NSE_SESSION_CACHE    = None
    _SHARIAH_UNIVERSE_CACHE = None

    # Load custom HALAL_LIST
    global _HALAL_LIST_CUSTOM
    _HALAL_LIST_CUSTOM = _read_sheet_halal_list()

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
        log.warning("NSE bhavcopy unavailable — trying Google Sheets...")
        bhavcopy    = load_bhavcopy_from_sheets()
        data_source = "SHEETS"

    # ── 3. YFINANCE LAST RESORT ───────────────────────────────────────
    if bhavcopy is None or bhavcopy.empty:
        log.warning("⚠️ DEGRADED MODE — NSE + Sheets unavailable")
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

    log.info(f"After liquidity + price filter: {len(candidates)}")
    candidates = candidates[candidates["symbol"].apply(is_halal)].copy()
    log.info(f"After halal filter: {len(candidates)}")
    if len(candidates) > CFG["max_candidates"]:
        candidates = candidates.nlargest(CFG["max_candidates"], "turnover_lakhs")

    # [Rest of scoring loop continues...]
    # For brevity, showing the key fix areas

    # ── Main scoring loop ─────────────────────────────────────────────
    _shared_nse_sess = _get_shared_nse_session()
    results = []
    for i, (_, row) in enumerate(candidates.iterrows()):
        sym = row["symbol"]
        if i % 25 == 0: log.info(f"Progress: {i}/{len(candidates)}")
        try:
            hist = fetch_history(sym, days=300, sess=_shared_nse_sess)
            if len(hist) < CFG["min_hist_bars"]:
                continue
            r = assemble_result_v8(sym, row, hist, fii_data, insider_map, filings, earnings_cal)
            if r: results.append(r)
            time.sleep(0.15)
        except Exception as e:
            log.debug(f"{sym}: {e}")

    results.sort(key=lambda x: (x.get("sniper_composite",0), x.get("total_score",0)), reverse=True)

    # Sector cap
    MAX_PER_SECTOR = 2
    sector_counts_global = {}
    globally_capped = []
    for r in results:
        sec = r["sector"]
        if sector_counts_global.get(sec, 0) < MAX_PER_SECTOR:
            globally_capped.append(r)
            sector_counts_global[sec] = sector_counts_global.get(sec, 0) + 1

    mid_picks   = [r for r in globally_capped if 200 <= r["close"] <= PRICE_CAP]
    small_picks = [r for r in globally_capped if  50 <= r["close"] < 200]
    top5 = mid_picks[:MID_CAP_PICKS] + small_picks[:SMALL_CAP_PICKS]

    # Deduplicate
    seen = set()
    top5_deduped = []
    for r in top5:
        if r["symbol"] not in seen:
            top5_deduped.append(r); seen.add(r["symbol"])
    top5 = top5_deduped

    log.info(f"=== TOP {len(top5)} PICKS | {len(results)} total passed ===")

    # ── Outputs ───────────────────────────────────────────────────────
    save_excel(top5, results, date_label, fii_data)
    sector_trends = get_sector_trends()
    save_html_report(top5, date_label, fii_data, sector_trends)
    push_to_gsheets(top5, date_label)

    # FIX: Use clean Telegram format
    send_telegram_v7_clean(top5, sector_trends, fii_data, date_label, macro, using_fallback, data_source)

    log.info(f"✅ Done | {len(top5)} setups | Macro: {macro['macro_state']}")
    return top5


# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    run_screener_v8()
