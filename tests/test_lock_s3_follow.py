import unittest

from buy.lock_config import apply_defaults
from buy.lock_s3 import evaluate_strategy3, normalize_s3_fill, s3_fill_key
from buy.lock_s3 import NIULAI4


SLUG = "btc-updown-5m-1791153900"


def fill(*, side="BUY", outcome="Up", tx="0x1", price=0.4, size=10):
    return {
        "wallet": NIULAI4,
        "slug": SLUG,
        "trade_side": side,
        "outcome": outcome,
        "asset": "token-up",
        "price": price,
        "size": size,
        "tx": tx,
    }


def decision(raw, *, account=None, cfg=None):
    item = normalize_s3_fill(raw)
    return evaluate_strategy3(
        {"his_fill": item, "market_key": "btc_5m", "slug": SLUG, "ttm_s": 20, "detect_source": "data_api", "detect_lag_ms": 42},
        account or {"spent_s3": 0, "pending_s3": 0},
        apply_defaults({"strategy3_enabled": True, **(cfg or {})}),
    )


class Strategy3FollowTests(unittest.TestCase):
    def test_disabled_has_no_order(self):
        out = evaluate_strategy3({"his_fill": fill(), "market_key": "btc_5m", "ttm_s": 20}, {}, apply_defaults({}))
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "strategy_off")

    def test_buy_is_same_side_ten_dollars_at_099(self):
        out = decision(fill())
        self.assertEqual(out["action"], "buy")
        self.assertEqual(out["side"], "up")
        self.assertEqual(out["notional"], 10.0)
        self.assertEqual(out["limit"], 0.99)
        self.assertEqual(out["detect_lag_ms"], 42)

    def test_second_fill_add_or_flip_then_cap(self):
        first = decision(fill(tx="0x1"))
        second = decision(fill(tx="0x2", outcome="Down"), account={"spent_s3": 10, "pending_s3": 0})
        third = decision(fill(tx="0x3"), account={"spent_s3": 20, "pending_s3": 0})
        self.assertEqual(first["action"], "buy")
        self.assertEqual(second["action"], "buy")
        self.assertEqual(second["side"], "down")
        self.assertEqual(third["reason"], "strategy_cap")

    def test_duplicate_fill_is_skipped(self):
        raw = fill(tx="same")
        out = evaluate_strategy3({"his_fill": raw, "market_key": "btc_5m", "ttm_s": 20, "seen_fill_ids": [s3_fill_key(normalize_s3_fill(raw))]}, {}, apply_defaults({"strategy3_enabled": True}))
        self.assertEqual(out["reason"], "duplicate_fill")

    def test_ttm_sell_and_other_market_are_skipped(self):
        self.assertEqual(decision(fill(), cfg={"strategy3_min_ttm_s": 30})["reason"], "too_late")
        self.assertEqual(decision(fill(side="SELL"))["reason"], "fill_ignored")
        out = evaluate_strategy3({"his_fill": fill(), "market_key": "eth_5m", "ttm_s": 20}, {}, apply_defaults({"strategy3_enabled": True}))
        self.assertEqual(out["reason"], "strategy_market_off")


if __name__ == "__main__":
    unittest.main()
