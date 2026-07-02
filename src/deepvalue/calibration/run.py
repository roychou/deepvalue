"""
L7 — calibration / decay-alarm runner (monthly cron, spec §11).

Reads the accumulated weekly books (data/forward/book_*.json), fetches FORWARD prices for every
booked name from the same IBKR paper gateway the weekly run uses, and checks OUT-OF-SAMPLE whether
the MD&A Deterioration Lead still predicts forward returns — the founding-risk decay alarm. Writes
a calibration report artifact + sends a Telegram summary. No LLM spend (pure computation).

Until enough forward time accrues (the live rig only just started), most horizons have no matured
returns yet — the runner says so honestly rather than inventing a number.

    python -m deepvalue.calibration.run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date
from pathlib import Path

from deepvalue.calibration.metrics import deterioration_ic, verdict_spread
from deepvalue.calibration.outcomes import FORWARD_DIR, load_books, score_outcomes
from deepvalue.forward import ibkr_prices, notify

log = logging.getLogger("tedium.calibration")
HORIZONS = (63, 126, 252)
MIN_N = 3   # below this an IC is meaningless


async def _run() -> int:
    books = load_books()
    if not books:
        log.info("no books yet — nothing to calibrate")
        notify.notify("L7 calibration — no data",
                      "The monthly decay alarm ran, but no weekly books have been emitted yet, "
                      "so there is no track record to score. Nothing is wrong — this resolves "
                      "itself once the weekly screen has produced its first book.")
        return 0
    as_ofs = sorted(b.get("as_of") for b in books if b.get("as_of"))
    tickers = sorted({c["ticker"] for b in books for c in b.get("book", [])})
    log.info("calibrating %d books (%s..%s), %d distinct names", len(books), as_ofs[0], as_ofs[-1], len(tickers))

    # forward prices from IBKR — lookback must reach the OLDEST book through today
    lookback = min(1000, (date.today() - date.fromisoformat(as_ofs[0])).days + 30)
    ib = await ibkr_prices.connect()
    try:
        ibkr_prices.assert_paper_ready(ib)
        prices = await ibkr_prices.fetch_prices_for(ib, tickers, lookback_days=max(lookback, 60))
    finally:
        ib.disconnect()

    outcomes = score_outcomes(books, prices, horizons=HORIZONS)
    n_realized = sum(1 for o in outcomes if o.realized)
    per_h = []
    for h in HORIZONS:
        ic = deterioration_ic(books, prices, horizon_days=h)
        sp = verdict_spread(books, prices, horizon_days=h)
        per_h.append({"horizon_days": h, **ic, **{k: sp[k] for k in ("n_buy", "n_watch", "spread")}})

    today = date.today().isoformat()
    report = {"as_of": today, "n_books": len(books), "span": [as_ofs[0], as_ofs[-1]],
              "n_outcomes": len(outcomes), "n_realized": n_realized, "horizons": per_h}
    out = FORWARD_DIR / f"calibration_{today}.json"
    out.write_text(json.dumps(report, indent=2))
    log.info("wrote %s", out)

    notify.notify(f"📉 L7 calibration — {today}", _summary(report))
    return 0


_MONTHS = {63: "~3 months", 126: "~6 months", 252: "~12 months"}


def _summary(rep: dict) -> str:
    """Plain-language monthly readout. Written for the phone reader: say what the decay alarm
    is, what 'matured' means, and what the numbers say — not just IC/n shorthand."""
    lines = [
        "This is the monthly decay alarm: it checks whether the language-deterioration signal "
        "is still predicting forward returns on the names the live rig actually booked — "
        "a true out-of-sample test of the edge.",
        "",
        f"Track record so far: {rep['n_books']} weekly books from {rep['span'][0]} to "
        f"{rep['span'][1]}. Of {rep['n_outcomes']} booked positions x horizons, "
        f"{rep['n_realized']} have matured (enough trading days have passed to measure the "
        f"forward return).",
        "",
        "Per horizon (positive correlation = deteriorating names underperformed = edge holds):",
    ]
    matured = False
    for h in rep["horizons"]:
        ic, n, label = h["ic_neg_deterioration"], h["n"], _MONTHS.get(
            h["horizon_days"], f"{h['horizon_days']} trading days")
        if ic is None or n < MIN_N:
            lines.append(f"• {label}: not enough positions old enough to measure yet "
                         f"({n} matured; need {MIN_N}+).")
            continue
        matured = True
        sp = h["spread"]
        spread = ("the BUY-vs-WATCH return gap isn't measurable yet" if sp is None else
                  f"BUYs outperformed WATCHes by {sp * 100:+.1f}% "
                  f"({h['n_buy']} BUYs vs {h['n_watch']} WATCHes)")
        lines.append(f"• {label}: correlation between deterioration score and forward return "
                     f"is {ic:+.3f} across {n} matured positions; {spread}.")
    lines.append("")
    if not matured:
        lines.append("Verdict: the rig is still too young to judge — most positions haven't "
                     "been held long enough to measure a forward return. No action needed; "
                     "this alarm re-runs monthly and will say so when real numbers exist.")
    else:
        best = max((h for h in rep["horizons"] if h["ic_neg_deterioration"] is not None and h["n"] >= MIN_N),
                   key=lambda h: h["n"])
        ic = best["ic_neg_deterioration"]
        lines.append("Verdict: " + (
            "the edge looks INTACT out-of-sample — deteriorating-language names are "
            "underperforming, as the backtest predicted." if ic > 0.02
            else "⚠️ DECAY WATCH — the signal is NOT predicting underperformance in live "
                 "forward data. Investigate before trusting the language check for BUYs."))
        lines.append("(Sample is still small, so read this as a trend, not proof.)")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep request URLs (tokens) out of logs
    argparse.ArgumentParser(description="L7 calibration / decay-alarm runner").parse_args()
    try:
        raise SystemExit(asyncio.run(_run()))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — a calibration failure must alert, not silently die
        logging.getLogger("tedium.calibration").exception("calibration failed")
        notify.notify("⚠️ L7 calibration FAILED",
                      f"The monthly decay alarm crashed before producing a report — no "
                      f"calibration was recorded this month.\n\nError: {type(e).__name__}: {e}\n\n"
                      f"Check the container logs: docker logs tediumpremium-app.")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
