from __future__ import annotations

import ast
import json
import math
import unittest
from pathlib import Path
from types import SimpleNamespace

from buy.binance_trades import PriceGrid, parse_agg_trade
from buy.fivemin import (
    DEFAULTS, FiveMinIO, Strategy, cash_free, dry_run, gate_ratio,
    local_day, market_offset, proxy_price, run_ready, settlement_pnl, twap60,
)

ROOT = Path(__file__).resolve().parents[1]


def cfg(**extra):
    value = dict(DEFAULTS)
    value.update({"fivemin_enabled": True, "dry_run": False, "redeem_enabled": True})
    value.update(extra)
    return value


def market(cid="cid", start=1000):
    return SimpleNamespace(condition_id=cid, slug=f"btc-up-or-down-5m-{start}",
                           start_ts=start, end_ts=start + 300, up_token="up", dn_token="dn",
                           active=True, closed=False, neg_risk=False)


class GridTests(unittest.TestCase):
    def test_ceil_bucket_last_trade_wins_forward_fill_and_ring_missing(self):
        grid = PriceGrid(1800)
        self.assertEqual(grid.add(1000, "100"), 10)
        self.assertEqual(grid.add(1001, "101"), 11)
        self.assertEqual(grid.add(1000, "102"), 10)
        self.assertEqual(grid.price_at(10), 102)
        self.assertEqual(grid.price_at(11), 101)
        self.assertEqual(grid.price_at(12), 101)
        self.assertIsNone(PriceGrid().price_at(1))
        self.assertEqual(parse_agg_trade('{"T":1001,"p":"99.5"}'), (1001.0, 99.5))

    def test_twap_offset_proxy_and_decision_staleness(self):
        grid = PriceGrid()
        for g in range(0, 1000):
            grid.add(g * 100, 100 + g)
        expected = sum(100 + (600 - 10 * j) for j in range(60)) / 60
        self.assertEqual(twap60(grid, 600), expected)
        self.assertEqual(market_offset(grid, 60.0, 500.0), 500 - sum(100 + (600 - 10 * j) for j in range(60)) / 60)
        offset = 5.0
        self.assertEqual(proxy_price(grid, 60.0, offset), twap60(grid, 595) + offset)
        self.assertIsNone(twap60(PriceGrid(), 600))


class PurePolicyTests(unittest.TestCase):
    def test_gate_directions_and_floor(self):
        self.assertAlmostEqual(gate_ratio(1.2, 1.0, "up", 0.5), 0.4)
        self.assertAlmostEqual(gate_ratio(0.8, 1.0, "down", 0.5), 0.4)
        self.assertEqual(gate_ratio(None, 1, "up", 1), None)
        self.assertEqual(run_ready(None, True, 10, 3), (10, False))
        self.assertEqual(run_ready(10, True, 12.9, 3)[1], True)
        self.assertEqual(run_ready(10, False, 12.9, 3), (None, False))
        self.assertEqual(run_ready(None, True, 10, 2)[1], False)
        self.assertEqual(local_day(0), "1970-01-01")

    def test_cash_and_settlement(self):
        spends = [{"ts": 110, "cost": 2}, {"ts": 90, "cost": 100}]
        self.assertEqual(cash_free(100, 100, 120, 5, spends), 93)
        self.assertIsNone(cash_free(100, 0, 120, 0, []))
        self.assertEqual(settlement_pnl({"side": "up", "shares": 10, "cost": 8, "proceeds": 1}, "up"), 3)
        self.assertEqual(settlement_pnl({"side": "up", "shares": 10, "cost": 8, "proceeds": 1}, "down"), -7)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.logs, self.saved, self.buys, self.sells, self.alerts = [], [], [], [], []
        self.io = FiveMinIO(
            buy=lambda token, shares, limit, dry: self.buys.append((token, shares, limit, dry)) or {"shares": shares, "cost": shares * limit, "status": "matched"},
            sell=lambda row, pos, book, cfg, now: self.sells.append(row["condition_id"]) or {"shares": pos["shares"], "proceeds": pos["shares"] * book["bid"], "price": book["bid"], "status": "matched"},
            reserves=lambda cfg: 0, redeem=lambda row, now: self.logs.append(("redeem", row["condition_id"])),
            save=lambda state: self.saved.append(json.loads(json.dumps(state))),
            log=lambda event, **kw: self.logs.append((event, kw)),
            alert=lambda text, **kw: self.alerts.append(text), durable=lambda: True,
        )
        self.strategy = Strategy(self.io)
        self.strategy.add_market(market())
        row = self.strategy.state["markets"]["cid"]
        row["strike"] = 100.0
        row["offset"] = 0.0
        self.price = 0.0
        self.grid = SimpleNamespace(price_at=lambda g: self.price)
        self.strategy.sample_cash(1200, 1000)

    def quotes(self, up=0.05, down=0.90):
        return {"cid": {"up": {"bid": up, "ask": up + .01, "bids": [], "asks": []},
                         "down": {"bid": down, "ask": down + .01, "bids": [], "asks": []}}}

    def test_persistence_last_window_gate_not_persisted_and_first_run_wins(self):
        self.strategy.tick(cfg(), 1000.0 + 300 - 100, self.grid, self.quotes())
        self.assertFalse(self.buys)
        self.strategy.tick(cfg(), 1000.0 + 300 - 97.0, self.grid, self.quotes())
        self.assertTrue(self.buys)
        self.assertEqual(self.buys[0][0], "dn")

    def test_false_resets_and_no_ttm_floor(self):
        self.strategy.tick(cfg(fivemin_gate_k=0), 1201.0, self.grid, self.quotes())
        self.strategy.tick(cfg(fivemin_gate_k=0), 1201.1, self.grid, {"cid": {}})
        self.assertFalse(self.buys)
        row = self.strategy.state["markets"]["cid"]
        row["entered"] = False
        self.strategy.runs.clear()
        self.strategy.runs["cid"] = {"up": 1297.6}
        self.strategy.sample_cash(1290, 1000)
        self.strategy.tick(cfg(fivemin_gate_k=0), 1299.5, self.grid, self.quotes())
        self.assertTrue(self.buys)

    def test_gate_signal_consumed_by_cash_and_disabled_flag(self):
        self.strategy.io = FiveMinIO(**{**self.io.__dict__, "reserves": lambda cfg: 10000})
        self.strategy.runs["cid"] = {"up": 1200.0}
        self.strategy.tick(cfg(), 1203.0, self.grid, self.quotes())
        self.assertFalse(self.buys)
        self.assertTrue(any(row[0] == "fivemin_cash_skip" for row in self.logs))
        self.strategy.state["markets"]["cid"]["entered"] = False
        self.strategy.runs.clear()
        self.strategy.runs["cid"] = {"up": 1203.0}
        self.strategy.tick(cfg(fivemin_enabled=False, fivemin_gate_k=0), 1206.0, self.grid, self.quotes())
        self.assertFalse(self.buys)
        self.assertTrue(any(row[0] == "fivemin_signal_blocked" for row in self.logs))

    def test_dump_trigger_reset_min_notional_one_dump_and_settle_daily_stop(self):
        row = self.strategy.state["markets"]["cid"]
        row["position"] = {"token": "down", "side": "down", "shares": 10.0, "cost": 9.0,
                            "avg_px": .9, "dumped": False, "proceeds": 0.0, "settled": False}
        self.strategy.tick(cfg(), 1100, self.grid, self.quotes(up=.8, down=.39))
        self.strategy.tick(cfg(), 1102.8, self.grid, self.quotes(up=.8, down=.39))
        self.assertFalse(self.sells)
        self.strategy.tick(cfg(), 1103, self.grid, self.quotes(up=.8, down=.39))
        self.assertEqual(self.sells, ["cid"])
        self.strategy.boundaries[1300] = 100
        self.strategy.tick(cfg(), 1301, self.grid, self.quotes(up=.8, down=.39))
        self.assertTrue(row["position"]["settled"])
        self.assertAlmostEqual(row["position"]["pnl"], -5.1)

    def test_notional_hold_and_one_entry_per_market(self):
        row = self.strategy.state["markets"]["cid"]
        row["position"] = {"token": "down", "side": "down", "shares": 1, "cost": .9,
                            "avg_px": .9, "dumped": False, "proceeds": 0, "settled": False}
        self.strategy.tick(cfg(), 1100, self.grid, self.quotes(up=.8, down=.01))
        self.strategy.tick(cfg(), 1104, self.grid, self.quotes(up=.8, down=.01))
        self.assertFalse(self.sells)
        self.assertTrue(row["position"].get("notional_logged"))
        row["entered"] = True
        self.strategy.tick(cfg(fivemin_gate_k=0), 1200, self.grid, self.quotes())
        self.assertFalse(self.buys)


class MintbotGateTest(unittest.TestCase):
    def test_disabled_branch_contains_lazy_imports_and_default(self):
        source = (ROOT / "mintbot.py").read_text()
        self.assertIn("'fivemin_enabled': False", source)
        tree = ast.parse(source)
        imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module in ("buy.fivemin", "buy.binance_trades")]
        self.assertTrue(imports)
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        guarded = [n for n in ast.walk(main) if isinstance(n, ast.If) and ast.unparse(n.test) == "cfg.get('fivemin_enabled', False)"]
        self.assertTrue(guarded)
        self.assertTrue(all(any(n in ast.walk(branch) for branch in guarded) for n in imports))

class AdditionalFiveMinTests(unittest.TestCase):
    def make_strategy(self):
        logs, saved, buys, sells = [], [], [], []
        io = FiveMinIO(
            buy=lambda *a, **k: buys.append((a, k)) or {"shares": a[1], "cost": a[1] * a[2], "status": "matched"},
            sell=lambda row, pos, book, cfg, now: sells.append(row["condition_id"]) or {"shares": pos["shares"], "proceeds": pos["shares"] * book["bid"], "price": book["bid"], "status": "matched"},
            reserves=lambda cfg: 0, redeem=lambda row, now: logs.append(("redeem", row["condition_id"])),
            save=lambda state: saved.append(state), log=lambda event, **kw: logs.append((event, kw)), durable=lambda: True,
        )
        strat = Strategy(io)
        strat.add_market(market(start=1000))
        row = strat.state["markets"]["cid"]
        row.update(strike=100.0, offset=0.0)
        strat.sample_cash(1200, 1000)
        return strat, logs, buys, sells

    def test_missing_strike_blocks_and_first_boundary_sample_wins(self):
        strat, logs, buys, _ = self.make_strategy()
        strat.boundary_samples([SimpleNamespace(obs_ts=1200.0001, twap="101"), SimpleNamespace(obs_ts=1200.0002, twap="99")])
        self.assertEqual(strat.boundaries[1200], 101)
        row = strat.state["markets"]["cid"]
        row["strike"] = None
        strat.tick(cfg(fivemin_gate_k=0), 1200, SimpleNamespace(price_at=lambda _: 100), {"cid": {"up": {"bid": .05}, "down": {"bid": .9}}})
        self.assertFalse(buys)
        self.assertTrue(any(e == "fivemin_no_strike" for e, _ in logs))

    def test_both_loser_runs_choose_earlier_run_and_gate_does_not_extend_persistence(self):
        strat, logs, buys, _ = self.make_strategy()
        grid = SimpleNamespace(price_at=lambda _: 100)
        q = {"cid": {"up": {"bid": .05}, "down": {"bid": .9}}}
        strat.runs["cid"] = {"up": 1200.0, "down": 1200.5}
        strat.tick(cfg(fivemin_gate_k=0), 1203.0, grid, q)
        self.assertEqual(buys[0][0][0], "dn")
        self.assertEqual(strat.state["markets"]["cid"]["position"]["side"], "down")

    def test_partial_fill_and_dublin_rollover(self):
        strat, _, buys, _ = self.make_strategy()
        strat.io = FiveMinIO(
            buy=lambda *a, **k: {"shares": 10, "cost": 9, "status": "partial"}, sell=strat.io.sell,
            reserves=strat.io.reserves, redeem=strat.io.redeem, save=strat.io.save,
            log=strat.io.log, durable=lambda: True,
        )
        strat.runs["cid"] = {"up": 1200.0}
        strat.tick(cfg(fivemin_gate_k=0), 1203, SimpleNamespace(price_at=lambda _: 100), {"cid": {"up": {"bid": .05}, "down": {"bid": .9}}})
        self.assertEqual(strat.state["markets"]["cid"]["position"]["shares"], 10)
        strat.roll_day(1_700_000_000)
        self.assertEqual(strat.state["day"], local_day(1_700_000_000))

    def test_runtime_disable_manages_existing_position(self):
        strat, _, _, sells = self.make_strategy()
        row = strat.state["markets"]["cid"]
        row["position"] = {"token": "down", "side": "down", "shares": 10, "cost": 9,
                            "dumped": False, "proceeds": 0, "settled": False, "dry_run": False}
        strat.tick(cfg(fivemin_enabled=False), 1100, SimpleNamespace(price_at=lambda _: 100), {"cid": {"up": {"bid": .8}, "down": {"bid": .39}}})
        strat.tick(cfg(fivemin_enabled=False), 1103, SimpleNamespace(price_at=lambda _: 100), {"cid": {"up": {"bid": .8}, "down": {"bid": .39}}})
        self.assertEqual(sells, ["cid"])

class RunnerTests(unittest.TestCase):
    def test_discovery_throttle_four_tokens_cash_and_cadence(self):
        from buy.fivemin import Runner
        clock = [1201.0]
        discoveries, balances, token_sets = [], [], []
        io = FiveMinIO(buy=lambda *a: {}, sell=lambda *a: {}, reserves=lambda cfg: 0,
                       redeem=lambda *a: None, save=lambda *a: None)
        strategy = Strategy(io)
        gateway = SimpleNamespace(discover=lambda slugs: discoveries.append(slugs) or [market("cid", 1200), market("next", 1500)])
        chain = SimpleNamespace(pUSD_balance=lambda *a: balances.append(a) or 100)
        books = SimpleNamespace(set_tokens=lambda tokens: token_sets.append(tokens),
                                set_stale_s=lambda _: None, set_min_size=lambda n: self.assertEqual(n, 0),
                                usable=lambda token: None)
        stopped = SimpleNamespace(start=lambda: None, stop=lambda: None)
        rtds = SimpleNamespace(start=lambda: None, stop=lambda: None, drain=lambda: [])
        runner = Runner(strategy, lambda: cfg(pUSD_address="cash"), gateway, chain, "funder",
                        PriceGrid(), stopped, rtds, books, clock=lambda: clock[0])
        runner.tick()
        self.assertEqual(len(discoveries), 1)
        self.assertEqual(token_sets[-1], ["up", "dn", "up", "dn"])
        self.assertEqual(len(balances), 1)
        self.assertEqual(runner.sleep_s(), .1)
        clock[0] += 19
        runner.tick()
        self.assertEqual(len(discoveries), 1)
        clock[0] += 1
        runner.tick()
        self.assertEqual(len(discoveries), 2)
        self.assertEqual(len(balances), 1)

    def test_boundary_settlement_pnl_and_gamma_fallback_schedule(self):
        calls, redemptions = [], []
        io = FiveMinIO(buy=lambda *a: {}, sell=lambda *a: {}, reserves=lambda cfg: 0,
                       redeem=lambda row, now: redemptions.append(row["condition_id"]), save=lambda *a: None,
                       gamma_winner=lambda slug: calls.append(slug) or "up")
        strategy = Strategy(io)
        strategy.add_market(market("cid", 1200))
        row = strategy.state["markets"]["cid"]
        row.update(entered=True, strike=100, offset=0)
        row["position"] = {"side": "up", "token": "up", "shares": 10, "cost": 8,
                           "proceeds": 1, "dumped": True, "settled": False, "dry_run": False}
        strategy.tick(cfg(), 1619, PriceGrid(), {})
        self.assertFalse(calls)
        strategy.tick(cfg(), 1620, PriceGrid(), {})
        self.assertEqual(calls, [row["slug"]])
        self.assertEqual(row["position"]["pnl"], 3)
        self.assertEqual(redemptions, ["cid"])
        strategy.tick(cfg(), 1650, PriceGrid(), {})
        self.assertEqual(calls, [row["slug"]])

    def test_daily_stop_rollover_and_restart_entered_flag(self):
        from datetime import datetime, timezone
        io = FiveMinIO(buy=lambda *a: self.fail("duplicate entry"), sell=lambda *a: {},
                       reserves=lambda cfg: 0, redeem=lambda *a: None, save=lambda *a: None)
        strat = Strategy(io)
        strat.add_market(market("cid", 1200))
        row = strat.state["markets"]["cid"]
        row.update(entered=True, strike=100, offset=0)
        row["position"] = {"side": "up", "shares": 0, "cost": 31, "proceeds": 0,
                           "dumped": True, "settled": False, "dry_run": False}
        strat.boundaries[1500] = 99
        strat.tick(cfg(), 1501, PriceGrid(), {})
        self.assertEqual(strat.state["paused_until_day"], strat.state["day"])
        clone = Strategy(io, json.loads(json.dumps(strat.state)))
        self.assertTrue(clone.state["markets"]["cid"]["entered"])
        next_day = datetime(1970, 1, 2, tzinfo=timezone.utc).timestamp()
        clone.tick(cfg(), next_day, PriceGrid(), {})
        self.assertIsNone(clone.state["paused_until_day"])
        self.assertEqual(clone.state["realized"], 0)


if __name__ == "__main__":
    unittest.main()
