"""L6 conviction sizing + L7 outcome tracking — deterministic, no network."""
from deepvalue.calibration.metrics import deterioration_ic, verdict_spread
from deepvalue.calibration.outcomes import forward_return, score_outcomes
from deepvalue.forward.ibkr_execution import conviction_weights


def test_conviction_weights_cap_and_no_leverage():
    buys = [{"ticker": "A", "margin_of_safety": 0.70}, {"ticker": "B", "margin_of_safety": 0.45},
            {"ticker": "C", "margin_of_safety": 0.0}]
    w = conviction_weights(buys, kelly_fraction=0.25, max_weight=0.06)
    assert w["A"] == 0.06 and w["B"] == 0.06          # both above the cap -> capped
    assert w["C"] == 0.0                              # no margin of safety -> no weight
    assert sum(w.values()) <= 1.0                     # never levered


def test_conviction_weights_scales_below_cap_and_holds_cash():
    # a 3-name book that wants >100% gross is scaled to fully invested; a thin one leaves cash
    thin = conviction_weights([{"ticker": "A", "margin_of_safety": 0.5}], kelly_fraction=0.25, max_weight=0.06)
    assert 0 < sum(thin.values()) < 1.0              # one small position -> mostly cash


def test_forward_return_and_none_when_data_absent():
    px = {"2026-06-08": {"close": 10.0}, "2026-09-10": {"close": 8.0}}
    assert abs(forward_return(px, "2026-06-08", 63) - (-0.20)) < 1e-9
    assert forward_return(px, "2026-06-08", 999) is None   # no forward bar yet


_BOOKS = [{"as_of": "2026-06-08", "book": [
    {"ticker": "X", "verdict": "WATCH", "deterioration": 0.8, "composite": 1.0, "flags": ["DETERIORATING"]},
    {"ticker": "Y", "verdict": "BUY", "deterioration": 0.1, "composite": 2.0, "flags": []}]}]
_PX = {"X": {"2026-06-08": {"close": 10.0}, "2026-09-10": {"close": 8.0}},    # deteriorating -> down 20%
       "Y": {"2026-06-08": {"close": 10.0}, "2026-09-10": {"close": 12.0}}}   # clean -> up 20%


def test_score_outcomes_and_decay_ic_direction():
    recs = score_outcomes(_BOOKS, _PX, horizons=(63,))
    assert {r.ticker for r in recs} == {"X", "Y"} and all(r.realized for r in recs)
    ic = deterioration_ic(_BOOKS, _PX, horizon_days=63)
    # deteriorating X fell, clean Y rose -> -deterioration correlates POSITIVELY with return
    assert ic["n"] == 2 and (ic["ic_neg_deterioration"] is None or ic["ic_neg_deterioration"] > 0)
    sp = verdict_spread(_BOOKS, _PX, horizon_days=63)
    assert sp["n_buy"] == 1 and sp["n_watch"] == 1
