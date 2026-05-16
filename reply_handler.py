#!/usr/bin/env python3
"""
reply_handler.py — Telegram reply poller for SNIPER v3.0-M
Bismillah — In the name of Allah, the Most Gracious, the Most Merciful

Run via GitHub Actions every 10 minutes during market hours:
  cron: "*/10 3-10 * * 1-5"   # 8:30 AM - 4 PM IST on weekdays

Parses your Telegram replies and logs decisions to the DB.

Supported reply formats:
  TAKEN TCS @ 3445           → logs TAKEN with entry price
  TAKEN TCS 3445             → same (@ optional)
  TAKEN TCS                  → logs TAKEN, entry = signal close price
  SKIPPED TCS earnings        → logs SKIPPED with reason
  SKIPPED TCS                → logs SKIPPED, reason = "unspecified"
  SKIPPED                    → logs ALL today's picks as SKIPPED  ← NEW
  PARTIAL TCS @ 3440 50       → logs TAKEN with 50 shares
  HELP or ?                  → sends command reference back

FIXES in this version:
  FIX-A  Bare "SKIPPED" (no symbol) now marks ALL of today's picks as SKIPPED.
         Common pattern: user reviews Telegram picks at end of day and replies
         "SKIPPED" to dismiss everything — was silently ignored before.
  FIX-B  Symbol validation before every DB write. If the symbol is not in
         today's sniper_results, the handler replies with an error and refuses
         to create an orphan row (which would corrupt the feedback loop).
  FIX-C  PARTIAL with shares=0 now warns the user instead of silently logging
         a zero-share decision (uninformative for the capacity guard).
  FIX-D  Added HELP / ? command so users can retrieve the format list in-chat.
  FIX-E  Unrecognised commands now get a helpful nudge instead of silent drop.
"""

import os, re, sqlite3, logging, requests
from datetime import datetime
from pathlib import Path
from typing import List

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH          = Path(os.getenv("CACHE_PATH", "outputs/sniper_cache.db"))

# ── Patterns (case-insensitive) ───────────────────────────────────────────────
_TAKEN   = re.compile(r"^TAKEN\s+([A-Z&]+)(?:\s+[@:]?\s*([\d.]+))?", re.I)
_SKIPPED = re.compile(r"^SKIPPED(?:\s+([A-Z&]+)(?:\s+(.+))?)?$", re.I)  # FIX-A: symbol now optional
_PARTIAL = re.compile(r"^PARTIAL\s+([A-Z&]+)(?:\s+[@:]?\s*([\d.]+))?(?:\s+(\d+))?", re.I)
_HELP    = re.compile(r"^(HELP|\?)$", re.I)  # FIX-D

_HELP_TEXT = (
    "📖 SNIPER reply commands:\n"
    "  TAKEN SYM [@price]     — log a trade\n"
    "  PARTIAL SYM [@price] [shares] — log partial entry\n"
    "  SKIPPED SYM [reason]   — skip one pick\n"
    "  SKIPPED                — skip ALL today's picks\n"
    "  HELP or ?              — this message"
)


# ── Offset persistence ────────────────────────────────────────────────────────

def _get_updates(offset: int = 0):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 10}, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception as e:
        log.warning(f"Telegram getUpdates failed: {e}")
    return []


def _save_offset(offset: int):
    try:
        Path("outputs/tg_offset.txt").write_text(str(offset))
    except Exception:
        pass


def _load_offset() -> int:
    try:
        return int(Path("outputs/tg_offset.txt").read_text().strip())
    except Exception:
        return 0


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_todays_signal(symbol: str) -> dict:
    """Look up today's signal for a symbol. Returns empty dict if not found."""
    today = datetime.today().strftime("%Y-%m-%d")
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        row = con.execute(
            "SELECT close, fused_score, grade FROM sniper_results WHERE symbol=? AND run_date=? LIMIT 1",
            (symbol.upper(), today)
        ).fetchone()
        meta = con.execute(
            "SELECT primary_fused_score FROM meta_features WHERE symbol=? AND run_date=? LIMIT 1",
            (symbol.upper(), today)
        ).fetchone()
        con.close()
        return {
            "close":     row[0] if row else None,
            "fused":     row[1] if row else None,
            "grade":     row[2] if row else None,
            "meta_prob": meta[0] if meta else None,
        }
    except Exception as e:
        log.warning(f"Signal lookup {symbol}: {e}")
        return {}


def _get_todays_picks() -> List[dict]:
    """Return all symbols that were picks today (from sniper_results)."""
    today = datetime.today().strftime("%Y-%m-%d")
    try:
        con  = sqlite3.connect(DB_PATH, timeout=5)
        rows = con.execute(
            "SELECT symbol, close, fused_score, grade FROM sniper_results WHERE run_date=?",
            (today,)
        ).fetchall()
        con.close()
        return [{"symbol": r[0], "close": r[1], "fused": r[2], "grade": r[3]} for r in rows]
    except Exception as e:
        log.warning(f"Today's picks lookup failed: {e}")
        return []


def _log_decision(symbol: str, decision: str, entry_price=None,
                   shares=0, skip_reason=None):
    today = datetime.today().strftime("%Y-%m-%d")
    sig   = _get_todays_signal(symbol)

    if entry_price is None and sig.get("close"):
        entry_price = sig["close"]

    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.execute("""
            INSERT OR REPLACE INTO trade_decisions
              (run_date, symbol, decision, entry_price, shares_taken, skip_reason,
               ai_confidence, worth_flag)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            today, symbol.upper(), decision,
            entry_price, shares or 0, skip_reason,
            sig.get("meta_prob"), None
        ))
        con.commit(); con.close()
        log.info(f"✅ Decision logged: {symbol} → {decision} | "
                 f"₹{entry_price or '—'} | reason: {skip_reason or '—'}")
    except Exception as e:
        log.error(f"Decision log failed: {e}")


def _send_ack(chat_id: str, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram sendMessage failed: {e}")


# ── Main poller ───────────────────────────────────────────────────────────────

def process_updates():
    offset  = _load_offset()
    updates = _get_updates(offset)

    if not updates:
        log.debug("No new updates")
        return

    for update in updates:
        uid     = update.get("update_id", 0)
        msg     = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text    = (msg.get("text") or "").strip().upper()

        # Only process from your chat
        if chat_id != TELEGRAM_CHAT_ID:
            _save_offset(uid + 1)
            continue

        if not text:
            _save_offset(uid + 1)
            continue

        log.info(f"Processing reply: {text}")

        # ── HELP ─────────────────────────────────────────────────────────────
        # FIX-D: respond to HELP or ? with the command reference
        if _HELP.match(text):
            _send_ack(chat_id, _HELP_TEXT)
            _save_offset(uid + 1)
            continue

        # ── TAKEN ─────────────────────────────────────────────────────────────
        m = _TAKEN.match(text)
        if m:
            sym   = m.group(1)
            price = float(m.group(2)) if m.group(2) else None

            # FIX-B: validate symbol exists in today's picks before writing
            sig = _get_todays_signal(sym)
            if sig.get("close") is None:
                _send_ack(chat_id,
                    f"⚠️ {sym} not in today's picks — check ticker and try again.")
                _save_offset(uid + 1)
                continue

            _log_decision(sym, "TAKEN", entry_price=price)
            ack = f"✅ TAKEN {sym} logged"
            if price: ack += f" @ ₹{price:.0f}"
            if sig.get("grade"): ack += f" | {sig['grade']}"
            _send_ack(chat_id, ack)
            _save_offset(uid + 1)
            continue

        # ── SKIPPED ───────────────────────────────────────────────────────────
        m = _SKIPPED.match(text)
        if m:
            sym    = m.group(1)  # None when bare "SKIPPED"
            reason = (m.group(2) or "unspecified").lower() if sym else "skipped all"

            # FIX-A: bare "SKIPPED" → mark every today's pick as SKIPPED
            if sym is None:
                picks = _get_todays_picks()
                if not picks:
                    _send_ack(chat_id, "ℹ️ No picks found for today — nothing to skip.")
                    _save_offset(uid + 1)
                    continue
                for p in picks:
                    _log_decision(p["symbol"], "SKIPPED", skip_reason="skipped all")
                syms_str = ", ".join(p["symbol"] for p in picks)
                _send_ack(chat_id,
                    f"📋 All {len(picks)} today's picks marked SKIPPED:\n{syms_str}")
                _save_offset(uid + 1)
                continue

            # Single symbol SKIPPED — FIX-B: validate first
            sig = _get_todays_signal(sym)
            if sig.get("close") is None:
                _send_ack(chat_id,
                    f"⚠️ {sym} not in today's picks — check ticker and try again.")
                _save_offset(uid + 1)
                continue

            _log_decision(sym, "SKIPPED", skip_reason=reason)
            _send_ack(chat_id, f"📋 SKIPPED {sym} logged — reason: {reason}")
            _save_offset(uid + 1)
            continue

        # ── PARTIAL ───────────────────────────────────────────────────────────
        m = _PARTIAL.match(text)
        if m:
            sym    = m.group(1)
            price  = float(m.group(2)) if m.group(2) else None
            shares = int(m.group(3)) if m.group(3) else 0

            # FIX-B: validate symbol
            sig = _get_todays_signal(sym)
            if sig.get("close") is None:
                _send_ack(chat_id,
                    f"⚠️ {sym} not in today's picks — check ticker and try again.")
                _save_offset(uid + 1)
                continue

            # FIX-C: warn on zero shares — still log but flag it
            if shares == 0:
                _send_ack(chat_id,
                    f"⚠️ PARTIAL {sym}: no share count given. "
                    f"Logging anyway — reply 'PARTIAL {sym} @price shares' to correct.")

            _log_decision(sym, "TAKEN", entry_price=price, shares=shares)
            ack = f"✅ PARTIAL {sym} logged"
            if price:  ack += f" @ ₹{price:.0f}"
            if shares: ack += f" | {shares} shares"
            if shares == 0: ack += " | ⚠️ shares=0"
            _send_ack(chat_id, ack)
            _save_offset(uid + 1)
            continue

        # ── Unrecognised ──────────────────────────────────────────────────────
        # FIX-E: helpful nudge instead of silent drop
        _send_ack(chat_id,
            f"❓ Unknown command: {text[:40]}\nReply HELP or ? for the command list.")
        _save_offset(uid + 1)

    log.info(f"Processed {len(updates)} update(s)")


if __name__ == "__main__":
    process_updates()
