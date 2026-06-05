import json

from deepvalue.ingest.fundamentals_store import Period, as_of, load_periods
from deepvalue.quant.metrics import ncav, nnwc, tangible_book, value_metrics
from deepvalue.quant.trap_signals import altman_z, beneish_m, dilution_yoy, piotroski_f


def _period(income=None, balance=None, period_end="2020-12-31", filed="2021-03-01"):
    return Period("T", "1", period_end, filed, income or {}, balance or {})


# ----- value metrics -----

def test_value_metrics_netnet():
    p = _period(
        income={"weightedAverageShsOutDil": 100, "ebit": 50, "ebitda": 60, "revenue": 200},
        balance={"totalCurrentAssets": 300, "totalLiabilities": 100, "totalStockholdersEquity": 250,
                 "goodwill": 20, "intangibleAssets": 30, "totalDebt": 40, "cashAndCashEquivalents": 60},
    )
    vm = value_metrics(p, price=1.0)        # market cap = 100
    assert vm["market_cap"] == 100
    assert ncav(p) == 200                   # 300 - 100
    assert vm["price_to_ncav"] == 0.5       # cheap net-net
    assert tangible_book(p) == 200          # 250 - 20 - 30
    assert vm["p_tbv"] == 0.5
    assert vm["ev"] == 80                   # 100 + 40 - 60
    assert vm["ev_ebit"] == 80 / 50


def test_nnwc_haircut_and_missing_inputs():
    p = _period(balance={"cashAndCashEquivalents": 100, "netReceivables": 40,
                         "inventory": 20, "totalLiabilities": 90})
    assert nnwc(p) == 100 + 0.75 * 40 + 0.5 * 20 - 90  # 50 (haircut assets minus ALL liabilities)
    assert ncav(_period(balance={"totalCurrentAssets": 10})) is None  # missing TL -> None


# ----- trap signals -----

def test_altman_z_zones():
    safe = _period(
        income={"ebit": 40, "revenue": 200, "weightedAverageShsOutDil": 100},
        balance={"totalAssets": 200, "totalCurrentAssets": 150, "totalCurrentLiabilities": 30,
                 "retainedEarnings": 120, "totalLiabilities": 50})
    assert altman_z(safe, price=2.0)["z_zone"] == "safe"
    distress = _period(
        income={"ebit": -20, "revenue": 30, "weightedAverageShsOutDil": 100},
        balance={"totalAssets": 200, "totalCurrentAssets": 40, "totalCurrentLiabilities": 90,
                 "retainedEarnings": -150, "totalLiabilities": 180})
    assert altman_z(distress, price=0.2)["z_zone"] == "distress"
    assert altman_z(_period(), price=1.0)["z_score"] is None  # no totals -> None


def test_piotroski_counts_and_needs_prior():
    cur = _period(income={"netIncome": 10, "revenue": 200, "grossProfit": 80,
                          "weightedAverageShsOutDil": 100, "depreciationAndAmortization": 5},
                  balance={"totalAssets": 100, "totalCurrentAssets": 60, "totalCurrentLiabilities": 30,
                           "longTermDebt": 10})
    prior = _period(income={"netIncome": 5, "revenue": 180, "grossProfit": 70,
                            "weightedAverageShsOutDil": 100},
                    balance={"totalAssets": 100, "totalCurrentAssets": 50, "totalCurrentLiabilities": 40,
                             "longTermDebt": 20}, period_end="2019-12-31")
    f = piotroski_f(cur, prior)
    assert f["f_checks"] == 9 and 0 <= f["f_score"] <= 9 and f["f_score"] >= 6  # healthy, improving
    assert piotroski_f(cur, None)["f_checks"] == 3  # only the 3 prior-free checks


def test_beneish_neutral_below_threshold():
    flat = {"revenue": 100, "netReceivables": 10, "grossProfit": 40, "totalAssets": 200,
            "totalCurrentAssets": 80, "propertyPlantEquipmentNet": 50,
            "depreciationAndAmortization": 10, "sellingGeneralAndAdministrativeExpenses": 20,
            "longTermDebt": 30, "totalCurrentLiabilities": 25, "netIncome": 12}
    m = beneish_m(_period(income=flat, balance=flat), _period(income=flat, balance=flat,
                  period_end="2019-12-31"))
    assert m["m_score"] is not None and m["m_flag"] is False  # unchanged firm -> not a manipulator
    assert beneish_m(_period(), None)["m_score"] is None      # no prior -> None


def test_dilution_yoy():
    cur = _period(income={"weightedAverageShsOutDil": 120})
    prior = _period(income={"weightedAverageShsOutDil": 100}, period_end="2019-12-31")
    assert dilution_yoy(cur, prior) == 0.2   # 20% share growth


# ----- point-in-time store -----

def test_as_of_is_point_in_time(tmp_path):
    f = tmp_path / "T__active.json"
    f.write_text(json.dumps({"symbol": "T", "cik": "1", "income": [
        {"date": "2019-12-31", "filingDate": "2020-03-01", "netIncome": 1},
        {"date": "2020-12-31", "filingDate": "2021-03-01", "netIncome": 2},
    ], "balance": [
        {"date": "2019-12-31", "totalAssets": 10}, {"date": "2020-12-31", "totalAssets": 20}]}))
    assert len(load_periods("T", cache_dir=tmp_path)) == 2
    # as of mid-2020 we must NOT see the 2020 fiscal year (filed 2021)
    p = as_of("T", "2020-06-01", cache_dir=tmp_path)
    assert p.period_end == "2019-12-31" and p.get("totalAssets") == 10
    assert as_of("T", "2019-01-01", cache_dir=tmp_path) is None  # nothing filed yet
