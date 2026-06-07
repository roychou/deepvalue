"""
SEC EDGAR data source: point-in-time fundamentals from XBRL company facts.

Replaces FMP as the fundamentals source (FMP free-tier capped statement history
at 5 and gated quarterly/constituents). EDGAR is free, has deep history, and is
natively point-in-time — every fact carries a real `filed` date. See
notes/edgar-design.md.

`build_filings_history(ticker)` returns the same shape the rest of the system
already consumes (the list of per-quarter raw dicts that `get_filings_history`
produced from FMP), so `get_fundamentals_as_of` is unchanged — only the source
underneath swaps.

XBRL shape notes (confirmed against MSFT):
- Flow metrics (revenue, net income, EPS) are *duration* facts with start+end.
  companyfacts mixes true quarters (~90-day span) with YTD cumulations (183/274-
  day) and the annual FY (~365). We keep the ~90-day quarters and derive Q4 from
  the annual minus the three reported quarters.
- Stock metrics (equity, debt) are *instant* facts (end only, no start); we take
  the value at each quarter's period-end date.
- Point-in-time: each quarter's `report_date` is the earliest `filed` among its
  facts (when it was first published). Restatements filed later are not applied
  (we use values as originally filed — the correct PIT semantic for v1).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from deepvalue.ingest.fundamentals import calc_debt_equity, calc_growth_yoy, calc_margin

logger = logging.getLogger(__name__)

SEC_DATA_BASE = "https://data.sec.gov"
SEC_WWW_BASE = "https://www.sec.gov"
TIMEOUT_SECONDS = 30

CACHE_DIR = Path("data/cache/edgar")
REF_DIR = Path("data/reference")

# Revenue is tagged differently across eras/filers — try in priority order.
# (Energy/utility filers like APA report gross "IncludingAssessedTax"; without it
# their revenue extraction yields zero quarters and the whole name silently drops.)
REVENUE_CONCEPTS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "RevenuesNetOfInterestExpense",  # bank/broker presentation
]
# Total debt: prefer the combined tag; fall back to current + noncurrent components.
DEBT_TOTAL_CONCEPTS = ["LongTermDebt", "DebtLongtermAndShorttermCombinedAmount"]
DEBT_NONCURRENT_CONCEPTS = ["LongTermDebtNoncurrent"]
DEBT_CURRENT_CONCEPTS = ["LongTermDebtCurrent", "DebtCurrent"]

_QUARTER_MIN_DAYS, _QUARTER_MAX_DAYS = 80, 100
_ANNUAL_MIN_DAYS, _ANNUAL_MAX_DAYS = 350, 380


class EdgarError(RuntimeError):
    """Raised when EDGAR returns an error or unexpected shape."""


def _user_agent() -> str:
    ua = os.environ.get("EDGAR_USER_AGENT")
    if not ua:
        raise EdgarError(
            "EDGAR_USER_AGENT is not set. SEC requires a User-Agent with contact info "
            "(e.g. 'parley-research you@example.com'). Add it to .env."
        )
    return ua


_SEC_MIN_INTERVAL = 0.15   # SEC fair-access cap ~10 req/s; stay well under to avoid 10-min blocks
_sec_last_call = 0.0


def _sec_throttle() -> None:
    """Block briefly so successive SEC network calls stay under the rate cap. Applied
    only inside the network helpers, so cached reads (the common case) pay nothing."""
    global _sec_last_call
    wait = _sec_last_call + _SEC_MIN_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _sec_last_call = time.monotonic()


def _get(url: str) -> Any:
    _sec_throttle()
    try:
        resp = httpx.get(url, headers={"User-Agent": _user_agent()}, timeout=TIMEOUT_SECONDS)
    except httpx.RequestError as e:
        raise EdgarError(f"EDGAR request failed: {e}") from e
    if resp.status_code != 200:
        raise EdgarError(f"EDGAR returned {resp.status_code} for {url}: {resp.text[:200]}")
    return resp.json()


# ==========================================
# TICKER -> CIK (SEC's authoritative map)
# ==========================================


# SEC regenerates company_tickers.json continuously; a local copy older than this
# is refetched on next use. A stale copy silently misses newly-added/changed current
# members (we hit exactly this — CTRA/DAY/HOLX were absent). The TTL is the refresh
# *trigger*: no cron, the next lookup after expiry self-heals.
_CIK_MAP_MAX_AGE = timedelta(days=30)


def _cik_map_path() -> Path:
    return REF_DIR / "company_tickers.json"


def _ensure_cik_map_fresh() -> None:
    """(Re)download the SEC ticker->CIK map if absent or past the TTL.

    On a failed refresh we keep the existing copy (an offline run or an SEC
    hiccup shouldn't break lookups); only a missing-and-unfetchable map raises."""
    path = _cik_map_path()
    if path.exists():
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
        if age <= _CIK_MAP_MAX_AGE:
            return
    try:
        REF_DIR.mkdir(parents=True, exist_ok=True)
        data = _get(f"{SEC_WWW_BASE}/files/company_tickers.json")
        path.write_text(json.dumps(data))
        _load_cik_map.cache_clear()
        logger.info("Refreshed SEC ticker->CIK map (company_tickers.json)")
    except EdgarError as e:
        if not path.exists():
            raise
        logger.warning(f"SEC CIK map refresh failed ({e}); using cached copy")


@lru_cache(maxsize=1)
def _load_cik_map() -> dict[str, str]:
    """Returns {TICKER: zero-padded 10-digit CIK} from the on-disk SEC map.
    Cached in-process; _ensure_cik_map_fresh clears it on a successful refresh."""
    data = json.loads(_cik_map_path().read_text())
    out: dict[str, str] = {}
    for row in data.values():
        out[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
    return out


@lru_cache(maxsize=1)
def _load_fmp_cik_map() -> dict[str, str]:
    """Ticker -> CIK from the FMP-derived historical map (data/reference/
    ticker_cik_historical.json). Fallback for names the SEC's current-only
    company_tickers map misses: drift (e.g. CTRA/DAY/HOLX) and delisted names
    (whose XBRL still lives on EDGAR by permanent CIK). Values are 10-digit
    strings or null; nulls are skipped. Empty dict if the file isn't present."""
    path = REF_DIR / "ticker_cik_historical.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {t.upper(): cik for t, cik in data.items() if cik}


def ticker_to_cik(ticker: str) -> str:
    _ensure_cik_map_fresh()
    t = ticker.upper()
    # SEC map is authoritative + broad for current names; the FMP historical map is
    # the fallback for what SEC can't have (delisted names) and any current-map drift.
    cik = _load_cik_map().get(t) or _load_fmp_cik_map().get(t)
    if cik is None:
        raise EdgarError(f"No CIK found for ticker {ticker} in SEC or FMP CIK maps")
    return cik


# ==========================================
# COMPANY FACTS (cached)
# ==========================================


def _submissions(ticker: str) -> dict:
    """Cached SEC submissions blob for a ticker (lean — far cheaper than companyfacts)."""
    cik = ticker_to_cik(ticker)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    cache_path = CACHE_DIR / f"{ticker.upper()}_submissions_{today}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    data = _get(f"{SEC_DATA_BASE}/submissions/CIK{cik}.json")
    cache_path.write_text(json.dumps(data))
    return data


def _parse_recent_filings(recent: dict, forms: tuple[str, ...]) -> list[dict]:
    """Shape the SEC `filings.recent` blob into our filing records, newest first."""
    out = [
        {
            "form": form,
            "filed": filed,
            "accession": acc.replace("-", ""),
            "primary_document": doc,
        }
        for form, filed, acc, doc in zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
            recent.get("primaryDocument", []),
        )
        if form in forms
    ]
    out.sort(key=lambda f: f["filed"], reverse=True)
    return out


def filings_by_cik(cik: str, forms: tuple[str, ...] = ("10-K",)) -> list[dict]:
    """Filings for a CIK directly — the survivorship-correct path for the L3 backtest.

    Delisted tickers don't resolve in the current SEC ticker map (and reuse would point
    to the wrong company), but a permanent CIK does — and the price-grab manifest already
    carries each name's CIK. Keyed by CIK (zero-padded to 10 digits) + today (the recent
    blob is mutable). Returns [] on any fetch error so one bad name can't abort a sweep.
    """
    cik10 = str(cik).zfill(10)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"CIK{cik10}_submissions_{date.today():%Y%m%d}.json"
    try:
        if cache_path.exists():
            data = json.loads(cache_path.read_text())
        else:
            data = _get(f"{SEC_DATA_BASE}/submissions/CIK{cik10}.json")
            cache_path.write_text(json.dumps(data))
    except EdgarError as e:
        logger.warning(f"filings_by_cik({cik10}) failed: {e}")
        return []
    return _parse_recent_filings(data.get("filings", {}).get("recent", {}), forms)


def filing_doc_url_by_cik(cik: str, accession: str, primary_document: str) -> str:
    """Archives URL for a filing document, keyed by CIK (no ticker resolution)."""
    return f"{SEC_WWW_BASE}/Archives/edgar/data/{int(cik)}/{accession}/{primary_document}"


# SIC is essentially static, so cache it PERMANENTLY in one small map (cik10 -> sic|"NA")
# rather than re-hitting SEC every run — re-fetching hundreds of names per screen trips
# SEC's ~10-minute rate block, which silently leaks banks into the screen.
_SIC_MAP_PATH = REF_DIR / "cik_sic.json"
_sic_map: dict[str, str] | None = None


def _load_sic_map() -> dict[str, str]:
    global _sic_map
    if _sic_map is None:
        try:
            _sic_map = json.loads(_SIC_MAP_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            _sic_map = {}
    return _sic_map


def flush_sic_map() -> None:
    if _sic_map is not None:
        REF_DIR.mkdir(parents=True, exist_ok=True)
        _SIC_MAP_PATH.write_text(json.dumps(_sic_map))


def company_sic(cik: str) -> str | None:
    """SIC code for a CIK, from a persistent map (fetched once from SEC submissions, then
    reused forever). Used for sector exclusions (banks/insurers/blank-check) the screen
    can't infer from financials. Returns None only if SEC can't be reached at all."""
    cik10 = str(cik).zfill(10)
    smap = _load_sic_map()
    if cik10 in smap:
        v = smap[cik10]
        return None if v == "NA" else v
    # opportunistically reuse a same-day submissions cache (from filings_by_cik) if present
    cache_path = CACHE_DIR / f"CIK{cik10}_submissions_{date.today():%Y%m%d}.json"
    data = None
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            data = None
    for attempt in range(3):
        if data is not None:
            break
        try:
            data = _get(f"{SEC_DATA_BASE}/submissions/CIK{cik10}.json")
        except EdgarError:
            time.sleep(0.8 * (attempt + 1))
    if data is None:
        return None                              # SEC unreachable — do NOT cache the miss
    sic = data.get("sic")
    smap[cik10] = str(sic) if sic else "NA"
    flush_sic_map()
    return str(sic) if sic else None


# Excluded SIC ranges (spec §10 / policy: banks, insurers, blank-check — negative-WC or
# book-value businesses where the value/trap metrics misfire).
def is_excluded_sector(sic: str | None) -> bool:
    if not sic or not sic.isdigit():
        return False
    n = int(sic)
    return (6020 <= n <= 6300        # depository / non-depository credit (banks)
            or 6310 <= n <= 6411     # insurance
            or n in (6712, 6719)     # bank / holding companies (how most US banks file)
            or n == 6770)            # blank checks


def recent_filings(ticker: str, forms: tuple[str, ...] = ("10-Q", "10-K")) -> list[dict]:
    """Structured recent filings, newest first: {form, filed, accession, primary_document}.

    `accession` is dash-stripped (ready for the Archives URL). Used by the sentiment
    specialist to locate the actual filing document.
    """
    recent = _submissions(ticker).get("filings", {}).get("recent", {})
    forms_list = recent.get("form", [])
    filed_list = recent.get("filingDate", [])
    acc_list = recent.get("accessionNumber", [])
    doc_list = recent.get("primaryDocument", [])
    out = [
        {
            "form": form,
            "filed": filed,
            "accession": acc.replace("-", ""),
            "primary_document": doc,
        }
        for form, filed, acc, doc in zip(forms_list, filed_list, acc_list, doc_list)
        if form in forms
    ]
    out.sort(key=lambda f: f["filed"], reverse=True)
    return out


def recent_filing_dates(ticker: str, forms: tuple[str, ...] = ("10-Q", "10-K")) -> list[str]:
    """Sorted filing dates (YYYY-MM-DD) for the given forms — the event screen's "did
    this name just file?" check. Derived from the same cached submissions blob.

    Returns [] for a ticker that doesn't resolve to a CIK (neither SEC nor FMP map):
    the screen runs over the whole index, so an unresolvable name must drop out
    quietly rather than abort the run (it simply never registers as a fresh filer)."""
    try:
        return sorted(f["filed"] for f in recent_filings(ticker, forms))
    except EdgarError:
        return []


def _get_text(url: str) -> str:
    """GET a URL and return raw text (filing documents are HTML, not JSON)."""
    _sec_throttle()
    try:
        resp = httpx.get(url, headers={"User-Agent": _user_agent()}, timeout=TIMEOUT_SECONDS)
    except httpx.RequestError as e:
        raise EdgarError(f"EDGAR request failed: {e}") from e
    if resp.status_code != 200:
        raise EdgarError(f"EDGAR returned {resp.status_code} for {url}: {resp.text[:200]}")
    return resp.text


def fetch_company_facts(ticker: str) -> dict:
    """Fetch (and cache) the full XBRL company-facts blob for a ticker."""
    cik = ticker_to_cik(ticker)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    cache_path = CACHE_DIR / f"{ticker.upper()}_{today}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    data = _get(f"{SEC_DATA_BASE}/api/xbrl/companyfacts/CIK{cik}.json")
    cache_path.write_text(json.dumps(data))
    return data


# ==========================================
# EXTRACTION HELPERS
# ==========================================


def _span_days(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    y1, m1, d1 = map(int, start.split("-"))
    y2, m2, d2 = map(int, end.split("-"))
    return (date(y2, m2, d2) - date(y1, m1, d1)).days


def _concept_rows(gaap: dict, names: list[str], unit: str = "USD") -> list[dict]:
    """Rows for the first present concept in `names` (priority fallback)."""
    for name in names:
        node = gaap.get(name)
        if node and unit in node.get("units", {}):
            return node["units"][unit]
    return []


def _duration_by_end(rows: list[dict], min_days: int, max_days: int) -> dict[str, dict]:
    """Map period-end date -> earliest-filed duration record within [min,max]-day span.

    Keyed on the fact's own `end` date, NOT fy/fp (those reflect the filing's
    fiscal context, not the fact's period — a known XBRL trap). Earliest `filed`
    gives the value as originally reported (point-in-time).
    """
    out: dict[str, dict] = {}
    for r in rows:
        end = r.get("end")
        if not end:
            continue
        days = _span_days(r.get("start"), end)
        if days is None or not (min_days <= days <= max_days):
            continue
        if end not in out or r.get("filed", "") < out[end].get("filed", ""):
            out[end] = r
    return out


def _quarter_flow(rows: list[dict]) -> dict[str, dict]:
    """End-date -> true-quarter (~90-day) record for a duration (flow) concept."""
    return _duration_by_end(rows, _QUARTER_MIN_DAYS, _QUARTER_MAX_DAYS)


def _annual_flow(rows: list[dict]) -> dict[str, dict]:
    """Fiscal-year-end -> annual (~365-day) record for a duration concept."""
    return _duration_by_end(rows, _ANNUAL_MIN_DAYS, _ANNUAL_MAX_DAYS)


def _best_revenue_rows(gaap: dict) -> list[dict]:
    """Revenue rows from the concept whose quarterly flow reaches the most recent
    period. Filers migrate revenue concepts over time, so the first merely-present
    concept can be one frozen years in the past: NVDA keeps current revenue under
    `Revenues` while its `RevenueFromContract...` tag stops in 2020, and AAPL is the
    reverse. Among concepts that expose true ~90-day quarters, pick the one reaching
    the latest period-end (tie-break: more quarters, then list priority). Falls back
    to the first present concept if none expose quarters, so annual-only filers
    (BA/LW/TDG) still resolve."""
    best_rows: list[dict] = []
    best_key: tuple[str, int, int] | None = None
    for i, name in enumerate(REVENUE_CONCEPTS):
        rows = _concept_rows(gaap, [name])
        if not rows:
            continue
        qf = _quarter_flow(rows)
        if not qf:
            continue
        key = (max(qf), len(qf), -i)  # latest coverage, then richness, then priority
        if best_key is None or key > best_key:
            best_key, best_rows = key, rows
    if best_rows:
        return best_rows
    return _concept_rows(gaap, REVENUE_CONCEPTS)


def _match_prior_year(end: str, available: list[str], tol_days: int = 20) -> str | None:
    """The period-end ~1 year before `end`, nearest within tolerance. Robust to
    52/53-week fiscal calendars whose quarter-ends drift a few days year over year
    (AAPL's Q2 ends 2026-03-28 vs 2025-03-29) — an exact same-MM-DD match silently
    finds no prior period and yields a NaN growth rate. Quarters sit ~90 days apart,
    so a small tolerance can only match the true prior-year period."""
    target = date(*map(int, end.split("-"))) - timedelta(days=365)
    best, best_diff = None, tol_days + 1
    for e in available:
        diff = abs((date(*map(int, e.split("-"))) - target).days)
        if diff < best_diff:
            best, best_diff = e, diff
    return best


def _instant_at(rows: list[dict], end_date: str) -> float | None:
    """Value of an instant (stock) concept at a given period-end date, earliest filed."""
    best: dict | None = None
    for r in rows:
        if r.get("start") is not None or r.get("end") != end_date:
            continue
        if best is None or r.get("filed", "") < best.get("filed", ""):
            best = r
    return float(best["val"]) if best else None


def _total_debt_at(gaap: dict, end_date: str) -> float:
    """Total debt at a period-end: prefer a combined tag, else current + noncurrent."""
    for name in DEBT_TOTAL_CONCEPTS:
        v = _instant_at(_concept_rows(gaap, [name]), end_date)
        if v is not None:
            return v
    nonc = _instant_at(_concept_rows(gaap, DEBT_NONCURRENT_CONCEPTS), end_date)
    curr = _instant_at(_concept_rows(gaap, DEBT_CURRENT_CONCEPTS), end_date)
    if nonc is None and curr is None:
        return float("nan")
    return (nonc or 0.0) + (curr or 0.0)


# ==========================================
# BUILD FILINGS HISTORY (existing consumer shape)
# ==========================================


def build_filings_history(ticker: str) -> list[dict]:
    """Per-period point-in-time fundamentals for `ticker`, most recent first. Shape
    `get_fundamentals_as_of` consumes: {report_date, period_end_date, diluted_eps,
    profit_margin, rev_growth_yoy, debt_to_equity, freq}.

    Primary path: US-GAAP quarterly filers (10-Q/10-K) — Q4 derived from the FY, TTM
    diluted EPS for P/E. Fallback: annual / IFRS / foreign-currency filers (20-F/40-F:
    ASML, PDD, CCEP, Ferrovial, Thomson Reuters), built from annual periods.
    """
    facts = fetch_company_facts(ticker)
    allfacts = facts.get("facts", {})
    gaap = allfacts.get("us-gaap", {})
    if gaap:
        quarterly = _build_quarterly_usgaap(gaap)
        if quarterly:
            return quarterly
    annual = _build_annual_any(allfacts)
    if annual:
        return annual
    if not gaap and "ifrs-full" not in allfacts:
        raise EdgarError(f"No us-gaap or ifrs-full facts for {ticker}")
    return []


def _build_quarterly_usgaap(gaap: dict) -> list[dict]:
    """US-GAAP quarterly fundamentals (the original path): Q4 = FY minus the three
    reported quarters; TTM diluted EPS for P/E; same-quarter-prior-year YoY growth."""
    rev_rows = _best_revenue_rows(gaap)
    ni_rows = _concept_rows(gaap, ["NetIncomeLoss"])
    eps_rows = _concept_rows(gaap, ["EarningsPerShareDiluted"], unit="USD/shares")
    eq_rows = _concept_rows(gaap, ["StockholdersEquity"])

    rev_q = _quarter_flow(rev_rows)   # all keyed by period-end date
    ni_q = _quarter_flow(ni_rows)
    eps_q = _quarter_flow(eps_rows)
    rev_fy = _annual_flow(rev_rows)
    ni_fy = _annual_flow(ni_rows)
    eps_fy = _annual_flow(eps_rows)

    # Derive Q4 (flow) = FY - (the three quarters ending within that fiscal year).
    def _derive_q4(fy_map: dict[str, dict], q_map: dict[str, dict]) -> None:
        for fye, fy_rec in fy_map.items():
            if fye in q_map:
                continue
            # Q1-Q3 of this fiscal year end within ~11 months before fye
            # (a day-span window is robust to 52/53-week fiscal drift).
            parts = [
                q for end, q in q_map.items()
                if (sp := _span_days(end, fye)) is not None and 45 < sp < 330
            ]
            if len(parts) != 3:
                continue
            q_map[fye] = {
                "end": fye,
                "val": fy_rec["val"] - sum(p["val"] for p in parts),
                "filed": fy_rec.get("filed", ""),
            }

    _derive_q4(rev_fy, rev_q)
    _derive_q4(ni_fy, ni_q)
    _derive_q4(eps_fy, eps_q)

    eps_ends = sorted(eps_q)

    def _ttm_eps(end: str) -> float:
        if end not in eps_q:
            return float("nan")
        i = eps_ends.index(end)
        if i < 3:
            return float("nan")
        return sum(float(eps_q[e]["val"]) for e in eps_ends[i - 3:i + 1])

    rev_ends = list(rev_q)
    filings: list[dict] = []
    for end, rev_rec in rev_q.items():
        revenue = float(rev_rec["val"])
        ni_rec = ni_q.get(end)
        prior_end = _match_prior_year(end, rev_ends)
        prior_rev = rev_q.get(prior_end) if prior_end else None
        net_income = float(ni_rec["val"]) if ni_rec else float("nan")
        equity = _instant_at(eq_rows, end)
        equity = equity if equity is not None else float("nan")
        total_debt = _total_debt_at(gaap, end)
        filings.append({
            "report_date": rev_rec.get("filed", ""),
            "period_end_date": end,
            "diluted_eps": _ttm_eps(end),  # TTM, for a meaningful P/E
            "profit_margin": calc_margin(net_income, revenue),
            "rev_growth_yoy": (
                calc_growth_yoy(revenue, float(prior_rev["val"])) if prior_rev else float("nan")
            ),
            "debt_to_equity": calc_debt_equity(total_debt, equity),
            "freq": "quarterly",
        })
    filings.sort(key=lambda f: f["report_date"], reverse=True)
    return filings


# Concept names per taxonomy, for the annual/foreign fallback. Revenue uses the
# us-gaap list above; ifrs-full has its own tags.
_TAXONOMIES: dict[str, dict[str, list[str]]] = {
    "us-gaap": {
        "revenue": REVENUE_CONCEPTS,
        "net_income": ["NetIncomeLoss"],
        "eps": ["EarningsPerShareDiluted"],
        "equity": ["StockholdersEquity"],
    },
    "ifrs-full": {
        "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
        "net_income": ["ProfitLoss"],
        "eps": ["DilutedEarningsLossPerShare", "BasicAndDilutedEarningsLossPerShare"],
        "equity": ["EquityAttributableToOwnersOfParent", "Equity"],
    },
}


def _revenue_rows_any(node: dict, concepts: list[str]) -> tuple[list[dict], str | None]:
    """First present revenue concept's rows + its reporting currency. Prefer USD when
    the filer also reports it (PDD, TRI) so P/E stays valid; else the native currency
    (EUR, etc.)."""
    for name in concepts:
        units = node.get(name, {}).get("units", {})
        if not units:
            continue
        unit = "USD" if "USD" in units else next(iter(units), None)
        if unit:
            return units[unit], unit
    return [], None


def _build_annual_any(allfacts: dict) -> list[dict]:
    """Annual fundamentals for filers without US-GAAP quarterly data — foreign 20-F/40-F
    filers (us-gaap-annual or ifrs-full, often non-USD). Margin and YoY growth are
    currency-free ratios; EPS / P-E are populated only when the filer reports in USD
    (else NaN — never a currency-mismatched P/E). IFRS debt tags vary, so debt/equity
    is best-effort (NaN when not found)."""
    for taxo, spec in _TAXONOMIES.items():
        node = allfacts.get(taxo, {})
        if not node:
            continue
        rev_rows, currency = _revenue_rows_any(node, spec["revenue"])
        rev_fy = _annual_flow(rev_rows)
        if not rev_fy:
            continue
        is_usd = currency == "USD"
        ni_fy = _annual_flow(_concept_rows(node, spec["net_income"], unit=currency))
        eps_fy = _annual_flow(_concept_rows(node, spec["eps"], unit=f"{currency}/shares"))
        eq_rows = _concept_rows(node, spec["equity"], unit=currency)
        rev_ends = list(rev_fy)
        filings: list[dict] = []
        for end, rec in rev_fy.items():
            revenue = float(rec["val"])
            ni_rec = ni_fy.get(end)
            net_income = float(ni_rec["val"]) if ni_rec else float("nan")
            prior_end = _match_prior_year(end, rev_ends)
            prior_rev = rev_fy.get(prior_end) if prior_end else None
            equity = _instant_at(eq_rows, end)
            equity = equity if equity is not None else float("nan")
            debt = _total_debt_at(node, end) if taxo == "us-gaap" else float("nan")
            eps = float(eps_fy[end]["val"]) if (is_usd and end in eps_fy) else float("nan")
            filings.append({
                "report_date": rec.get("filed", ""),
                "period_end_date": end,
                "diluted_eps": eps,  # annual (full-year) EPS; NaN for non-USD reporters
                "profit_margin": calc_margin(net_income, revenue),
                "rev_growth_yoy": (
                    calc_growth_yoy(revenue, float(prior_rev["val"])) if prior_rev else float("nan")
                ),
                "debt_to_equity": calc_debt_equity(debt, equity),
                "freq": "annual",
            })
        filings.sort(key=lambda f: f["report_date"], reverse=True)
        if filings:
            return filings
    return []
