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
        notify.notify("L7 calibration — no data", "No weekly books emitted yet; nothing to score.")
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


def _summary(rep: dict) -> str:
    lines = [f"Books: {rep['n_books']} over {rep['span'][0]}..{rep['span'][1]} | "
             f"outcomes {rep['n_realized']}/{rep['n_outcomes']} matured",
             "Decay alarm — forward IC of -deterioration (positive = edge holds):"]
    matured = False
    for h in rep["horizons"]:
        ic, n = h["ic_neg_deterioration"], h["n"]
        if ic is None or n < MIN_N:
            lines.append(f"  {h['horizon_days']}d: no matured data yet (n={n})")
            continue
        matured = True
        sp = h["spread"]
        spread = "n/a" if sp is None else f"{sp * 100:+.1f}%"
        lines.append(f"  {h['horizon_days']}d: IC={ic:+.3f} (n={n}) | BUY-WATCH spread {spread} "
                     f"({h['n_buy']}/{h['n_watch']})")
    if not matured:
        lines.append("Verdict: rig still young — not enough matured forward returns to judge. Holding.")
    else:
        best = max((h for h in rep["horizons"] if h["ic_neg_deterioration"] is not None and h["n"] >= MIN_N),
                   key=lambda h: h["n"])
        ic = best["ic_neg_deterioration"]
        lines.append("Verdict: " + ("edge INTACT out-of-sample (positive IC)." if ic > 0.02
                     else "⚠️ DECAY WATCH — forward IC not positive; investigate before trusting L3."))
        lines.append("(Small n — noisy; read as a trend, not significance.)")
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
        notify.notify("⚠️ L7 calibration FAILED", f"{type(e).__name__}: {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
