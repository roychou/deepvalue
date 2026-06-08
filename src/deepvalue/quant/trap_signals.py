"""
L1 trap-risk signals (spec §5) — the pre-warning that makes the expensive layers
efficient. Deterministic, pure Python, no LLM. Computed in the same pass as value
metrics so a cheap name is tagged with HOW LIKELY IT'S DYING.

  - Altman Z   : bankruptcy proximity (manufacturing form; market-cap X4).
  - Piotroski F: 0–9 fundamental strength.
  - Beneish M  : earnings-manipulation likelihood.
  - runway     : cash / annual burn (months).
  - dilution   : YoY diluted-share growth.

CASH-FLOW CAVEAT: the FMP grab carries income + balance only. Operating cash flow —
needed for Piotroski's CFO/accruals signals and Beneish TATA — is APPROXIMATED as
NI + D&A − Δ(non-cash working capital). Flagged per-signal (cfo_approx). Going-concern
language and auditor changes come from L0 filing text, not here (left None).
"""
from __future__ import annotations

import math

from deepvalue.ingest.fundamentals_store import Period


def _f(x) -> float | None:
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _g(p: Period | None, field: str) -> float | None:
    return None if p is None else _f(p.get(field))


def _div(a, b):
    return None if (a is None or b is None or b == 0) else a / b


def operating_cfo(p: Period, prior: Period | None) -> tuple[float | None, bool]:
    """Operating cash flow. Prefers the REAL cash-flow statement line (post re-grab);
    falls back to NI + D&A − Δ(non-cash working capital). Returns (cfo, approx?)."""
    real = _f(p.get("operatingCashFlow")) or _f(p.get("netCashProvidedByOperatingActivities"))
    if real is not None:
        return real, False
    ni = _f(p.get("netIncome"))
    da = _f(p.get("depreciationAndAmortization")) or 0.0
    if ni is None:
        return None, True
    if prior is None:
        return ni + da, True
    d_rec = (_f(p.get("netReceivables")) or 0.0) - (_g(prior, "netReceivables") or 0.0)
    d_inv = (_f(p.get("inventory")) or 0.0) - (_g(prior, "inventory") or 0.0)
    d_ap = (_f(p.get("accountPayables")) or 0.0) - (_g(prior, "accountPayables") or 0.0)
    return ni + da - d_rec - d_inv + d_ap, True


def altman_z(p: Period, price: float) -> dict:
    """Z = 1.2·WC/TA + 1.4·RE/TA + 3.3·EBIT/TA + 0.6·MktCap/TL + 1.0·Sales/TA.
    >2.99 safe · 1.81–2.99 grey · <1.81 distress."""
    ta = _f(p.get("totalAssets"))
    if not ta:
        return {"z_score": None, "z_zone": None}
    ca = _f(p.get("totalCurrentAssets")) or 0.0
    cl = _f(p.get("totalCurrentLiabilities")) or 0.0
    re = _f(p.get("retainedEarnings")) or 0.0
    ebit = _f(p.get("ebit")) or 0.0
    tl = _f(p.get("totalLiabilities"))
    sales = _f(p.get("revenue")) or 0.0
    sh = _f(p.get("weightedAverageShsOutDil") or p.get("weightedAverageShsOut"))
    mc = (sh * price) if sh is not None else None
    if tl is None or tl == 0 or mc is None:
        return {"z_score": None, "z_zone": None}
    z = (1.2 * (ca - cl) / ta + 1.4 * re / ta + 3.3 * ebit / ta
         + 0.6 * mc / tl + 1.0 * sales / ta)
    zone = "distress" if z < 1.81 else ("grey" if z < 2.99 else "safe")
    return {"z_score": round(z, 3), "z_zone": zone}


def piotroski_f(p: Period, prior: Period | None) -> dict:
    """0–9; one point per healthy signal. ROA/leverage/turnover deltas need the prior year
    (then those points are skipped). CFO is approximated (cfo_approx flagged)."""
    ta = _f(p.get("totalAssets"))
    ni = _f(p.get("netIncome"))
    cfo, cfo_approx = operating_cfo(p, prior)
    pts, checks = 0, 0

    def pt(cond):
        nonlocal pts, checks
        if cond is None:
            return
        checks += 1
        pts += 1 if cond else 0

    pt(None if (ni is None or not ta) else ni > 0)          # ROA > 0
    pt(None if cfo is None else cfo > 0)                    # CFO > 0
    pt(None if (cfo is None or ni is None) else cfo > ni)   # accruals: CFO > NI
    if prior is not None and ta:
        ta0 = _g(prior, "totalAssets")
        roa = _div(ni, ta); roa0 = _div(_g(prior, "netIncome"), ta0)
        pt(None if (roa is None or roa0 is None) else roa > roa0)            # ΔROA > 0
        lev = _div(_f(p.get("longTermDebt")), ta)
        lev0 = _div(_g(prior, "longTermDebt"), ta0)
        pt(None if (lev is None or lev0 is None) else lev < lev0)            # Δleverage < 0
        cr = _div(_f(p.get("totalCurrentAssets")), _f(p.get("totalCurrentLiabilities")))
        cr0 = _div(_g(prior, "totalCurrentAssets"), _g(prior, "totalCurrentLiabilities"))
        pt(None if (cr is None or cr0 is None) else cr > cr0)               # Δcurrent ratio > 0
        sh = _f(p.get("weightedAverageShsOutDil")); sh0 = _g(prior, "weightedAverageShsOutDil")
        pt(None if (sh is None or sh0 is None) else sh <= sh0 * 1.02)       # no meaningful issuance
        gm = _div(_f(p.get("grossProfit")), _f(p.get("revenue")))
        gm0 = _div(_g(prior, "grossProfit"), _g(prior, "revenue"))
        pt(None if (gm is None or gm0 is None) else gm > gm0)               # Δmargin > 0
        at = _div(_f(p.get("revenue")), ta); at0 = _div(_g(prior, "revenue"), ta0)
        pt(None if (at is None or at0 is None) else at > at0)               # Δasset turnover > 0
    return {"f_score": pts, "f_checks": checks, "cfo_approx": cfo_approx}


def beneish_m(p: Period, prior: Period | None) -> dict:
    """8-variable earnings-manipulation M-score. M > −1.78 → likely manipulator. Needs the
    prior year; TATA uses approximated CFO. Uncomputable components default to neutral."""
    if prior is None:
        return {"m_score": None, "m_flag": None}
    s, s0 = _f(p.get("revenue")), _g(prior, "revenue")
    rec, rec0 = _f(p.get("netReceivables")), _g(prior, "netReceivables")
    gp, gp0 = _f(p.get("grossProfit")), _g(prior, "grossProfit")
    ta, ta0 = _f(p.get("totalAssets")), _g(prior, "totalAssets")
    ca, ca0 = _f(p.get("totalCurrentAssets")), _g(prior, "totalCurrentAssets")
    ppe, ppe0 = _f(p.get("propertyPlantEquipmentNet")), _g(prior, "propertyPlantEquipmentNet")
    dep, dep0 = _f(p.get("depreciationAndAmortization")), _g(prior, "depreciationAndAmortization")
    sga = _f(p.get("sellingGeneralAndAdministrativeExpenses"))
    sga0 = _g(prior, "sellingGeneralAndAdministrativeExpenses")
    ltd, ltd0 = _f(p.get("longTermDebt")), _g(prior, "longTermDebt")
    cl, cl0 = _f(p.get("totalCurrentLiabilities")), _g(prior, "totalCurrentLiabilities")
    if None in (s, s0, ta, ta0) or not s0 or not ta or not ta0:
        return {"m_score": None, "m_flag": None}

    r = _div
    dsri = r(r(rec, s), r(rec0, s0))
    gmi = r(r(gp0, s0), r(gp, s))
    aqi = r(_one_minus(r((ca or 0) + (ppe or 0), ta)), _one_minus(r((ca0 or 0) + (ppe0 or 0), ta0)))
    sgi = r(s, s0)
    depi = r(r(dep0, (dep0 or 0) + (ppe0 or 0)), r(dep, (dep or 0) + (ppe or 0)))
    sgai = r(r(sga, s), r(sga0, s0))
    lvgi = r(r((ltd or 0) + (cl or 0), ta), r((ltd0 or 0) + (cl0 or 0), ta0))
    cfo, cfo_approx = operating_cfo(p, prior)
    ni = _f(p.get("netIncome"))
    tata = r((ni - cfo) if (ni is not None and cfo is not None) else None, ta)
    parts = {"DSRI": dsri, "GMI": gmi, "AQI": aqi, "SGI": sgi, "DEPI": depi,
             "SGAI": sgai, "LVGI": lvgi, "TATA": tata}
    d = {k: (v if v is not None else (0.0 if k == "TATA" else 1.0)) for k, v in parts.items()}
    m = (-4.84 + 0.92 * d["DSRI"] + 0.528 * d["GMI"] + 0.404 * d["AQI"] + 0.892 * d["SGI"]
         + 0.115 * d["DEPI"] - 0.172 * d["SGAI"] + 4.679 * d["TATA"] - 0.327 * d["LVGI"])
    return {"m_score": round(m, 3), "m_flag": m > -1.78, "cfo_approx": cfo_approx}


def _one_minus(x):
    return None if x is None else 1 - x


def cash_runway_months(p: Period) -> float | None:
    """Cash / annual cash burn × 12. Burn = negative operating cash flow (real if grabbed,
    else net income). None if cash-generative or no data."""
    cash = _f(p.get("cashAndShortTermInvestments")) or _f(p.get("cashAndCashEquivalents"))
    burn = (_f(p.get("operatingCashFlow"))
            or _f(p.get("netCashProvidedByOperatingActivities")) or _f(p.get("netIncome")))
    if cash is None or burn is None or burn >= 0:
        return None
    return round(cash / (-burn) * 12.0, 1)


def dilution_yoy(p: Period, prior: Period | None) -> float | None:
    sh = _f(p.get("weightedAverageShsOutDil") or p.get("weightedAverageShsOut"))
    sh0 = _g(prior, "weightedAverageShsOutDil") or _g(prior, "weightedAverageShsOut")
    if sh is None or not sh0:
        return None
    return round(sh / sh0 - 1.0, 4)


def trap_signals(p: Period, prior: Period | None, price: float) -> dict:
    """All §5 trap-risk signals for one name. going_concern / auditor_change are left None
    here — they come from L0 filing text, not the financials."""
    return {**altman_z(p, price), **piotroski_f(p, prior), **beneish_m(p, prior),
            "runway_months": cash_runway_months(p), "dilution_yoy": dilution_yoy(p, prior),
            "going_concern": None, "auditor_change": None}
