#!/usr/bin/env python3
"""
reply_handler_v5.py — Telegram reply poller for UNIFIED HALAL SNIPER v5.0 [AUDITED]
Bismillah — In the name of Allah, the Most Gracious, the Most Merciful

Run via GitHub Actions every 10 minutes during market hours:
  cron: "*/10 3-10 * * 1-5"   # 8:30 AM - 4 PM IST on weekdays

Supported reply formats (v5.0):
  /confirm #1             → TAKEN for today's pick ranked #1
  /confirm #2             → TAKEN for today's pick ranked #2
  /skip #1                → SKIPPED for pick #1
  /skip #2                → SKIPPED for pick #2
  TAKEN SYM [@price]      → log entry (price optional, auto-fills)
  TAKEN ALL               → log ALL today's picks as TAKEN (uses latest run_date)
  PARTIAL SYM [@price] [shares] → log partial entry (shares default 50)
  SKIP SYM                → skip a specific symbol
  SKIP ALL                → skip ALL today's picks
  HELP or ?               → send command reference
"""

import os, re, sqlite3, logging, time, random
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH          = Path(os.getenv("CACHE_PATH", "outputs/sniper_cache.db"))

# Reply timeout — picks not confirmed within this window are auto-SKIPPED
REPLY_TIMEOUT_MINUTES = int(os.getenv("REPLY_TIMEOUT_MINUTES", "30"))

# Startup guard
_STARTUP_WARNINGS: list = []
if not TELEGRAM_TOKEN:
    _STARTUP_WARNINGS.append("⚠️  TELEGRAM_TOKEN not set — all getUpdates calls will fail")
if not TELEGRAM_CHAT_ID:
    _STARTUP_WARNINGS.append("⚠️  TELEGRAM_CHAT_ID not set — all incoming messages will be ignored")

# ── Patterns (case-insensitive) ───────────────────────────────────────────────
_TAKEN   = re.compile(r"^TAKEN\s+([A-Z&\-]+)(?:[\s@:]+\s*([\d.]+))?", re.I)
_TAKEN_ALL = re.compile(r"^TAKEN?\s+ALL$", re.I)
_SKIPPED = re.compile(r"^SKIPPED(?:\s+.*)?$", re.I)
_PARTIAL = re.compile(r"^PARTIAL\s+([A-Z&\-]+)(?:\s+[@:]?\s*([\d.]+)(?:\s+(\d+))?)?", re.I)
_HELP    = re.compile(r"^(HELP|\?)$", re.I)

_CONFIRM = re.compile(r"^/confirm\s+#?(\d+)$", re.I)
_SKIP_N  = re.compile(r"^/skip\s+#?(\d+)$", re.I)
_STATUS  = re.compile(r"^/status$", re.I)   # FIX-v5.4: real-time system health query

_SKIP_RE    = re.compile(r"^SKIP(?:PED)?\s+([A-Z&\-]+)$", re.I)
_SKIP_ALL_RE = re.compile(r"^SKIP(?:PED)?\s+ALL$", re.I)

_SYMBOL_RE = re.compile(r"^[A-Z&\-]{1,20}$")

_MAX_PRICE  = 1_000_000
_MAX_SHARES = 100_000

_HELP_TEXT = (
    "📖 <b>SNIPER v5.4 reply commands:</b>\n"
    "  <code>/confirm #1</code>                      — take today's pick #1\n"
    "  <code>/confirm #2</code>                      — take today's pick #2\n"
    "  <code>/skip #1</code>                         — skip today's pick #1\n"
    "  <code>TAKEN SYM [@price]</code>               — log entry (price optional, auto-fills)\n"
    "  <code>TAKEN ALL</code>                        — log ALL today's picks as TAKEN\n"
    "  <code>PARTIAL SYM [@price] [shares]</code>    — log partial entry (shares default 50)\n"
    "  <code>SKIP SYM</code>                          — skip a specific symbol\n"
    "  <code>SKIP ALL</code>                          — skip ALL today's picks\n"
    "  <code>/status</code>                          — system health &amp; open positions\n"
    "  <code>HELP</code> or <code>?</code>           — this message\n\n"
    "ℹ️ No reply = SKIPPED (auto-logged after 30 min or at EOD).\n"
    "Only reply if you TOOK or PARTIALLY took a position."
)

_SKIPPED_REDIRECT = (
    "ℹ️ <b>SKIPPED is no longer needed.</b>\n"
    "Use <code>/skip #N</code> or just don't reply — "
    "the system auto-logs silence as SKIPPED.\n"
    "Only reply if you TOOK or PARTIALLY took a position."
)

# ── SQLite connection (context manager) ──────────────────────────────────────
@contextmanager
def _db_conn(timeout: int = 10):
    con = None
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=timeout)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        yield con
        con.commit()
    except Exception:
        if con:
            try: con.rollback()
            except Exception: pass
        raise
    finally:
        if con:
            try: con.close()
            except Exception: pass

# ── Input validation ─────────────────────────────────────────────────────────
def _sanitize_symbol(raw: str) -> str:
    sym = raw.strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise ValueError(f"Invalid symbol '<code>{sym[:20]}</code>' — must be letters/& only, max 20 chars")
    return sym

def _validate_price(raw_price: Optional[str], sym: str) -> Optional[float]:
    if raw_price is None:
        return None
    try:
        price = float(raw_price)
    except ValueError:
        raise ValueError(f"Invalid price '{raw_price}' for {sym}")
    if price <= 0:
        raise ValueError(f"Price must be > 0 for {sym} (got {price})")
    if price > _MAX_PRICE:
        raise ValueError(f"Price ₹{price:,.0f} exceeds max ₹{_MAX_PRICE:,.0f} — typo?")
    return price

def _validate_shares(raw_shares: Optional[str], sym: str, default: int = 50) -> int:
    if raw_shares is None:
        return default
    try:
        shares = int(raw_shares)
    except ValueError:
        raise ValueError(f"Invalid share count '{raw_shares}' for {sym}")
    if shares < 0:
        raise ValueError(f"Shares cannot be negative for {sym} (got {shares})")
    if shares > _MAX_SHARES:
        raise ValueError(f"Share count {shares} exceeds max {_MAX_SHARES:,} — typo?")
    return shares

# ── Persistent offset (file‑based, cache separate from DB) ───────────────────
OFFSET_FILE = Path("tg_offset.txt")

def _load_offset() -> int:
    try:
        if OFFSET_FILE.exists():
            return int(OFFSET_FILE.read_text().strip())
    except Exception:
        pass
    return 0

def _save_offset(offset: int) -> None:
    try:
        OFFSET_FILE.write_text(str(offset))
    except Exception as e:
        log.warning(f"Failed to save offset: {e}")

# ── /confirm and /skip by rank (now checks both tables) ──────────────────────
def _get_pick_by_rank(con: sqlite3.Connection, rank: int) -> Optional[dict]:
    if rank < 1:
        log.debug(f"_get_pick_by_rank: invalid rank={rank} (must be ≥ 1)")
        return None

    # Get latest run_date from either table
    latest_run = con.execute("""
        SELECT MAX(run_date) FROM (
            SELECT run_date FROM sniper_results
            UNION
            SELECT run_date FROM sniper_results_v54
        )
    """).fetchone()[0]
    if not latest_run:
        return None

    # Try sniper_results (fused picks)
    rows = con.execute("""
        SELECT symbol, grade, close, stop_loss, r1, fused_score
        FROM sniper_results
        WHERE run_date = ?
        ORDER BY fused_score DESC
        LIMIT ?
    """, (latest_run, rank)).fetchall()
    if len(rows) < rank:
        # Fallback to sniper_results_v54 (lane winners)
        rows = con.execute("""
            SELECT symbol, grade, close, stop_loss, r1, fused_score
            FROM sniper_results_v54
            WHERE run_date = ?
            ORDER BY fused_score DESC
            LIMIT ?
        """, (latest_run, rank)).fetchall()
    if len(rows) < rank:
        return None
    row = rows[rank - 1]
    return {
        "symbol":     row[0],
        "grade":      row[1],
        "close":      row[2],
        "stop_loss":  row[3],
        "r1":         row[4],
        "fused":      row[5],
    }

# ── Auto‑expire picks with no reply after timeout (checks both tables) ───────
def _auto_expire_timeout_picks() -> None:
    today = datetime.today().strftime("%Y-%m-%d")
    cutoff = (datetime.today() - timedelta(minutes=REPLY_TIMEOUT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _db_conn() as con:
            # Combine picks from both tables
            pending = con.execute(f"""
                SELECT symbol FROM (
                    SELECT symbol, created_at FROM sniper_results WHERE run_date = ?
                    UNION ALL
                    SELECT symbol, created_at FROM sniper_results_v54 WHERE run_date = ?
                ) AS all_picks
                WHERE symbol NOT IN (
                    SELECT symbol FROM trade_decisions WHERE run_date = ?
                )
                AND created_at <= ?
            """, (today, today, today, cutoff)).fetchall()
            for (sym,) in pending:
                try:
                    con.execute("""
                        INSERT OR IGNORE INTO trade_decisions
                          (run_date, symbol, decision, skip_reason, logged_at)
                        VALUES (?, ?, 'SKIPPED', 'timeout_30min', datetime('now'))
                    """, (today, sym))
                    log.info(f"Auto-SKIPPED {sym} (no reply in {REPLY_TIMEOUT_MINUTES} min)")
                except Exception:
                    pass
    except Exception as e:
        log.debug(f"_auto_expire_timeout_picks: {e}")

# ── Earnings gate (checks days_to_earnings) ──────────────────────────────────
def _check_earnings_gate(symbol: str) -> tuple:
    try:
        today = datetime.today().strftime("%Y-%m-%d")
        with _db_conn() as con:
            row = con.execute("""
                SELECT days_to_earnings FROM meta_features
                WHERE symbol = ? AND run_date = ?
            """, (symbol.upper(), today)).fetchone()
        if row and row[0] is not None:
            earn_days = int(row[0])
            if 0 <= earn_days <= 1:
                return False, f"Earnings in {earn_days}d — entry blocked to avoid volatility"
    except Exception as e:
        log.debug(f"Earnings gate check {symbol}: {e}")
    return True, "OK"

# ── Telegram getUpdates with retry ──────────────────────────────────────────
def _get_updates(offset: int = 0, max_attempts: int = 3) -> list:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    for attempt in range(max_attempts):
        if attempt:
            delay = (2 ** attempt) + random.uniform(0, 0.5)
            log.debug(f"getUpdates retry {attempt}/{max_attempts-1} — sleeping {delay:.1f}s")
            time.sleep(delay)
        try:
            resp = requests.get(
                url,
                params={"offset": offset, "timeout": 5, "allowed_updates": ["message"]},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json().get("result", [])
            log.warning(f"getUpdates HTTP {resp.status_code}")
        except Exception as e:
            log.warning(f"getUpdates attempt {attempt+1}: {e}")
    return []

def _send_ack(chat_id: str, text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"_send_ack failed: {e}")

# ── DB helper: get signal for a symbol (checks both tables) ─────────────────
def _get_todays_signal(con: sqlite3.Connection, symbol: str) -> dict:
    # Get latest run_date from either table
    latest_run = con.execute("""
        SELECT MAX(run_date) FROM (
            SELECT run_date FROM sniper_results
            UNION
            SELECT run_date FROM sniper_results_v54
        )
    """).fetchone()[0]
    if not latest_run:
        return {}

    # Try sniper_results first (fused picks)
    row = con.execute(
        "SELECT close, fused_score, grade FROM sniper_results WHERE symbol=? AND run_date=?",
        (symbol.upper(), latest_run)
    ).fetchone()
    if not row:
        # Try sniper_results_v54 (lane winners)
        row = con.execute(
            "SELECT close, fused_score, grade FROM sniper_results_v54 WHERE symbol=? AND run_date=?",
            (symbol.upper(), latest_run)
        ).fetchone()
    if not row:
        return {}

    # Safe meta_prob retrieval
    meta_prob = None
    try:
        meta_row = con.execute(
            "SELECT meta_prob FROM meta_features WHERE symbol=? AND run_date=?",
            (symbol.upper(), latest_run)
        ).fetchone()
        if meta_row:
            meta_prob = meta_row[0]
    except sqlite3.OperationalError:
        pass

    cal = None
    try:
        cal = con.execute(
            "SELECT calibrated_confidence, position_size_tier, halal_tier FROM judged_picks WHERE symbol=? AND run_date=?",
            (symbol.upper(), latest_run)
        ).fetchone()
    except Exception:
        pass

    return {
        "close":                 row[0],
        "fused":                 row[1],
        "grade":                 row[2],
        "meta_prob":             meta_prob,
        "calibrated_confidence": cal[0] if cal else None,
        "position_size_tier":    cal[1] if cal else None,
        "halal_tier":            cal[2] if cal else None,
    }

def _log_decision(con: sqlite3.Connection, symbol: str, decision: str,
                  entry_price: Optional[float] = None,
                  shares: int = 0, skip_reason: Optional[str] = None,
                  meta_prob: Optional[float] = None) -> None:
    today = datetime.today().strftime("%Y-%m-%d")
    con.execute("""
        INSERT OR REPLACE INTO trade_decisions
          (run_date, symbol, decision, entry_price, shares_taken,
           skip_reason, ai_confidence, worth_flag)
        VALUES (?,?,?,?,?,?,?,?)
    """, (
        today, symbol.upper(), decision,
        entry_price, shares or 0, skip_reason,
        meta_prob, None
    ))
    log.info(f"✅ Decision logged: {symbol} → {decision} | ₹{entry_price or '—'} | shares={shares} | reason={skip_reason or '—'}")

# ── Main polling loop ───────────────────────────────────────────────────────
def process_updates() -> None:
    for w in _STARTUP_WARNINGS:
        log.warning(w)
    if not TELEGRAM_TOKEN:
        return

    _auto_expire_timeout_picks()

    offset = _load_offset()
    updates = _get_updates(offset)

    if not updates:
        log.debug("No new updates")
        return

    seen_ids: set = set()
    max_uid = offset

    for update in updates:
        uid = update.get("update_id", 0)
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        raw_text = (msg.get("text") or "").strip()
        text = raw_text.upper()

        if uid in seen_ids:
            log.debug(f"Duplicate update_id {uid} — skipping")
            max_uid = max(max_uid, uid)
            continue
        seen_ids.add(uid)
        max_uid = max(max_uid, uid)

        if chat_id != TELEGRAM_CHAT_ID:
            continue
        if not text:
            continue

        log.info(f"Processing update {uid}: '{raw_text[:60]}'")

        # --- HELP ---
        if _HELP.match(text):
            _send_ack(chat_id, _HELP_TEXT)
            continue

        # --- /status — FIX-v5.4: real-time system health ----------------
        if _STATUS.match(raw_text):
            try:
                with _db_conn() as con:
                    open_pos = con.execute(
                        "SELECT COUNT(*) FROM pick_outcomes WHERE status='open'"
                    ).fetchone()[0]
                    today_picks = con.execute(
                        "SELECT COUNT(*) FROM sniper_results WHERE run_date=date('now')"
                    ).fetchone()[0] + con.execute(
                        "SELECT COUNT(*) FROM sniper_results_v54 WHERE run_date=date('now')"
                    ).fetchone()[0]
                    total_decisions = con.execute(
                        "SELECT COUNT(*) FROM trade_decisions"
                    ).fetchone()[0]
                    total_closed = con.execute(
                        "SELECT COUNT(*) FROM pick_outcomes WHERE status!='open'"
                    ).fetchone()[0]
                    wins = con.execute(
                        "SELECT COUNT(*) FROM pick_outcomes WHERE status IN ('r1_hit','r2_hit','r3_hit')"
                    ).fetchone()[0]
                    last_run = con.execute("""
                        SELECT MAX(run_date) FROM (
                            SELECT run_date FROM sniper_results
                            UNION
                            SELECT run_date FROM sniper_results_v54
                        )
                    """).fetchone()[0] or "never"
                    meta_trained = con.execute(
                        "SELECT COUNT(*) FROM meta_features WHERE profitable IS NOT NULL"
                    ).fetchone()[0]
                    surv_rows = con.execute(
                        "SELECT COUNT(*) FROM survival_training"
                    ).fetchone()[0]

                wr_str = (
                    f"{wins}/{total_closed} ({wins*100//total_closed}% WR)"
                    if total_closed > 0 else "no closed trades yet"
                )
                model_status = (
                    f"trained ({meta_trained} labelled samples)"
                    if meta_trained >= 20 else f"cold-start ({meta_trained}/20 samples)"
                )
                survival_status = (
                    f"active ({surv_rows} rows)"
                    if surv_rows >= 100 else f"dormant ({surv_rows}/100 rows)"
                )

                msg = (
                    f"📊 <b>System Status — SNIPER v5.4</b>\n"
                    f"🕐 <b>Last run:</b> {last_run}\n\n"
                    f"<b>Positions</b>\n"
                    f"  Open: <b>{open_pos}</b>\n"
                    f"  Today's picks: <b>{today_picks}</b>\n"
                    f"  Total decisions logged: <b>{total_decisions}</b>\n\n"
                    f"<b>Performance</b>\n"
                    f"  Closed trades: {wr_str}\n\n"
                    f"<b>ML Models</b>\n"
                    f"  Meta-labeler: {model_status}\n"
                    f"  Survival model: {survival_status}\n\n"
                    f"<i>Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC</i>"
                )
                _send_ack(chat_id, msg)
                log.info("/status replied successfully")
            except Exception as e:
                log.error(f"/status DB error: {e}")
                _send_ack(chat_id, "⚠️ Could not fetch status — DB error. Please retry.")
            continue

        # --- SKIPPED redirect ---
        if _SKIPPED.match(text):
            _send_ack(chat_id, _SKIPPED_REDIRECT)
            continue

        # --- /confirm #N ---
        m_confirm = _CONFIRM.match(raw_text)
        if m_confirm:
            rank = int(m_confirm.group(1))
            try:
                with _db_conn() as con:
                    pick = _get_pick_by_rank(con, rank)
                    if pick is None:
                        _send_ack(chat_id, f"⚠️ No pick at rank #{rank} today — check /confirm #1 or #2.")
                        continue
                    sym = pick["symbol"]
                    allowed, reason = _check_earnings_gate(sym)
                    if not allowed:
                        _send_ack(chat_id, f"⛔ {sym} BLOCKED — {reason}")
                        continue
                    price = float(pick["close"] or 0)
                    _log_decision(con, sym, "TAKEN", entry_price=price)
                    ack = (
                        f"✅ <b>/confirm #{rank} — {sym} TAKEN</b> @ ₹{price:,.0f}\n"
                        f"   Grade: <b>{pick.get('grade','?')}</b> | "
                        f"Fused: {pick.get('fused','?')}/100\n"
                        f"   SL ₹{pick.get('stop_loss',0):.0f} | R1 ₹{pick.get('r1',0):.0f}\n"
                        f"   Bismillah 🤲 — trade with discipline."
                    )
                    _send_ack(chat_id, ack)
            except Exception as e:
                log.error(f"/confirm #{rank} DB error: {e}")
                _send_ack(chat_id, f"⚠️ Could not log /confirm #{rank} — DB error. Please retry.")
            continue

        # --- /skip #N ---
        m_skip_n = _SKIP_N.match(raw_text)
        if m_skip_n:
            rank = int(m_skip_n.group(1))
            try:
                with _db_conn() as con:
                    pick = _get_pick_by_rank(con, rank)
                    if pick is None:
                        _send_ack(chat_id, f"⚠️ No pick at rank #{rank} today.")
                        continue
                    sym = pick["symbol"]
                    _log_decision(con, sym, "SKIPPED", skip_reason="manual_skip_command")
                    _send_ack(chat_id, f"📝 <b>/skip #{rank} — {sym} logged as SKIPPED.</b>")
            except Exception as e:
                log.error(f"/skip #{rank} DB error: {e}")
                _send_ack(chat_id, f"⚠️ Could not log /skip #{rank} — DB error.")
            continue

        # --- TAKEN ALL ---
        if _TAKEN_ALL.match(text):
            log.info(f"TAKEN ALL matched for text: '{raw_text}'")
            try:
                with _db_conn() as con:
                    # Check if either table exists
                    table_check = con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sniper_results','sniper_results_v54')"
                    ).fetchall()
                    if not table_check:
                        _send_ack(chat_id, "⚠️ No picks available yet – main sniper run hasn't completed.\nPlease wait a few minutes and try again.")
                        continue
                    # Get latest run_date from either table
                    latest_run = con.execute("""
                        SELECT MAX(run_date) FROM (
                            SELECT run_date FROM sniper_results
                            UNION
                            SELECT run_date FROM sniper_results_v54
                        )
                    """).fetchone()[0]
                    if not latest_run:
                        _send_ack(chat_id, "⚠️ No picks found for today. Nothing to mark as TAKEN.")
                        continue
                    # Fetch from both tables, deduplicate by symbol
                    rows = con.execute("""
                        SELECT symbol, close FROM sniper_results WHERE run_date = ?
                        UNION
                        SELECT symbol, close FROM sniper_results_v54 WHERE run_date = ?
                    """, (latest_run, latest_run)).fetchall()
                if not rows:
                    _send_ack(chat_id, "⚠️ No picks found for today. Nothing to mark as TAKEN.")
                    continue
                taken_syms, blocked_syms = [], []
                with _db_conn() as con:
                    for sym, price in rows:
                        allowed, reason = _check_earnings_gate(sym)
                        if not allowed:
                            blocked_syms.append(f"{sym} ({reason})")
                            continue
                        entry_price = float(price or 0)
                        _log_decision(con, sym, "TAKEN", entry_price=entry_price)
                        taken_syms.append(f"{sym} @ ₹{entry_price:,.0f}")
                ack_lines = []
                if taken_syms:
                    ack_lines.append(f"✅ <b>{len(taken_syms)} pick(s) marked as TAKEN:</b>")
                    ack_lines += [f"  • {s}" for s in taken_syms]
                    ack_lines.append("Bismillah 🤲 — trade with discipline.")
                if blocked_syms:
                    ack_lines.append(f"\n⛔ <b>{len(blocked_syms)} blocked (earnings gate):</b>")
                    ack_lines += [f"  • {s}" for s in blocked_syms]
                _send_ack(chat_id, "\n".join(ack_lines))
                log.info(f"TAKEN ALL: {len(taken_syms)} logged, {len(blocked_syms)} blocked")
            except Exception as e:
                log.error(f"TAKEN ALL DB error: {e}")
                _send_ack(chat_id, "⚠️ TAKEN ALL failed — DB error. Please retry.")
            continue

        # --- SKIP ALL ---
        if _SKIP_ALL_RE.match(text):
            try:
                with _db_conn() as con:
                    table_check = con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sniper_results','sniper_results_v54')"
                    ).fetchall()
                    if not table_check:
                        _send_ack(chat_id, "⚠️ No picks available yet – main sniper run hasn't completed.\nPlease wait a few minutes and try again.")
                        continue
                    latest_run = con.execute("""
                        SELECT MAX(run_date) FROM (
                            SELECT run_date FROM sniper_results
                            UNION
                            SELECT run_date FROM sniper_results_v54
                        )
                    """).fetchone()[0]
                    if not latest_run:
                        _send_ack(chat_id, "⚠️ No picks found for today. Nothing to skip.")
                        continue
                    rows = con.execute("""
                        SELECT symbol FROM sniper_results WHERE run_date = ?
                        UNION
                        SELECT symbol FROM sniper_results_v54 WHERE run_date = ?
                    """, (latest_run, latest_run)).fetchall()
                if not rows:
                    _send_ack(chat_id, "⚠️ No picks found for today. Nothing to skip.")
                    continue
                skipped_syms = []
                with _db_conn() as con:
                    for (sym,) in rows:
                        _log_decision(con, sym, "SKIPPED", skip_reason="manual_all")
                        skipped_syms.append(sym)
                ack = f"📝 <b>{len(skipped_syms)} pick(s) marked as SKIPPED:</b>\n"
                ack += "\n".join(f"  • {s}" for s in skipped_syms)
                _send_ack(chat_id, ack)
                log.info(f"SKIP ALL: {len(skipped_syms)} picks marked SKIPPED")
            except Exception as e:
                log.error(f"SKIP ALL DB error: {e}")
                _send_ack(chat_id, "⚠️ SKIP ALL failed — DB error. Please retry.")
            continue

        # --- SKIP SYMBOL ---
        m_skip_sym = _SKIP_RE.match(text)
        if m_skip_sym:
            try:
                sym = _sanitize_symbol(m_skip_sym.group(1))
            except ValueError as ve:
                _send_ack(chat_id, f"⚠️ {ve}")
                continue
            try:
                with _db_conn() as con:
                    sig = _get_todays_signal(con, sym)
                    if sig.get("close") is None:
                        _send_ack(chat_id, f"⚠️ <code>{sym}</code> not found in the latest picks.\nThe main sniper may not have run yet, or this symbol was not selected today.\nCheck the latest report or try <code>TAKEN ALL</code>.")
                        continue
                    _log_decision(con, sym, "SKIPPED", skip_reason="manual_reply")
                    _send_ack(chat_id, f"📝 <b>{sym}</b> logged as SKIPPED.")
            except Exception as e:
                log.error(f"SKIP {sym} DB error: {e}")
                _send_ack(chat_id, f"⚠️ Could not log SKIP {sym} — DB error. Please retry.")
            continue

        # --- TAKEN (single symbol) ---
        m_taken = _TAKEN.match(text)
        m_partial = _PARTIAL.match(text)

        if m_taken and not text.startswith("PARTIAL"):
            try:
                sym = _sanitize_symbol(m_taken.group(1))
            except ValueError as ve:
                _send_ack(chat_id, f"⚠️ {ve}")
                continue
            try:
                price = _validate_price(m_taken.group(2), sym)
            except ValueError as ve:
                _send_ack(chat_id, f"⚠️ {ve}")
                continue
            allowed, reason = _check_earnings_gate(sym)
            if not allowed:
                _send_ack(chat_id, f"⛔ <code>{sym}</code> BLOCKED — {reason}\nEntry not logged.")
                continue
            try:
                with _db_conn() as con:
                    sig = _get_todays_signal(con, sym)
                    if sig.get("close") is None:
                        _send_ack(chat_id, f"⚠️ <code>{sym}</code> not found in the latest picks.\nThe main sniper may not have run yet, or this symbol was not selected today.")
                        continue
                    if price is None:
                        price = sig["close"]
                    _log_decision(con, sym, "TAKEN", entry_price=price, meta_prob=sig.get("meta_prob"))
                    ack = f"✅ <b>TAKEN {sym}</b> logged @ ₹{price:,.0f}"
                    if sig.get("grade"):
                        ack += f" | <b>{sig['grade']}</b>"
                    if sig.get("calibrated_confidence"):
                        ack += f"\n   Cal. confidence: <b>{sig['calibrated_confidence']:.0%}</b>"
                    if sig.get("position_size_tier"):
                        ack += f" | Size: {sig['position_size_tier']}"
                    if sig.get("halal_tier"):
                        ack += f" | Halal: {sig['halal_tier']}"
                    _send_ack(chat_id, ack)
            except Exception as e:
                log.error(f"TAKEN {sym} DB error: {e}")
                _send_ack(chat_id, f"⚠️ Could not log TAKEN {sym} — DB error. Please retry.")
            continue

        # --- PARTIAL (single symbol) ---
        if m_partial:
            try:
                sym = _sanitize_symbol(m_partial.group(1))
            except ValueError as ve:
                _send_ack(chat_id, f"⚠️ {ve}")
                continue
            try:
                price = _validate_price(m_partial.group(2), sym)
                shares = _validate_shares(m_partial.group(3), sym)
            except ValueError as ve:
                _send_ack(chat_id, f"⚠️ {ve}")
                continue
            allowed, reason = _check_earnings_gate(sym)
            if not allowed:
                _send_ack(chat_id, f"⛔ <code>{sym}</code> BLOCKED — {reason}\nPartial entry not logged.")
                continue
            try:
                with _db_conn() as con:
                    sig = _get_todays_signal(con, sym)
                    if sig.get("close") is None:
                        _send_ack(chat_id, f"⚠️ <code>{sym}</code> not found in the latest picks.\nThe main sniper may not have run yet, or this symbol was not selected today.")
                        continue
                    if price is None:
                        price = sig["close"]
                    _log_decision(con, sym, "TAKEN", entry_price=price, shares=shares, meta_prob=sig.get("meta_prob"))
                    ack = f"✅ <b>PARTIAL {sym}</b> logged @ ₹{price:,.0f}"
                    if shares:
                        ack += f" | <b>{shares:,} shares</b>"
                    else:
                        ack += f" | <b>50 shares</b> (default)"
                    if sig.get("position_size_tier"):
                        ack += f"\n   Recommended size tier: {sig['position_size_tier']}"
                    _send_ack(chat_id, ack)
            except Exception as e:
                log.error(f"PARTIAL {sym} DB error: {e}")
                _send_ack(chat_id, f"⚠️ Could not log PARTIAL {sym} — DB error. Please retry.")
            continue

        # ── Unrecognised: only reply if the message looks like a command ──
        command_keywords = ["/", "TAKEN", "SKIP", "PARTIAL", "HELP"]
        if any(kw in text for kw in command_keywords):
            _send_ack(
                chat_id,
                f"❓ Unknown command: <code>{raw_text[:40]}</code>\n"
                "Reply <code>HELP</code> or <code>?</code> for valid commands.\n"
                "ℹ️ No reply needed to skip — silence auto-logs after 30 min."
            )
        else:
            log.debug(f"Ignored non-command message: '{raw_text[:60]}'")

    # Save offset after processing all updates
    if max_uid >= offset:
        _save_offset(max_uid + 1)
        log.info(f"Processed {len(updates)} update(s) — offset advanced to {max_uid + 1}")

if __name__ == "__main__":
    process_updates()
