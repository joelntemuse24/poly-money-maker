"""Unit tests for sell policy opposite-leg confirmation."""
from sim.policy import evaluate

BASE = {
    "sell_threshold": 0.08,
    "sell_window_min": 120.0,
    "sell_lastchance_s": 0,
    "sell_lastchance_threshold": 0.35,
    "hedge_enabled": False,
    "hedge_threshold": 0.50,
    "sell_confirm_opposite": 0.70,
}


def _ev(**kw):
    d = dict(
        seconds_left=600.0,
        up_bid=0.50,
        dn_bid=0.50,
        up_size=5.0,
        dn_size=5.0,
        sold_up=False,
        sold_dn=False,
        strategy=BASE,
    )
    d.update(kw)
    return evaluate(**d)


def test_threshold_sell_when_opposite_confirmed():
    d = _ev(dn_bid=0.07, up_bid=0.80)
    assert d.action == "sell_dn"
    assert d.reason == "threshold"


def test_threshold_blocked_when_opposite_soft():
    d = _ev(dn_bid=0.07, up_bid=0.40)
    assert d.action == "none"
    assert d.reason == "threshold_unconfirmed"


def test_threshold_blocked_when_opposite_missing():
    d = _ev(dn_bid=0.07, up_bid=None)
    assert d.action == "none"
    assert d.reason == "threshold_unconfirmed"


def test_confirm_off_allows_soft_opposite():
    strat = dict(BASE, sell_confirm_opposite=0.0)
    d = _ev(dn_bid=0.07, up_bid=0.40, strategy=strat)
    assert d.action == "sell_dn"


def test_both_legs_still_ambiguous():
    d = _ev(up_bid=0.07, dn_bid=0.07)
    # both <= thr; after confirm neither may pass if opp also soft
    assert d.action == "none"


def test_up_sell_confirmed():
    d = _ev(up_bid=0.06, dn_bid=0.75)
    assert d.action == "sell_up"


if __name__ == "__main__":
    test_threshold_sell_when_opposite_confirmed()
    test_threshold_blocked_when_opposite_soft()
    test_threshold_blocked_when_opposite_missing()
    test_confirm_off_allows_soft_opposite()
    test_both_legs_still_ambiguous()
    test_up_sell_confirmed()
    print("ok")
