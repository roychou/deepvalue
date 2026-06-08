"""
Live universe loader (forward path) — the survivorship-immune analogue of the backtest's
Sharadar/FMP roster.

A forward run can only trade what exists NOW, so survivorship bias is impossible and we need
no paid vendor: the universe is driven straight off SEC EDGAR's free daily index — the
companies that just filed a 10-K / 10-Q. This is also the *right* trigger for the Tedium
Premium, because the MD&A Deterioration Lead is a filing-driven signal: a name becomes
interesting exactly when it files a fresh annual/quarterly report to diff against last year's.

The loader stays cheap and pure — it ENUMERATES recent filers (one network call per filing
day, deduped). Sector exclusion, liquidity floors, and the actual screen happen downstream in
the forward session, which already touches each candidate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from deepvalue.ingest.edgar import EdgarError, _get_text, _load_cik_map

SEC_WWW_BASE = "https://www.sec.gov"


@dataclass(frozen=True)
class Filer:
    """One filing event from the EDGAR daily index."""
    cik: str            # numeric CIK (no zero-pad)
    company: str        # company name as EDGAR records it
    form: str           # e.g. "10-K", "10-Q"
    filed: str          # filing date, YYYY-MM-DD (point-in-time: this IS the as-of)
    accession: str      # dash-stripped accession, ready for the Archives URL
    txt_path: str       # the submission .txt path from the index


def _quarter(d: date) -> int:
    return (d.month - 1) // 3 + 1


def _daily_index_url(d: date) -> str:
    """The pipe-delimited master index for a single filing day. 404s on
    weekends/holidays (no filings) — callers skip those."""
    return (f"{SEC_WWW_BASE}/Archives/edgar/daily-index/"
            f"{d.year}/QTR{_quarter(d)}/master.{d:%Y%m%d}.idx")


def _parse_master_idx(text: str, forms: tuple[str, ...]) -> list[Filer]:
    """Parse a daily master.idx. Data rows are 'CIK|Company|Form|Date Filed|Filename';
    header/preamble lines lack exactly five pipe fields and are skipped."""
    out: list[Filer] = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik, company, form, filed, fname = (p.strip() for p in parts)
        if form not in forms or not cik.isdigit():
            continue
        # Normalize Date Filed to ISO YYYY-MM-DD — the daily index emits YYYYMMDD, but
        # downstream point-in-time cuts (prices, fundamentals) compare ISO strings. A
        # non-ISO date here would silently defeat the point-in-time discipline.
        digits = filed.replace("-", "")
        if len(digits) == 8 and digits.isdigit():
            filed = f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
        # edgar/data/<cik>/<accession>.txt -> accession (dash-stripped for Archives URLs)
        acc = fname.rsplit("/", 1)[-1].removesuffix(".txt")
        out.append(Filer(cik=cik, company=company, form=form, filed=filed,
                         accession=acc.replace("-", ""), txt_path=fname))
    return out


def recent_filers(since: str, until: str | None = None,
                  forms: tuple[str, ...] = ("10-K", "10-Q")) -> list[Filer]:
    """Every `forms` filing on EDGAR from `since` to `until` inclusive (YYYY-MM-DD;
    `until` defaults to today). Walks the daily index day by day — non-filing days
    (weekends/holidays) 404 and are silently skipped. Deduped on (cik, accession),
    newest filing day first."""
    start = date.fromisoformat(since)
    end = date.fromisoformat(until) if until else date.today()
    seen: set[tuple[str, str]] = set()
    filers: list[Filer] = []
    d = start
    while d <= end:
        try:
            text = _get_text(_daily_index_url(d))
        except EdgarError:
            d += timedelta(days=1)
            continue  # weekend/holiday/not-yet-published — no filings that day
        for f in _parse_master_idx(text, forms):
            key = (f.cik, f.accession)
            if key not in seen:
                seen.add(key)
                filers.append(f)
        d += timedelta(days=1)
    filers.sort(key=lambda f: f.filed, reverse=True)
    return filers


def recent_filers_back(days_back: int = 7,
                       forms: tuple[str, ...] = ("10-K", "10-Q")) -> list[Filer]:
    """Convenience: the last `days_back` calendar days of filers up to today. The weekly
    forward scan's default entry point (days_back=7)."""
    since = (date.today() - timedelta(days=days_back)).isoformat()
    return recent_filers(since, forms=forms)


def cik_to_ticker_map() -> dict[str, str]:
    """Reverse of SEC's ticker->CIK map: numeric-CIK -> ticker. Downstream needs a ticker
    to price the name (IBKR) and pull its fundamentals. CIKs absent from the map (most
    micro-caps without a current common-stock ticker) simply won't resolve — the session
    drops them, which is correct (un-priceable names aren't tradeable)."""
    out: dict[str, str] = {}
    for ticker, cik in _load_cik_map().items():
        out.setdefault(str(int(cik)), ticker)  # first (canonical) ticker wins for a CIK
    return out
