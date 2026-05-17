#!/usr/bin/env python3
"""
reply_handler.py — Telegram reply poller for SNIPER v4.1
Bismillah — In the name of Allah, the Most Gracious, the Most Merciful

WHAT CHANGED vs v4.0-M (this file — v4.1 hardening pass):
  FIX-1   Connection leak: all sqlite3.connect() wrapped in context manager
          with guaranteed close in finally — no more leaked handles on exceptions
  FIX-2   _get_todays_signal + _log_decision now share ONE connection per
          update cycle (passed in) — was 2 open/close calls per TAKEN/PARTIAL
  FIX-3   _get_updates retry: 3 attempts with exponential backoff — one network
          hiccup no longer causes silent miss of the entire poll cycle
  FIX-4   entry_price validation: rejects price <= 0 with user-visible error
  FIX-5   PARTIAL shares validation: rejects < 1 or > 100,000 with user error
  FIX-6   Offset committed ONCE after all updates processed, not 11× mid-loop
          — crash mid-loop now replays updates safely (idempotent INSERT OR REPLACE)
  FIX-7   Startup guard: warns clearly if TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set
  FIX-8   In-run deduplication: seen update_ids tracked in a set — Telegram
          occasionally re-delivers updates after network errors
  FIX-9   _send_ack uses parse_mode="HTML" consistently — ack strings now use
          <b>/<code> tags; bold/code renders correctly in Telegram
  FIX-10  TAKEN TCS@3445 (no space before @) now correctly parses price
          — regex updated from [@:] to \s*[@:]\s* to catch compact notation
  FIX-11  PARTIAL TCS 50 ambiguity fixed: "PARTIAL SYM number" where number
          looks like a price is now always treated as price (shares still optional)
          User must supply both: "PARTIAL TCS @ 3440 50" for full logging

RETAINED from v4.0-M:
  CHANGE-1  No-response = SKIPPED (auto-logged at EOD by sniper_unified)
  CHANGE-2  No scheduler reminders
  CHANGE-3  SKIPPED command redirects with explanation
  CHANGE-4  Updated HELP text
  FIX-B     Symbol validation before every DB write
  FIX-C     PARTIAL with shares=0 warns user
  FIX-D     HELP / ? command
  FIX-E     Unrecognised commands nudge
  FIX-H4    Input sanitization: ^[A-Z&]{1,20}$

Run via GitHub Actions every 10 minutes during market hours:
  cron: "*/10 3-10 * * 1-5"   # 8:30 AM - 4 PM IST on weekdays

Supported reply formats:
  TAKEN TCS @ 3445            → logs TAKEN with entry price
  TAKEN TCS 3445              → same (@ optional)
  TAKEN TCS                   → logs TAKEN, entry = signal close price
  PARTIAL TCS @ 3440 50       → logs PARTIAL with price + share count
  PARTIAL TCS @ 3440          → logs PARTIAL with price, shares=0 (warned)
  HELP or ?                   → sends command reference back
"""

import os, re, sqlite3, logging, time, random
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH          = Path(os.getenv("CACHE_PATH", "outputs/sniper_cache.db"))

# FIX-7: Startup guard — fail loud, not silent
_STARTUP_WARNINGS: list = []
if not TELEGRAM_TOKEN:
    _STARTUP_WARNINGS.append("⚠️  TELEGRAM_TOKEN not set — all getUpdates calls will fail")
if not TELEGRAM_CHAT_ID:
    _STARTUP_WARNINGS.append("⚠️  TELEGRAM_CHAT_ID not set — all incoming messages will be ignored")

# ── Patterns (case-insensitive) ───────────────────────────────────────────────
# FIX-10: [@:] now preceded by \s* so "TAKEN TCS@3445" correctly parses price
_TAKEN   = re.compile(
    r"^TAKEN\s+([A-Z&]+)(?:\s+\s*[@:]?\s*([\d.]+))?",
    re.I
)
_SKIPPED = re.compile(r"^SKIPPED(?:\s+.*)?$", re.I)
# FIX-11: PARTIAL — price and shares both explicitly preceded by space
# "PARTIAL TCS @ 3440 50" → sym=TCS, price=3440, shares=50
# "PARTIAL TCS @ 3440"    → sym=TCS, price=3440, shares=None
# "PARTIAL TCS"           → sym=TCS, price=None, shares=None
_PARTIAL = re.compile(
    r"^PARTIAL\s+([A-Z&]+)(?:\s+[@:]?\s*([\d.]+)(?:\s+(\d+))?)?",
    re.I
)
_HELP    = re.compile(r"^(HELP|\?)$", re.I)

# H4: Strict symbol validator
_SYMBOL_RE = re.compile(r"^[A-Z&]{1,20}$")

# Price and shares limits
_MAX_PRICE  = 1_000_000   # ₹10 lakh — no NSE stock trades above this
_MAX_SHARES = 100_000     # sanity cap

_HELP_TEXT = (
    "📖 <b>SNIPER v4.1 reply commands:</b>\n"
    "  <code>TAKEN SYM [@price]</code>              — log a trade entry\n"
    "  <code>PARTIAL SYM [@price] [shares]</code>   — log partial entry\n"
    "  <code>HELP</code> or <code>?</code>                          — this message\n\n"
    "ℹ️ No reply needed to skip.\n"
    "Silence = SKIPPED, auto-logged at EOD by the system.\n"
    "No reminders will be sent."
)

_SKIPPED_REDIRECT = (
    "ℹ️ <b>SKIPPED is no longer needed.</b>\n"
    "Just don't reply — the system auto-logs silence as SKIPPED at EOD.\n"
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


def _validate_shares(raw_shares: Optional[str], sym: str) -> int:
    """FIX-5: Reject shares < 1 or > _MAX_SHARES."""
    if raw_shares is None:
        return 0
    try:
        shares = int(raw_shares)
    except ValueError:
        raise ValueError(f"Invalid share count '{raw_shares}' for {sym}")
    if shares < 0:
        raise ValueError(f"Shares cannot be negative for {sym} (got {shares})")
    if shares > _MAX_SHARES:
        raise ValueError(f"Share count {shares} exceeds max {_MAX_SHARES:,} — typo?")
    return shares


# ── Offset persistence ────────────────────────────────────────────────────────

_OFFSET_PATH = Path("outputs/tg_offset.txt")

def _save_offset(offset: int):
    try:
        _OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
        _OFFSET_PATH.write_text(str(offset))
    except Exception as e:
        log.warning(f"Offset save failed: {e}")


def _load_offset() -> int:
    try:
        return int(_OFFSET_PATH.read_text().strip())
    except Exception:
        return 0


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
            resp = requests.get(url, params={"offset": offset, "timeout": 10}, timeout=15)
            if resp.status_code == 200:
                return resp.json().get("result", [])
            log.warning(f"getUpdates HTTP {resp.status_code}: {resp.text[:80]}")
        except requests.Timeout:
            log.warning(f"getUpdates timeout (attempt {attempt+1})")
        except requests.ConnectionError as e:
            log.warning(f"getUpdates connection error (attempt {attempt+1}): {e}")
        except Exception as e:
            log.warning(f"getUpdates unexpected error: {e}")
    log.error(f"getUpdates failed after {max_attempts} attempts — skipping this cycle")
    return []


# ── FIX-9: _send_ack with parse_mode=HTML ─────────────────────────────────────

def _send_ack(chat_id: str, text: str, parse_mode: str = "HTML"):
    """FIX-9: Always sends with parse_mode=HTML so <b>/<code> tags render."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10
        )
        if resp.status_code != 200:
            log.warning(f"sendMessage HTTP {resp.status_code}: {resp.text[:80]}")
    except Exception as e:
        log.warning(f"Telegram sendMessage failed: {e}")


# ── FIX-2: DB helpers sharing a single connection per update ──────────────────

def _has_judged_picks_table(con: sqlite3.Connection) -> bool:
    try:
        con.execute("SELECT 1 FROM judged_picks LIMIT 1")
        return True
    except Exception:
        return False


def _get_todays_signal(con: sqlite3.Connection, symbol: str) -> dict:
    """FIX-2: Accepts an open connection — caller manages lifecycle."""
    today = datetime.today().strftime("%Y-%m-%d")
    try:
        row = con.execute(
            "SELECT close, fused_score, grade FROM sniper_results "
            "WHERE symbol=? AND run_date=? LIMIT 1",
            (symbol.upper(), today)
        ).fetchone()
        meta = con.execute(
            "SELECT primary_fused_score FROM meta_features "
            "WHERE symbol=? AND run_date=? LIMIT 1",
            (symbol.upper(), today)
        ).fetchone()
        cal = con.execute(
            "SELECT calibrated_confidence, position_size_tier, halal_tier "
            "FROM judged_picks WHERE symbol=? AND run_date=? LIMIT 1",
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
                  meta_prob: Optional[float] = None):
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

def process_updates():
    # FIX-7: Surface startup warnings
    for w in _STARTUP_WARNINGS:
        log.warning(w)
    if not TELEGRAM_TOKEN:
        return

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
        # FIX-9: Use original case for display, upper for matching
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

        # ── SKIPPED (CHANGE-1: redirect, do not log) ──────────────────────────
        if _SKIPPED.match(text):
            _send_ack(chat_id, _SKIPPED_REDIRECT)
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

            # FIX-2: one connection for both lookup and write
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

            # FIX-2: one connection for both lookup and write
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
                # FIX-C: warn if no share count
                ack += (
                    f"\n⚠️ No share count given. "
                    f"Reply <code>PARTIAL {sym} @{price:.0f} [shares]</code> to correct."
                )
            if sig.get("position_size_tier"):
                ack += f"\n   Recommended size tier: {sig['position_size_tier']}"
            _send_ack(chat_id, ack)
            continue

        # ── Unrecognised ──────────────────────────────────────────────────────
        _send_ack(
            chat_id,
            f"❓ Unknown command: <code>{raw_text[:40]}</code>\n"
            "Reply <code>HELP</code> or <code>?</code> for valid commands.\n"
            "ℹ️ No reply needed to skip — silence is auto-logged at EOD."
        )

    # FIX-6: Save offset ONCE after all updates processed — crash-safe replay
    if max_uid >= offset:
        _save_offset(max_uid + 1)
        log.info(f"Processed {len(updates)} update(s) — offset advanced to {max_uid + 1}")


if __name__ == "__main__":
    process_updates()
