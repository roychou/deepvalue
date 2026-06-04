import math

import pytest

from deepvalue.eval.ic import (
    cross_sectional_ic,
    forward_return,
    ic_summary,
    select_non_overlapping,
    spearman,
)

# ==========================================
# SPEARMAN
# ==========================================


def test_spearman_perfect_monotonic():
    # y is a monotonic (non-linear) transform of x -> Spearman = 1.0 (Pearson wouldn't be)
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [1.0, 4.0, 9.0, 16.0]
    assert spearman(xs, ys) == pytest.approx(1.0)


def test_spearman_perfect_inverse():
    assert spearman([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_spearman_handles_ties():
    # average-tie ranks; just assert it's defined and in range
    rho = spearman([1.0, 1.0, 2.0, 3.0], [5.0, 6.0, 6.0, 9.0])
    assert rho is not None and -1.0 <= rho <= 1.0


def test_spearman_zero_variance_is_none():
    assert spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None


def test_spearman_length_mismatch_raises():
    with pytest.raises(ValueError):
        spearman([1.0, 2.0], [1.0])


# ==========================================
# FORWARD RETURN
# ==========================================


def _prices(seq: list[tuple[str, float]]) -> dict[str, dict]:
    return {d: {"close": c} for d, c in seq}


def test_forward_return_basic():
    p = _prices([("2026-02-02", 100.0), ("2026-02-03", 101.0), ("2026-02-04", 110.0)])
    assert forward_return(p, "2026-02-02", 2) == pytest.approx(0.10)


def test_forward_return_picks_first_day_on_or_after_as_of():
    # as_of falls on a weekend/holiday gap; start from the next available day
    p = _prices([("2026-02-02", 100.0), ("2026-02-05", 100.0), ("2026-02-06", 105.0)])
    assert forward_return(p, "2026-02-03", 1) == pytest.approx(0.05)


def test_forward_return_off_the_end_is_none():
    p = _prices([("2026-02-02", 100.0), ("2026-02-03", 101.0)])
    assert forward_return(p, "2026-02-02", 5) is None


# ==========================================
# CROSS-SECTIONAL IC — calibration (planted signal)
# ==========================================


def test_ic_planted_positive_signal():
    """PLANTED-SIGNAL CALIBRATION: conviction ranks identically to forward return ->
    IC must be ~ +1.0. This is the IC analogue of a planted-failure test — a new
    validation metric that can't detect a signal it was handed is broken."""
    convictions = {"A": 0.9, "B": 0.3, "C": -0.2, "D": -0.8}
    fwd = {"A": 0.12, "B": 0.04, "C": -0.01, "D": -0.09}
    res = cross_sectional_ic("2026-02-02", convictions, fwd)
    assert res.n == 4
    assert res.ic == pytest.approx(1.0)


def test_ic_planted_inverse_signal_is_negative():
    """A perfectly WRONG model (high conviction on the worst names) must read strongly
    negative — the disqualify-fast direction."""
    convictions = {"A": 0.9, "B": 0.3, "C": -0.2, "D": -0.8}
    fwd = {"A": -0.09, "B": -0.01, "C": 0.04, "D": 0.12}
    assert cross_sectional_ic("d", convictions, fwd).ic == pytest.approx(-1.0)


def test_ic_drops_missing_forward_returns_and_abstentions():
    convictions = {"A": 0.9, "B": 0.3, "C": -0.2, "ABSTAIN": None}  # type: ignore[dict-item]
    fwd = {"A": 0.12, "B": 0.04, "C": -0.01, "NOPRICE": None}
    res = cross_sectional_ic("d", convictions, fwd)
    assert res.n == 3  # ABSTAIN (no conviction) and NOPRICE both excluded


def test_ic_too_few_names_is_none():
    res = cross_sectional_ic("d", {"A": 0.5}, {"A": 0.1})
    assert res.ic is None and res.n == 1


# ==========================================
# AGGREGATION
# ==========================================


def test_ic_summary_mean_and_tstat():
    from deepvalue.eval.ic import ICResult

    results = [
        ICResult("d1", 0.10, 80),
        ICResult("d2", 0.20, 90),
        ICResult("d3", 0.00, 70),
    ]
    s = ic_summary(results, horizon_days=5, spacing_days=5)
    assert s.n_dates == 3
    assert s.total_obs == 240
    assert s.mean_ic == pytest.approx(0.10)
    # std of [0.1,0.2,0.0] sample = 0.1; ic_ir = 1.0; t = 1.0*sqrt(3)
    assert s.ic_ir == pytest.approx(1.0)
    assert s.t_stat == pytest.approx(math.sqrt(3))
    assert s.overlap_warning is False


def test_ic_summary_flags_overlap():
    from deepvalue.eval.ic import ICResult

    s = ic_summary([ICResult("d1", 0.1, 50), ICResult("d2", 0.1, 50)],
                   horizon_days=20, spacing_days=5)
    assert s.overlap_warning is True


def test_ic_summary_skips_undefined_dates():
    from deepvalue.eval.ic import ICResult

    s = ic_summary([ICResult("d1", None, 1), ICResult("d2", 0.3, 60)],
                   horizon_days=5, spacing_days=5)
    assert s.n_dates == 1
    assert s.mean_ic == pytest.approx(0.3)


# ==========================================
# NON-OVERLAPPING SELECTION
# ==========================================


def test_select_non_overlapping_thins_by_horizon():
    dates = [f"2026-02-{d:02d}" for d in range(1, 11)]  # 10 daily dates
    kept = select_non_overlapping(dates, horizon_days=15, spacing_days=5)
    # step = ceil(15/5) = 3 -> every 3rd date
    assert kept == ["2026-02-01", "2026-02-04", "2026-02-07", "2026-02-10"]


def test_select_non_overlapping_noop_when_horizon_fits():
    dates = ["2026-02-01", "2026-02-08", "2026-02-15"]
    assert select_non_overlapping(dates, horizon_days=5, spacing_days=5) == dates
