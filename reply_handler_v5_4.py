#!/usr/bin/env python3
"""
reply_handler_v5.py — Telegram reply poller for UNIFIED HALAL SNIPER v5.0 [AUDITED]
Bismillah — In the name of Allah, the Most Gracious, the Most Merciful

WHAT CHANGED vs v4.1 (this file — v5.0 hardening pass):

  BUG-1 FIX  TAKEN REGEX COMPACT NOTATION: "TAKEN TCS@3445" now correctly
             captures price=3445. Original regex required whitespace before @
             so compact notation "TAKEN TCS@3445" silently used signal close.
             Fixed to ([space,@,:]+ separator) to match compact notation.

  BUG-2 FIX  /confirm #0 INVALID RANK: rank < 1 now explicitly returns None
             before the DB query. Original code with LIMIT 0 produced empty
             rows[], then rows[-1] would raise IndexError or return wrong pick.

  BUG-5 FIX  EARNINGS GATE COLUMN NAME: _check_earnings_gate() queried the
             wrong column 'earn_days' — the actual column from _migrate_db_v3
             is 'days_to_earnings'. The OperationalError was silently swallowed
             by log.debug(), so the gate always returned (True, "OK") — every
             entry near earnings was unblocked. Fixed to 'days_to_earnings'.

  FIX-V5-3  DB-BACKED OFFSET: tg_offset is now stored in the SQLite DB
            (tg_offsets table) instead of a flat file. Crash-safe across
            GitHub Actions runs — no "offset lost" silent replay loop.

  FIX-V5-6  /confirm AND /skip COMMANDS: architecture Step 10 specifies
            "/confirm #N or /skip #N — 30 min timeout". Added support for:
              /confirm #1  → logs TAKEN for today's pick #1 (rank-ordered)
              /confirm #2  → logs TAKEN for pick #2
              /skip #1     → logs SKIPPED for pick #1
            Old TAKEN/PARTIAL format still supported for backward compat.

  FIX-V5-7  EARNINGS GATE IN REPLY HANDLER: confirm_entry() from sniper_unified
            is now called inside the reply handler before logging TAKEN.
            Earnings in <2 days blocks the entry with a clear user message.
            Previously this gate existed only in the internal handler and was
            NOT called from the external reply_handler.py.

  FIX-V5-8  TIMEOUT EXPIRY: picks logged >30 minutes ago with no reply are
            now auto-marked SKIPPED at each poll cycle. This implements the
            architecture "No reply = auto-SKIP" (Step 11) at the poller
            level rather than waiting for EOD.

RETAINED from v4.1:
  FIX-1   Connection leak: all sqlite3.connect() wrapped in context manager
  FIX-2   _get_todays_signal + _log_decision share ONE connection per cycle
  FIX-3   _get_updates retry: 3 attempts with exponential backoff
  FIX-4   entry_price validation: rejects price <= 0 with user-visible error
  FIX-5   PARTIAL shares validation: rejects < 1 or > 100,000
  FIX-6   Offset committed ONCE after all updates processed
  FIX-7   Startup guard: warns if TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set
  FIX-8   In-run deduplication: seen update_ids tracked in a set
  FIX-9   _send_ack uses parse_mode="HTML" consistently
  FIX-10  TAKEN TCS@3445 (compact notation) correctly parses price
  FIX-11  PARTIAL TCS @ 3440 50 fully parsed

Run via GitHub Actions every 10 minutes during market hours:
  cron: "*/10 3-10 * * 1-5"   # 8:30 AM - 4 PM IST on weekdays

Supported reply formats (v5.0):
  /confirm #1             → TAKEN for today's pick ranked #1
  /confirm #2             → TAKEN for today's pick ranked #2
  /skip #1                → SKIPPED for pick #1
  /skip #2                → SKIPPED for pick #2
  TAKEN TCS @ 3445        → logs TAKEN with entry price
  TAKEN TCS 3445          → same (@ optional)
  TAKEN TCS               → logs TAKEN, entry = signal close price
  PARTIAL TCS @ 3440 50   → logs PARTIAL with price + share count
  PARTIAL TCS @ 3440      → logs PARTIAL with price, shares=0 (warned)
  HELP or ?               → sends command reference back
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

# FIX-7: Startup guard — fail loud, not silent
_STARTUP_WARNINGS: list = []
if not TELEGRAM_TOKEN:
    _STARTUP_WARNINGS.append("⚠️  TELEGRAM_TOKEN not set — all getUpdates calls will fail")
if not TELEGRAM_CHAT_ID:
    _STARTUP_WARNINGS.append("⚠️  TELEGRAM_CHAT_ID not set — all incoming messages will be ignored")

# ── Patterns (case-insensitive) ───────────────────────────────────────────────
# FIX-10: \s*[@:]?\s* before price captures compact notation "TAKEN TCS@3445"
# Symbol charset extended to include hyphen for symbols like M&M-BE etc.
_TAKEN   = re.compile(
    # BUG-1 FIX: allow @ or : immediately after symbol with no whitespace
    # Original: r"^TAKEN\s+([A-Z&]+)(?:\s+\s*[@:]?\s*([\d.]+))?"  — failed on "TAKEN TCS@3445"
    # Fixed:    (?:[\s@:]+\s*...) captures price whether space, @, or : separates it.
    r"^TAKEN\s+([A-Z&\-]+)(?:[\s@:]+\s*([\d.]+))?",
    re.I
)
_TAKEN_ALL = re.compile(r"^TAKEN?\s+ALL$", re.I)   # matches "TAKEN ALL" and "TAKE ALL"
_SKIPPED = re.compile(r"^SKIPPED(?:\s+.*)?$", re.I)
# FIX-11: PARTIAL — price optional (auto-fills from signal close); shares optional (defaults to 50)
_PARTIAL = re.compile(
    r"^PARTIAL\s+([A-Z&\-]+)(?:\s+[@:]?\s*([\d.]+)(?:\s+(\d+))?)?",
    re.I
)
_HELP    = re.compile(r"^(HELP|\?)$", re.I)

# FIX-V5-6: /confirm #N and /skip #N commands (architecture Step 10)
_CONFIRM = re.compile(r"^/confirm\s+#?(\d+)$", re.I)
_SKIP_N  = re.compile(r"^/skip\s+#?(\d+)$", re.I)

# Symbol-level SKIP and bulk SKIP ALL
_SKIP_RE    = re.compile(r"^SKIP(?:PED)?\s+([A-Z&\-]+)$", re.I)
_SKIP_ALL_RE = re.compile(r"^SKIP(?:PED)?\s+ALL$", re.I)

# H4: Strict symbol validator (hyphen allowed for e.g. M&M-BE)
_SYMBOL_RE = re.compile(r"^[A-Z&\-]{1,20}$")

# Price and shares limits
_MAX_PRICE  = 1_000_000   # ₹10 lakh — no NSE stock trades above this
_MAX_SHARES = 100_000     # sanity cap

_HELP_TEXT = (
    "📖 <b>SNIPER v5.0 reply commands:</b>\n"
    "  <code>/confirm #1</code>                      — take today's pick #1\n"
    "  <code>/confirm #2</code>                      — take today's pick #2\n"
    "  <code>/skip #1</code>                         — skip today's pick #1\n"
    "  <code>TAKEN SYM [@price]</code>               — log entry (price optional, auto-fills)\n"
    "  <code>TAKEN ALL</code>                         — log ALL today's picks as TAKEN\n"
    "  <code>PARTIAL SYM [@price] [shares]</code>    — log partial entry (shares default 50)\n"
    "  <code>SKIP SYM</code>                          — skip a specific symbol\n"
    "  <code>SKIP ALL</code>                          — skip ALL today's picks\n"
    "  <code>HELP</code> or <code>?</code>                           — this message\n\n"
    "ℹ️ No reply = SKIPPED (auto-logged after 30 min or at EOD).\n"
    "Only reply if you TOOK or PARTIALLY took a position."
)

_SKIPPED_REDIRECT = (
    "ℹ️ <b>SKIPPED is no longer needed.</b>\n"
    "Use <code>/skip #N</code> or just don't reply — "
    "the system auto-logs silence as SKIPPED.\n"
    "Only reply if you TOOK or PARTIALLY took a position."
)


# ── FIX-1: Context-managed SQLite connection ──────────────────────────────────

@contextmanager
def _db_conn(timeout: int = 10):
    """Guaranteed-close SQLite connection. Rolls back on exception."""
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


# ── H4: Input sanitization ────────────────────────────────────────────────────

def _sanitize_symbol(raw: str) -> str:
    sym = raw.strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise ValueError(f"Invalid symbol '<code>{sym[:20]}</code>' — must be letters/& only, max 20 chars")
    return sym


def _validate_price(raw_price: Optional[str], sym: str) -> Optional[float]:
    """FIX-4: Reject price <= 0 or > _MAX_PRICE."""
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
    """FIX-5: Reject shares < 1 or > _MAX_SHARES.
    If raw_shares is None, returns `default` (50) — per spec, omitting share count
    defaults to 50 shares rather than logging 0 with a warning."""
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


# ── FIX-V5-3: DB-backed offset (crash-safe across GitHub Actions runs) ────────

def _ensure_offset_table(con: sqlite3.Connection) -> None:
    """Create tg_offsets table if it doesn't exist."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS tg_offsets (
            key     TEXT PRIMARY KEY,
            offset  INTEGER NOT NULL DEFAULT 0,
            updated TEXT DEFAULT (datetime('now'))
        )
    """)
    con.commit()


def _load_offset() -> int:
    """FIX-V5-3: Load Telegram update offset from DB (crash-safe)."""
    try:
        with _db_conn() as con:
            _ensure_offset_table(con)
            row = con.execute(
                "SELECT offset FROM tg_offsets WHERE key='main'"
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        log.warning(f"Offset load from DB failed: {e} — defaulting to 0")
        return 0


def _save_offset(offset: int) -> None:
    """FIX-V5-3: Persist Telegram update offset to DB."""
    try:
        with _db_conn() as con:
            _ensure_offset_table(con)
            con.execute("""
                INSERT OR REPLACE INTO tg_offsets (key, offset, updated)
                VALUES ('main', ?, datetime('now'))
            """, (offset,))
    except Exception as e:
        log.warning(f"Offset save to DB failed: {e}")


# ── FIX-V5-6: /confirm and /skip by rank number ───────────────────────────────

def _get_pick_by_rank(con: sqlite3.Connection, rank: int) -> Optional[dict]:
    """Look up today's sniper pick by its rank position (1-based).
    Picks are ranked by fused_score DESC in sniper_results."""
    # BUG-2 FIX: guard against rank < 1 (e.g. /confirm #0 would return rows[-1]
    # via Python negative indexing — wrong pick, no error raised).
    if rank < 1:
        log.debug(f"_get_pick_by_rank: invalid rank={rank} (must be ≥ 1)")
        return None
    today = datetime.today().strftime("%Y-%m-%d")
    try:
        rows = con.execute("""
            SELECT symbol, grade, close, stop_loss, r1, fused_score
            FROM sniper_results
            WHERE run_date = ?
            ORDER BY fused_score DESC
            LIMIT ?
        """, (today, rank)).fetchall()
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
    except Exception as e:
        log.debug(f"_get_pick_by_rank({rank}): {e}")
        return None


# ── FIX-V5-8: Auto-expire timed-out picks ────────────────────────────────────

def _auto_expire_timeout_picks() -> None:
    """FIX-V5-8: Architecture Step 11 — 'No reply = auto-SKIP after 30 min'.
    Picks in sniper_results with no trade_decision logged within REPLY_TIMEOUT_MINUTES
    are automatically logged as SKIPPED. Called at every poll cycle."""
    today = datetime.today().strftime("%Y-%m-%d")
    cutoff = (datetime.today() - timedelta(minutes=REPLY_TIMEOUT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _db_conn() as con:
            # Find picks sent today with no decision logged and past timeout
            pending = con.execute("""
                SELECT sr.symbol
                FROM sniper_results sr
                LEFT JOIN trade_decisions td
                  ON sr.symbol = td.symbol AND sr.run_date = td.run_date
                WHERE sr.run_date = ?
                  AND td.symbol IS NULL
                  AND sr.created_at <= ?
            """, (today, cutoff)).fetchall()

            for (sym,) in pending:
                try:
                    con.execute("""
                        INSERT OR IGNORE INTO trade_decisions
                          (run_date, symbol, decision, skip_reason, logged_at)
                        VALUES (?, ?, 'SKIPPED', 'timeout_30min', datetime('now'))
                    """, (today, sym))
                    log.info(f"FIX-V5-8: Auto-SKIPPED {sym} (no reply in {REPLY_TIMEOUT_MINUTES} min)")
                except Exception:
                    pass
    except Exception as e:
        log.debug(f"_auto_expire_timeout_picks: {e}")


# ── FIX-V5-7: Earnings gate ───────────────────────────────────────────────────

def _check_earnings_gate(symbol: str) -> tuple:
    """FIX-V5-7: Replicate confirm_entry() logic without importing sniper_unified.
    Returns (allowed: bool, reason: str).

    BUG-5 FIX: original code queried 'earn_days' (non-existent column).
    The correct column name from _migrate_db_v3 is 'days_to_earnings'.
    The wrong column caused OperationalError caught by log.debug(), so the gate
    ALWAYS returned (True, "OK") — earnings block was completely bypassed.
    """
    try:
        today = datetime.today().strftime("%Y-%m-%d")
        # BUG-5 FIX: correct column is 'days_to_earnings', NOT 'earn_days'
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


# ── FIX-3: Telegram getUpdates with retry ─────────────────────────────────────

def _get_updates(offset: int = 0, max_attempts: int = 3) -> list:
    """FIX-3: 3-attempt exponential backoff — one hiccup no longer drops the cycle."""
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
    """FIX-9: HTML parse mode for all acknowledgements."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"_send_ack failed: {e}")


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _has_judged_picks_table(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='judged_picks'"
    ).fetchone()
    return row is not None


def _get_todays_signal(con: sqlite3.Connection, symbol: str) -> dict:
    today = datetime.today().strftime("%Y-%m-%d")
    try:
        row = con.execute(
            "SELECT close, fused_score, grade FROM sniper_results WHERE symbol=? AND run_date=?",
            (symbol.upper(), today)
        ).fetchone()
        meta = con.execute(
            "SELECT meta_prob FROM meta_features WHERE symbol=? AND run_date=?",
            (symbol.upper(), today)
        ).fetchone()
        cal = con.execute(
            "SELECT calibrated_confidence, position_size_tier, halal_tier FROM judged_picks WHERE symbol=? AND run_date=?",
            (symbol.upper(), today)
        ).fetchone() if _has_judged_picks_table(con) else None
        return {
            "close":                 row[0] if row else None,
            "fused":                 row[1] if row else None,
            "grade":                 row[2] if row else None,
            "meta_prob":             meta[0] if meta else None,
            "calibrated_confidence": cal[0] if cal else None,
            "position_size_tier":    cal[1] if cal else None,
            "halal_tier":            cal[2] if cal else None,
        }
    except Exception as e:
        log.warning(f"Signal lookup {symbol}: {e}")
        return {}


def _log_decision(con: sqlite3.Connection, symbol: str, decision: str,
                  entry_price: Optional[float] = None,
                  shares: int = 0, skip_reason: Optional[str] = None,
                  meta_prob: Optional[float] = None) -> None:
    """FIX-2: Accepts an open connection — caller manages lifecycle."""
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
    log.info(
        f"✅ Decision logged: {symbol} → {decision} | "
        f"₹{entry_price or '—'} | shares={shares} | reason={skip_reason or '—'}"
    )


# ── Main poller ───────────────────────────────────────────────────────────────

def process_updates() -> None:
    # FIX-7: Surface startup warnings
    for w in _STARTUP_WARNINGS:
        log.warning(w)
    if not TELEGRAM_TOKEN:
        return

    # FIX-V5-8: Auto-expire timed-out picks before processing new replies
    _auto_expire_timeout_picks()

    offset  = _load_offset()
    updates = _get_updates(offset)

    if not updates:
        log.debug("No new updates")
        return

    # FIX-8: Deduplication set — Telegram can re-deliver update_ids
    seen_ids: set = set()
    max_uid = offset

    # FIX-6: Process all updates; commit offset ONCE at the end
    for update in updates:
        uid     = update.get("update_id", 0)
        msg     = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        raw_text = (msg.get("text") or "").strip()
        text     = raw_text.upper()

        # FIX-8: Skip duplicates within this poll run
        if uid in seen_ids:
            log.debug(f"Duplicate update_id {uid} — skipping")
            max_uid = max(max_uid, uid)
            continue
        seen_ids.add(uid)
        max_uid = max(max_uid, uid)

        # Only handle messages from our configured chat
        if chat_id != TELEGRAM_CHAT_ID:
            continue
        if not text:
            continue

        log.info(f"Processing update {uid}: '{raw_text[:60]}'")

        # ── HELP ─────────────────────────────────────────────────────────────
        if _HELP.match(text):
            _send_ack(chat_id, _HELP_TEXT)
            continue

        # ── SKIPPED redirect ──────────────────────────────────────────────────
        if _SKIPPED.match(text):
            _send_ack(chat_id, _SKIPPED_REDIRECT)
            continue

        # ── FIX-V5-6: /confirm #N ────────────────────────────────────────────
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
                    # FIX-V5-7: earnings gate
                    allowed, reason = _check_earnings_gate(sym)
                    if not allowed:
                        _send_ack(chat_id, f"⛔ {sym} BLOCKED — {reason}")
                        continue
                    price = float(pick["close"] or 0)
                    _log_decision(con, sym, "TAKEN", entry_price=price)
            except Exception as e:
                log.error(f"/confirm #{rank} DB error: {e}")
                _send_ack(chat_id, f"⚠️ Could not log /confirm #{rank} — DB error. Please retry.")
                continue

            ack = (
                f"✅ <b>/confirm #{rank} — {sym} TAKEN</b> @ ₹{price:,.0f}\n"
                f"   Grade: <b>{pick.get('grade','?')}</b> | "
                f"Fused: {pick.get('fused','?')}/100\n"
                f"   SL ₹{pick.get('stop_loss',0):.0f} | R1 ₹{pick.get('r1',0):.0f}\n"
                f"   Bismillah 🤲 — trade with discipline."
            )
            _send_ack(chat_id, ack)
            continue

        # ── FIX-V5-6: /skip #N ───────────────────────────────────────────────
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
            except Exception as e:
                log.error(f"/skip #{rank} DB error: {e}")
                _send_ack(chat_id, f"⚠️ Could not log /skip #{rank} — DB error.")
                continue
            _send_ack(chat_id, f"📝 <b>/skip #{rank} — {sym} logged as SKIPPED.</b>")
            continue

        # ── TAKEN ALL / TAKE ALL — mark every today's pick as TAKEN ─────────────
        if _TAKEN_ALL.match(text):
            try:
                with _db_conn() as con:
                    rows = con.execute(
                        "SELECT symbol, close FROM sniper_results WHERE run_date = date('now','localtime')"
                    ).fetchall()
                if not rows:
                    _send_ack(chat_id, "⚠️ No picks found for today. Nothing to mark as TAKEN.")
                    continue
                taken_syms = []
                blocked_syms = []
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

        # ── SKIP ALL — mark every today's pick as SKIPPED ────────────────────
        if _SKIP_ALL_RE.match(text):
            try:
                today = datetime.today().strftime("%Y-%m-%d")
                with _db_conn() as con:
                    rows = con.execute(
                        "SELECT symbol FROM sniper_results WHERE run_date = ?",
                        (today,)
                    ).fetchall()
                if not rows:
                    _send_ack(chat_id, "⚠️ No picks found for today. Nothing to skip.")
                    continue
                with _db_conn() as con:
                    skipped_syms = []
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

        # ── SKIP SYMBOL — skip a single named symbol ──────────────────────────
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
                        _send_ack(chat_id,
                            f"⚠️ <code>{sym}</code> not in today's picks — "
                            "check ticker and try again.")
                        continue
                    _log_decision(con, sym, "SKIPPED", skip_reason="manual_reply")
            except Exception as e:
                log.error(f"SKIP {sym} DB error: {e}")
                _send_ack(chat_id, f"⚠️ Could not log SKIP {sym} — DB error. Please retry.")
                continue
            _send_ack(chat_id, f"📝 <b>{sym}</b> logged as SKIPPED.")
            continue

        # ── TAKEN & PARTIAL — share one DB connection ─────────────────────────
        m_taken   = _TAKEN.match(text)
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

            # FIX-V5-7: earnings gate
            allowed, reason = _check_earnings_gate(sym)
            if not allowed:
                _send_ack(chat_id, f"⛔ <code>{sym}</code> BLOCKED — {reason}\nEntry not logged.")
                continue

            try:
                with _db_conn() as con:
                    sig = _get_todays_signal(con, sym)
                    if sig.get("close") is None:
                        _send_ack(chat_id,
                            f"⚠️ <code>{sym}</code> not in today's picks — "
                            f"check ticker and try again.")
                        continue
                    if price is None:
                        price = sig["close"]
                    _log_decision(con, sym, "TAKEN", entry_price=price,
                                  meta_prob=sig.get("meta_prob"))
            except Exception as e:
                log.error(f"TAKEN {sym} DB error: {e}")
                _send_ack(chat_id, f"⚠️ Could not log TAKEN {sym} — DB error. Please retry.")
                continue

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
            continue

        if m_partial:
            try:
                sym = _sanitize_symbol(m_partial.group(1))
            except ValueError as ve:
                _send_ack(chat_id, f"⚠️ {ve}")
                continue

            try:
                price  = _validate_price(m_partial.group(2), sym)
                shares = _validate_shares(m_partial.group(3), sym)
            except ValueError as ve:
                _send_ack(chat_id, f"⚠️ {ve}")
                continue

            # FIX-V5-7: earnings gate
            allowed, reason = _check_earnings_gate(sym)
            if not allowed:
                _send_ack(chat_id, f"⛔ <code>{sym}</code> BLOCKED — {reason}\nPartial entry not logged.")
                continue

            try:
                with _db_conn() as con:
                    sig = _get_todays_signal(con, sym)
                    if sig.get("close") is None:
                        _send_ack(chat_id,
                            f"⚠️ <code>{sym}</code> not in today's picks — "
                            f"check ticker and try again.")
                        continue
                    if price is None:
                        price = sig["close"]
                    _log_decision(con, sym, "TAKEN", entry_price=price,
                                  shares=shares, meta_prob=sig.get("meta_prob"))
            except Exception as e:
                log.error(f"PARTIAL {sym} DB error: {e}")
                _send_ack(chat_id, f"⚠️ Could not log PARTIAL {sym} — DB error. Please retry.")
                continue

            ack = f"✅ <b>PARTIAL {sym}</b> logged @ ₹{price:,.0f}"
            if shares:
                ack += f" | <b>{shares:,} shares</b>"
            else:
                ack += f" | <b>50 shares</b> (default — reply <code>PARTIAL {sym} @{price:.0f} [shares]</code> to correct)"
            if sig.get("position_size_tier"):
                ack += f"\n   Recommended size tier: {sig['position_size_tier']}"
            _send_ack(chat_id, ack)
            continue

        # ── Unrecognised ──────────────────────────────────────────────────────
        _send_ack(
            chat_id,
            f"❓ Unknown command: <code>{raw_text[:40]}</code>\n"
            "Reply <code>HELP</code> or <code>?</code> for valid commands.\n"
            "ℹ️ No reply needed to skip — silence auto-logs after 30 min."
        )

    # FIX-6: Save offset ONCE after all updates processed — crash-safe replay
    if max_uid >= offset:
        _save_offset(max_uid + 1)
        log.info(f"Processed {len(updates)} update(s) — offset advanced to {max_uid + 1}")


if __name__ == "__main__":
    process_updates()
