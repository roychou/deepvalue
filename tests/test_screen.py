from deepvalue.quant.screen import _passes, _rank_key, load_profiles

PROFS = load_profiles()  # the real config/screen_profiles.yaml


def _thr(profile):
    return PROFS.get(profile, {}).get("thresholds", {})


def test_net_net_pass_and_fail():
    t = _thr("net_net")
    # cheap net-net: price <= 2/3 NCAV and p/tbv <= 1
    assert _passes("net_net", {"price_to_ncav": 0.5, "p_tbv": 0.8}, t) is True
    assert _passes("net_net", {"price_to_ncav": 0.66, "p_tbv": 1.0}, t) is True
    # too expensive on NCAV
    assert _passes("net_net", {"price_to_ncav": 0.9, "p_tbv": 0.5}, t) is False
    # cheap on NCAV but trades above tangible book
    assert _passes("net_net", {"price_to_ncav": 0.4, "p_tbv": 1.5}, t) is False
    # negative price_to_ncav (broken NCAV) must NOT pass
    assert _passes("net_net", {"price_to_ncav": -2.0, "p_tbv": 0.5}, t) is False
    # missing inputs -> not screenable
    assert _passes("net_net", {"price_to_ncav": None, "p_tbv": 0.5}, t) is False


def test_normalized_earnings_threshold():
    t = _thr("normalized_earnings")
    assert _passes("normalized_earnings", {"ev_ebit": 6.0}, t) is True
    assert _passes("normalized_earnings", {"ev_ebit": 8.0}, t) is True
    assert _passes("normalized_earnings", {"ev_ebit": 12.0}, t) is False
    assert _passes("normalized_earnings", {"ev_ebit": -5.0}, t) is False  # negative EBIT
    assert _passes("normalized_earnings", {"ev_ebit": None}, t) is False


def test_hidden_assets_deferred():
    assert _passes("hidden_assets", {"nonop_asset_coverage": 5.0}, _thr("hidden_assets")) is False


def test_rank_key_orders_cheapest_first():
    # lower metric -> lower (better) rank key
    assert _rank_key("net_net", {"price_to_ncav": 0.3}) < _rank_key("net_net", {"price_to_ncav": 0.6})
    assert _rank_key("normalized_earnings", {"ev_ebit": 4.0}) < _rank_key("normalized_earnings", {"ev_ebit": 7.0})
    # missing metric sinks to the bottom
    assert _rank_key("net_net", {"price_to_ncav": None}) == float("inf")
