"""Unused b15 budget folds into a22 when t15 never bought."""
from buy.entry_skip import a22_budget_with_orphan_b15, hourly_slice_budget


def test_orphan_adds_b15_when_t15_not_bought():
    assert a22_budget_with_orphan_b15({}, 160.0, 40.0) == 200.0
    assert a22_budget_with_orphan_b15({"t15_bought": False}, 160.0, 40.0) == 200.0


def test_orphan_zero_when_b15_filled():
    assert a22_budget_with_orphan_b15({"t15_bought": True}, 160.0, 40.0) == 160.0


def test_a22_slice_spends_orphan_via_raised_budget():
    meta = {"pnl_entry_cost": 0, "a22_spent_usd": 0}
    got = hourly_slice_budget(
        "a22", meta, a22_budget=200.0, b15_budget=40.0, market_cap=210.0,
    )
    assert abs(got - 200.0) < 1e-9


def test_after_b15_spend_a22_stays_base():
    meta = {"t15_bought": True, "pnl_entry_cost": 40.0, "a22_spent_usd": 0}
    bud = a22_budget_with_orphan_b15(meta, 160.0, 40.0)
    assert bud == 160.0
    got = hourly_slice_budget(
        "a22", meta, a22_budget=bud, b15_budget=40.0, market_cap=210.0,
    )
    assert abs(got - 160.0) < 1e-9
