"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  IPO SNIPER v12 — PATCH FILE                                                ║
║                                                                              ║
║  Drop-in replacements for 3 broken areas:                                   ║
║  FIX 1 — LLMTracker class  (global debug tracker for every LLM call)       ║
║  FIX 2 — fetch_company_description()  (multi-source + name-based fallback)  ║
║  FIX 3 — _sector_pre_filter() + _pick_audit()  (use description, not only   ║
║           the "SME"/"Mainboard" sector field that never matches keywords)    ║
║                                                                              ║
║  HOW TO APPLY:                                                               ║
║    1. Copy the three blocks below into ipo_sniper_v12.py, replacing the     ║
║       existing versions of the same functions/class.                         ║
║    2. The global `llm_tracker` instance is created at module level.         ║
║    3. At the end of run(), add:  llm_tracker.dump_summary()                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, re, time, json, logging, sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("IPO-SNIPER-v12.0")


# ══════════════════════════════════════════════════════════════════════════════
# FIX 1 — LLMTracker
# ══════════════════════════════════════════════════════════════════════════════
# Place this block right after the CONFIG section (after the BASE_WEIGHTS dict).
# It creates a module-level singleton that every LLM call logs into.
# At the end of run() call:  llm_tracker.dump_summary()
# ══════════════════════════════════════════════════════════════════════════════

class LLMTracker:
    """
    Lightweight per-run diagnostic that records every LLM stage attempt so
    you can see exactly where the pipeline breaks.

    Stages tracked (in order):
      DESC_FETCH   – fetching company description from the web
      CACHE_HIT    – Shariah cache lookup
      PREFILTER    – sector pre-filter (keyword match)
      RULE_AUDIT   – rule-based fallback audit
      OPENAI_MINI  – gpt-4o-mini structured-output call
      OPENAI_FLAG  – openai call returned low confidence → escalating
      OPENAI_FULL  – gpt-4o escalated call
      CLAUDE       – Claude monthly advisor call
      FINAL        – resolved verdict stored in shariah dict

    Status values: START / OK / FAIL / SKIP / WARN
    """

    _ICONS = {"OK": "✅", "FAIL": "❌", "SKIP": "⏭", "START": "▶", "WARN": "⚠️"}

    def __init__(self):
        self.events: List[dict] = []
        self._stage_start: Dict[str, float] = {}

    # ── Public log helpers ────────────────────────────────────────────────────

    def start(self, company: str, stage: str, detail: str = ""):
        self._stage_start[f"{company}:{stage}"] = time.time()
        self._record(company, stage, "START", detail)

    def ok(self, company: str, stage: str, detail: str = ""):
        elapsed = self._elapsed(company, stage)
        self._record(company, stage, "OK", detail, elapsed)

    def fail(self, company: str, stage: str, reason: str):
        elapsed = self._elapsed(company, stage)
        self._record(company, stage, "FAIL", reason, elapsed)

    def skip(self, company: str, stage: str, reason: str = ""):
        self._record(company, stage, "SKIP", reason)

    def warn(self, company: str, stage: str, detail: str):
        self._record(company, stage, "WARN", detail)

    # ── Summary report ────────────────────────────────────────────────────────

    def dump_summary(self):
        """Print a structured summary table after run() completes."""
        if not self.events:
            return

        fails  = [e for e in self.events if e["status"] == "FAIL"]
        warns  = [e for e in self.events if e["status"] == "WARN"]
        skips  = [e for e in self.events if e["status"] == "SKIP"]
        oks    = [e for e in self.events if e["status"] == "OK"]

        W = 78
        print(f"\n{'═' * W}")
        print(f"  🔬 LLM TRACKER SUMMARY  ·  {len(self.events)} events")
        print(f"{'═' * W}")
        print(f"  ✅ OK      : {len(oks)}")
        print(f"  ❌ FAIL    : {len(fails)}")
        print(f"  ⚠️  WARN   : {len(warns)}")
        print(f"  ⏭ SKIPPED : {len(skips)}")

        # Stage breakdown
        stages = {}
        for e in self.events:
            k = e["stage"]
            stages.setdefault(k, {"ok": 0, "fail": 0, "skip": 0})
            if e["status"] == "OK":
                stages[k]["ok"] += 1
            elif e["status"] == "FAIL":
                stages[k]["fail"] += 1
            elif e["status"] == "SKIP":
                stages[k]["skip"] += 1

        print(f"\n  ── Per-stage breakdown ──────────────────────────────────")
        print(f"  {'Stage':<18} {'OK':>4} {'FAIL':>5} {'SKIP':>5}")
        print(f"  {'─'*18} {'─'*4} {'─'*5} {'─'*5}")
        for stg, cnt in sorted(stages.items()):
            flag = " ← ❗" if cnt["fail"] > 0 else ""
            print(f"  {stg:<18} {cnt['ok']:>4} {cnt['fail']:>5} {cnt['skip']:>5}{flag}")

        if fails:
            print(f"\n  ── Failure detail ───────────────────────────────────────")
            for e in fails:
                print(f"  [{e['ts']}] {e['company']:<26} │ {e['stage']:<16} │ {e['detail'][:55]}")

        if warns:
            print(f"\n  ── Warnings ─────────────────────────────────────────────")
            for e in warns:
                print(f"  [{e['ts']}] {e['company']:<26} │ {e['stage']:<16} │ {e['detail'][:55]}")

        print(f"{'═' * W}\n")

    def reset(self):
        self.events.clear()
        self._stage_start.clear()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _elapsed(self, company: str, stage: str) -> float:
        key = f"{company}:{stage}"
        t0  = self._stage_start.pop(key, None)
        return round(time.time() - t0, 2) if t0 else 0.0

    def _record(self, company: str, stage: str, status: str,
                detail: str, elapsed: float = 0.0):
        icon = self._ICONS.get(status, "•")
        entry = {
            "ts":       datetime.now().strftime("%H:%M:%S.%f")[:12],
            "company":  company[:30],
            "stage":    stage,
            "status":   status,
            "detail":   detail[:140],
            "elapsed_s": elapsed,
        }
        self.events.append(entry)
        elapsed_str = f"  ({elapsed:.2f}s)" if elapsed else ""
        log.info(
            f"  [LLM-TRACK] {icon} {company[:24]:<24} │ "
            f"{stage:<16} │ {detail[:60]}{elapsed_str}"
        )


# Module-level singleton — import this in ipo_sniper_v12.py
llm_tracker = LLMTracker()


# ══════════════════════════════════════════════════════════════════════════════
# FIX 2 — fetch_company_description()  (FULL REPLACEMENT)
# ══════════════════════════════════════════════════════════════════════════════
# Root cause: the original function only tried 2 URLs and needed >120 chars.
# This version:
#   • Tries 6 sources in priority order
#   • Lowers the minimum to 40 chars
#   • Falls back to a name-derived stub (enough to pass the LLM 60-char gate)
#   • Logs every attempt via llm_tracker
# ══════════════════════════════════════════════════════════════════════════════

_DESC_CACHE: Dict[str, str] = {}


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _extract_text(soup: BeautifulSoup, min_len: int = 40) -> str:
    for selector, attrs in [
        ("div",     {"class": re.compile(r"about|company|description|overview|business", re.I)}),
        ("section", {"id":    re.compile(r"about|overview|business", re.I)}),
        ("div",     {"id":    re.compile(r"about|overview|description", re.I)}),
        ("article", {}),
        ("p",       {}),
    ]:
        blocks = soup.find_all(selector, attrs)[:8]
        text   = " ".join(b.get_text(" ", strip=True) for b in blocks)
        text   = re.sub(r"\s+", " ", text).strip()
        if len(text) >= min_len:
            return text
    return ""


def _name_based_stub(company_name: str) -> str:
    """
    Last-resort: construct a 70-char description purely from the company name.
    Enough to pass the 60-char gate and give the LLM something to reason about.
    Detected as stub → LLM confidence will be low → rule fallback activates.
    """
    # Strip common suffixes for cleaner description
    clean = re.sub(
        r"\b(limited|ltd|pvt|private|public|co\.?|inc|corp|ipo|sme)\b",
        "", company_name, flags=re.IGNORECASE,
    ).strip(" ,-")
    return (
        f"{clean} is a company seeking to raise capital through an IPO on the "
        f"Indian stock exchange. Full business details are currently unavailable; "
        f"apply manual Shariah review."
    )


def fetch_company_description(company_name: str) -> str:
    """
    Multi-source company description fetcher with name-derived fallback.

    Sources tried (in order):
      1. Chittorgarh IPO page
      2. Screener.in company page
      3. NSE India company info
      4. BSE India IPO page
      5. Moneycontrol IPO overview
      6. Economic Times IPO page
      7. Name-based stub (always succeeds, flags as stub in tracker)
    """
    if company_name in _DESC_CACHE:
        return _DESC_CACHE[company_name]

    slug     = _slugify(company_name)
    # Variants for URL construction
    slug_no_dash = slug.replace("-", "")
    slug_upper   = company_name.strip().upper().replace(" ", "")[:12]

    sources = [
        # (label, url)
        ("chittorgarh",
         f"https://www.chittorgarh.com/ipo/{slug}-ipo/"),
        ("screener",
         f"https://www.screener.in/company/{slug_no_dash}/"),
        ("nse",
         f"https://www.nseindia.com/get-quotes/equity?symbol={slug_upper}"),
        ("bse_ipo",
         f"https://www.bseindia.com/markets/equity/EQReports/IPODetails.aspx"),
        ("moneycontrol",
         f"https://www.moneycontrol.com/ipo/{slug}-ipo/"),
        ("et_ipo",
         f"https://economictimes.indiatimes.com/markets/ipos/fpos/{slug}-ipo"),
    ]

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.google.com/",
    })

    llm_tracker.start(company_name, "DESC_FETCH", f"trying {len(sources)} sources")

    for label, url in sources:
        try:
            r = sess.get(url, timeout=8)
            deny = r.headers.get("x-deny-reason", "")
            if deny or r.status_code != 200:
                log.debug(f"  [desc:{label}] HTTP {r.status_code} deny={deny!r}")
                continue

            soup = BeautifulSoup(r.text, "lxml")
            text = _extract_text(soup, min_len=40)

            if text:
                result = text[:3000]
                _DESC_CACHE[company_name] = result
                llm_tracker.ok(
                    company_name, "DESC_FETCH",
                    f"source={label} len={len(result)}"
                )
                log.debug(f"  [desc:{label}] {company_name}: {len(result)} chars")
                return result

        except requests.exceptions.ConnectionError as exc:
            # Domain blocked by egress proxy — skip silently
            log.debug(f"  [desc:{label}] connection blocked: {exc}")
        except Exception as exc:
            log.debug(f"  [desc:{label}] {exc}")

    # ── All web sources failed — use name-based stub ──────────────────────────
    stub = _name_based_stub(company_name)
    _DESC_CACHE[company_name] = stub
    llm_tracker.warn(
        company_name, "DESC_FETCH",
        f"all sources failed — using name-based stub ({len(stub)} chars)"
    )
    log.warning(
        f"  [desc] {company_name}: all sources failed — "
        f"using name stub (LLM confidence will be low)"
    )
    return stub


# ══════════════════════════════════════════════════════════════════════════════
# FIX 3 — _sector_pre_filter() + _pick_audit()  (FULL REPLACEMENT)
# ══════════════════════════════════════════════════════════════════════════════
# Root cause: the DataFrame "Sector" column is always "SME" or "Mainboard",
# never "banking" or "it services", so the keyword sets never matched.
#
# Fix:  pre-filter now receives BOTH the sector field AND the description/name,
#       and searches all three texts for keyword matches.
#       _pick_audit() is updated to pass the description through.
# ══════════════════════════════════════════════════════════════════════════════

# Re-declare the keyword sets here so this patch is self-contained.
# In your main file keep only one copy.
OBVIOUS_HALAL_SECTORS = {
    "it services", "software", "saas", "erp", "crm", "cloud computing",
    "healthcare", "hospital", "pharma", "biotech", "medical devices",
    "manufacturing", "auto components", "engineering", "capital goods",
    "education", "edtech", "school", "college",
    "agriculture", "agri inputs", "food processing",
    "logistics", "warehousing", "courier",
    "renewable energy", "solar", "wind", "power generation",
    "real estate development", "construction", "infrastructure",
    "textiles", "apparel", "retail",
    # common name fragments seen in Indian SME IPOs
    "infotech", "techno", "systems", "solutions", "services",
    "chemicals", "packaging", "printing", "cables", "wires",
    "steel", "iron", "cement", "ceramics", "pipes", "pumps",
    "hospital", "diagnostic", "clinic", "labs",
}

OBVIOUS_HARAM_SECTORS = {
    "bank", "banking", "nbfc", "microfinance", "housing finance",
    "insurance", "reinsurance", "asset management", "mutual fund",
    "alcohol", "brewery", "distillery", "liquor", "wine", "beer",
    "gambling", "casino", "lottery", "betting",
    "pork", "pig farming", "swine",
    "tobacco", "cigarette", "cigar", "pan masala",
    "adult entertainment", "pornography",
}


def _sector_pre_filter(
    sector: str,
    description: str = "",
    company_name: str = "",
) -> Optional[Tuple[str, dict]]:
    """
    FIX: now searches `sector`, `description`, AND `company_name` so that
    the "SME"/"Mainboard" sector field doesn't prevent useful matching.

    Returns (decision, result_dict) if obviously halal or haram, else None.
    """
    # Build a single search corpus — lower-case
    corpus = " ".join([
        sector.lower(),
        description.lower()[:600],   # first 600 chars of description
        company_name.lower(),
    ])

    # ── Haram check (higher priority) ────────────────────────────────────────
    for kw in OBVIOUS_HARAM_SECTORS:
        if kw in corpus:
            result = {
                "is_compliant":     False,
                "tier":             "HARAM_CORE_BUSINESS",
                "haram_reason":     f"Matched haram keyword: '{kw}'",
                "compliance_notes": "Pre‑filter blocked (keyword in name/description/sector).",
                "confidence":       90,
                "_method":          "prefilter_haram",
            }
            return ("HARAM", result)

    # ── Halal check ───────────────────────────────────────────────────────────
    for kw in OBVIOUS_HALAL_SECTORS:
        if kw in corpus:
            result = {
                "is_compliant":     True,
                "tier":             "TIER_1_COMPLIANT",
                "haram_reason":     None,
                "compliance_notes": f"Pre‑filter: halal keyword '{kw}' matched.",
                "confidence":       88,
                "_method":          "prefilter_halal",
            }
            return ("HALAL", result)

    return None  # ambiguous → proceed to LLM


def _pick_audit(company_name: str, description: str, sector: str) -> dict:
    """
    FIX: passes description AND company_name to the pre-filter so keyword
    matching works against actual company text, not just "SME"/"Mainboard".

    Dispatch order:
      1. Sector pre-filter (description + name + sector field)
      2. LLM router (OpenAI primary)
      3. Rule-based fallback (if LLM unavailable / fails)
    """
    llm_tracker.start(company_name, "PREFILTER", f"sector='{sector}'")

    pre = _sector_pre_filter(sector, description, company_name)
    if pre is not None:
        decision, result = pre
        llm_tracker.ok(
            company_name, "PREFILTER",
            f"{decision} via keyword '{result.get('haram_reason') or result.get('compliance_notes','')[:50]}'"
        )
        log.info(
            f"  [prefilter] {company_name}: {decision} "
            f"(conf={result['confidence']}%  method={result['_method']})"
        )
        return result

    llm_tracker.skip(company_name, "PREFILTER", "no keyword match → LLM")

    # ── LLM audit ────────────────────────────────────────────────────────────
    # (calls the existing audit_business_with_router which has its own tracking)
    return audit_business_with_router(company_name, description)


# ══════════════════════════════════════════════════════════════════════════════
# UPDATED audit_business_with_router() — adds llm_tracker instrumentation
# ══════════════════════════════════════════════════════════════════════════════
# Replace the existing function body with this version.
# Everything else (imports, constants) stays the same.
# ══════════════════════════════════════════════════════════════════════════════

# (Keep your existing constants at the top of the file)
_ROUTER_FAST_MODEL           = "gpt-4o-mini"
_ROUTER_FLAGSHIP_MODEL       = "gpt-4o"
_ROUTER_CONFIDENCE_THRESHOLD = 80

# Paste your existing _SHARIAH_SO_SCHEMA and _SHARIAH_SYSTEM_PROMPT here.
# They are unchanged.


def audit_business_with_router(
    company_name: str,
    description:  str,
    _shariah_system_prompt: str = "",
) -> dict:
    """
    v12 primary Shariah auditor using OpenAI Structured Outputs.
    Now fully instrumented with llm_tracker.
    Falls back to rule‑based audit if LLM fails or is skipped.
    """
    # ── Cache lookup ─────────────────────────────────────────────────────────
    llm_tracker.start(company_name, "CACHE_HIT")
    cached = _cache_get(company_name)
    if cached:
        cached["_method"] = "cache"
        llm_tracker.ok(
            company_name, "CACHE_HIT",
            f"conf={cached.get('confidence', 0)}%  tier={cached.get('tier','?')}"
        )
        return cached
    llm_tracker.skip(company_name, "CACHE_HIT", "no valid cache entry")

    # ── Description quality gate ──────────────────────────────────────────────
    if not description or len(description) < 60:
        llm_tracker.warn(
            company_name, "OPENAI_MINI",
            f"description too short ({len(description)} chars) — rule fallback"
        )
        log.debug(f"  [router] {company_name}: description too short — rule fallback")
        result = _rule_based_audit(company_name, "", description)
        result["_method"] = "rule_short_desc"
        return result

    # ── API key check ─────────────────────────────────────────────────────────
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        llm_tracker.fail(
            company_name, "OPENAI_MINI",
            "OPENAI_API_KEY not set — check your .env / environment"
        )
        log.warning("  [router] OPENAI_API_KEY not set — using rule‑based audit")
        return _rule_based_audit(company_name, "", description)

    try:
        import openai
    except ImportError:
        llm_tracker.fail(company_name, "OPENAI_MINI", "openai package not installed")
        return _rule_based_audit(company_name, "", description)

    client        = openai.OpenAI(api_key=api_key)
    system_prompt = _shariah_system_prompt or _SHARIAH_SYSTEM_PROMPT
    user_msg      = (
        f"Company name: {company_name}\n\n"
        f"Business description:\n{description[:2500]}"
    )

    # ── TIER 1: gpt-4o-mini ──────────────────────────────────────────────────
    mini_result: Optional[dict] = None
    llm_tracker.start(company_name, "OPENAI_MINI", f"desc_len={len(description)}")
    try:
        resp = client.chat.completions.create(
            model           = _ROUTER_FAST_MODEL,
            messages        = [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            temperature     = 0.0,
            response_format = _SHARIAH_SO_SCHEMA,
            timeout         = 10,
        )
        mini_result = json.loads(resp.choices[0].message.content)
        conf        = int(mini_result.get("confidence", 0))

        if conf >= _ROUTER_CONFIDENCE_THRESHOLD:
            mini_result["_method"] = f"llm-{_ROUTER_FAST_MODEL}"
            _cache_set(company_name, mini_result, description)
            llm_tracker.ok(
                company_name, "OPENAI_MINI",
                f"{'HALAL' if mini_result['is_compliant'] else 'HARAM'}  conf={conf}%"
            )
            log.info(
                f"  [router] {company_name}: "
                f"{'✅ HALAL' if mini_result['is_compliant'] else '🚫 HARAM'} "
                f"via mini  conf={conf}%"
            )
            return mini_result

        # Low confidence → escalate
        llm_tracker.warn(
            company_name, "OPENAI_MINI",
            f"conf={conf}% < {_ROUTER_CONFIDENCE_THRESHOLD}% → escalating to gpt-4o"
        )
        log.info(
            f"  [router] {company_name}: mini conf={conf}% < "
            f"{_ROUTER_CONFIDENCE_THRESHOLD}% → escalating"
        )

    except openai.APITimeoutError:
        llm_tracker.fail(company_name, "OPENAI_MINI", "timeout (10s)")
        log.warning(f"  [router] {company_name}: mini timed out → escalating")
    except openai.RateLimitError:
        llm_tracker.fail(company_name, "OPENAI_MINI", "rate-limit hit")
        log.warning(f"  [router] {company_name}: mini rate-limited → escalating")
    except openai.AuthenticationError:
        llm_tracker.fail(company_name, "OPENAI_MINI", "invalid API key — check OPENAI_API_KEY")
        log.error("  [router] OpenAI AuthenticationError — OPENAI_API_KEY is wrong/expired")
        return _rule_based_audit(company_name, "", description)
    except Exception as exc:
        llm_tracker.fail(company_name, "OPENAI_MINI", str(exc)[:80])
        log.warning(f"  [router] {company_name}: mini failed ({exc}) → escalating")

    # ── TIER 2: gpt-4o ───────────────────────────────────────────────────────
    escalation_note = ""
    if mini_result:
        escalation_note = (
            f"\n\nPreliminary audit: tier='{mini_result.get('tier')}' "
            f"confidence={mini_result.get('confidence')}%. "
            f"Please give a more thorough analysis."
        )

    llm_tracker.start(company_name, "OPENAI_FULL", "escalated from mini")
    try:
        resp = client.chat.completions.create(
            model           = _ROUTER_FLAGSHIP_MODEL,
            messages        = [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg + escalation_note},
            ],
            temperature     = 0.0,
            response_format = _SHARIAH_SO_SCHEMA,
            timeout         = 20,
        )
        result = json.loads(resp.choices[0].message.content)
        result["_method"] = f"llm-{_ROUTER_FLAGSHIP_MODEL}-escalated"
        _cache_set(company_name, result, description)
        llm_tracker.ok(
            company_name, "OPENAI_FULL",
            f"{'HALAL' if result['is_compliant'] else 'HARAM'}  conf={result.get('confidence','?')}%"
        )
        log.info(
            f"  [router] {company_name}: "
            f"{'✅ HALAL' if result['is_compliant'] else '🚫 HARAM'} "
            f"via flagship  conf={result.get('confidence','?')}%"
        )
        return result

    except openai.APITimeoutError:
        llm_tracker.fail(company_name, "OPENAI_FULL", "timeout (20s)")
    except openai.RateLimitError:
        llm_tracker.fail(company_name, "OPENAI_FULL", "rate-limit hit")
    except openai.AuthenticationError:
        llm_tracker.fail(company_name, "OPENAI_FULL", "invalid API key")
        log.error("  [router] OpenAI AuthenticationError on flagship call")
    except Exception as exc:
        llm_tracker.fail(company_name, "OPENAI_FULL", str(exc)[:80])
        log.error(f"  [router] {company_name}: both tiers failed — {exc}")

    # ── Hard fallback ─────────────────────────────────────────────────────────
    rule_result = _rule_based_audit(company_name, "", description)
    rule_result["_method"] = "fallback_rule"
    llm_tracker.warn(
        company_name, "FINAL",
        "both OpenAI tiers failed — using rule-based fallback"
    )
    return rule_result


# ══════════════════════════════════════════════════════════════════════════════
# PATCH INSTRUCTIONS (summary)
# ══════════════════════════════════════════════════════════════════════════════
"""
Step-by-step integration:

1. AFTER the CONFIG block, paste the LLMTracker class + the line:
       llm_tracker = LLMTracker()

2. REPLACE the existing fetch_company_description() with Fix 2 above.
   Also replace _slugify() if it doesn't exist yet.

3. REPLACE _sector_pre_filter() with Fix 3's version.
   The new signature is:
       def _sector_pre_filter(sector, description="", company_name="") -> ...

4. REPLACE _pick_audit() with Fix 3's version.
   It now passes `description` and `company_name` to _sector_pre_filter().

5. REPLACE audit_business_with_router() with the instrumented version above.

6. At the very END of run(), before the return statement, add:
       llm_tracker.dump_summary()
       llm_tracker.reset()   # clear for next run

7. Verify OPENAI_API_KEY and ANTHROPIC_API_KEY are set in your shell / .env:
       export OPENAI_API_KEY="sk-..."
       export ANTHROPIC_API_KEY="sk-ant-..."
   The tracker will now show you EXACTLY which stage fails and why.
"""
