from __future__ import annotations

import ast
import json
import math
import unittest
from pathlib import Path
from types import SimpleNamespace

from buy.binance_trades import PriceGrid, parse_agg_trade
from buy.fivemin import (
    DEFAULTS, FiveMinIO, Strategy, ask_depth, cash_free, dry_fill, dry_run, gate_ratio,
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
        self.strategy = Strategy(self.io, startup_ts=0)
        self.strategy.add_market(market(), now=1000)
        row = self.strategy.state["markets"]["cid"]
        row["strike"] = 100.0
        row["offset"] = 0.0
        self.price = 0.0
        self.grid = SimpleNamespace(price_at=lambda g: self.price, latest_exchange_ms=lambda: 1e15)
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
        self.strategy.tick(cfg(), 1100, self.grid, self.quotes(up=.8, down=.19))
        self.strategy.tick(cfg(), 1102.8, self.grid, self.quotes(up=.8, down=.19))
        self.assertFalse(self.sells)
        self.strategy.tick(cfg(), 1103, self.grid, self.quotes(up=.8, down=.19))
        self.assertEqual(self.sells, ["cid"])
        self.strategy.boundaries[1300] = 100
        self.strategy.tick(cfg(), 1301, self.grid, self.quotes(up=.8, down=.19))
        self.assertTrue(row["position"]["settled"])
        self.assertAlmostEqual(row["position"]["pnl"], -7.1)

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
        guarded = [n for n in ast.walk(main) if isinstance(n, ast.If) and ast.unparse(n.test) in ("cfg.get('fivemin_enabled', False)", "cfg.get('fivemin_enabled', False) or cfg.get('sniper_5m_enabled', False)")]
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
        strat = Strategy(io, startup_ts=0)
        strat.add_market(market(start=1000), now=1000)
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
        strat.tick(cfg(fivemin_gate_k=0), 1200, SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15), {"cid": {"up": {"bid": .05}, "down": {"bid": .9}}})
        self.assertFalse(buys)
        self.assertTrue(any(e == "fivemin_no_strike" for e, _ in logs))

    def test_no_strike_waits_for_grace_and_logs_once(self):
        strat, logs, buys, _ = self.make_strategy()
        row = strat.state["markets"]["cid"]
        row["strike"] = None
        for now in (1000, 1002, 1004.9):
            strat.tick(cfg(), now, PriceGrid(), {})
        self.assertEqual(strat.runs["cid"]["last_reason"], "no_strike")
        self.assertFalse(row.get("no_strike_logged"))
        self.assertFalse(any(e == "fivemin_no_strike" for e, _ in logs))
        for now in (1005, 1006):
            strat.tick(cfg(), now, PriceGrid(), {})
        self.assertEqual(sum(e == "fivemin_no_strike" for e, _ in logs), 1)
        self.assertFalse(buys)

    def test_exact_boundary_arriving_two_seconds_late_sets_strike(self):
        strat, logs, _, _ = self.make_strategy()
        strat.add_market(market("late", start=1200), now=1200)
        row = strat.state["markets"]["late"]
        strat.tick(cfg(), 1200, PriceGrid(), {})
        strat.boundary_samples([SimpleNamespace(obs_ts=1200, twap="101")])
        strat.tick(cfg(), 1202, PriceGrid(), {})
        self.assertEqual(row["strike"], 101)
        self.assertEqual(row["strike_source"], "rtds_boundary")
        self.assertFalse(any(e == "fivemin_no_strike" for e, _ in logs))

    def test_nearest_boundary_fallback_waits_for_grace(self):
        strat, logs, _, _ = self.make_strategy()
        strat.add_market(market("near", start=1200), now=1200)
        row = strat.state["markets"]["near"]
        strat.boundary_samples([
            SimpleNamespace(obs_ts=1198.9, twap="90"),
            SimpleNamespace(obs_ts=1199.0, twap="99"),
            SimpleNamespace(obs_ts=1200.25, twap="101"),
            SimpleNamespace(obs_ts=1199.5, twap="102"),
        ])
        self.assertNotIn(1200, strat.boundaries)
        self.assertEqual(strat._near_boundaries[1200], (0.25, 101))
        strat.tick(cfg(), 1204.9, PriceGrid(), {})
        self.assertIsNone(row.get("strike"))
        strat.tick(cfg(), 1205, PriceGrid(), {})
        strat.tick(cfg(), 1206, PriceGrid(), {})
        self.assertEqual(row["strike"], 101)
        self.assertEqual(row["strike_source"], "rtds_near")
        fallback = [kw for e, kw in logs if e == "fivemin_strike_fallback"]
        self.assertEqual(fallback, [{"slug": row["slug"], "diff_s": 0.25}])
        self.assertFalse(any(e == "fivemin_no_strike" for e, _ in logs))

    def test_boundary_caches_keep_last_32(self):
        strat, _, _, _ = self.make_strategy()
        strat.boundary_samples([
            SimpleNamespace(obs_ts=boundary + diff, twap="101")
            for boundary in range(300, 300 * 41, 300)
            for diff in (0, 0.5)
        ])
        expected = list(range(300 * 9, 300 * 41, 300))
        self.assertEqual(sorted(strat.boundaries), expected)
        self.assertEqual(sorted(strat._near_boundaries), expected)

    def test_close_eval_retains_last_window_books_ttm_ratio_and_counters(self):
        strat, logs, buys, _ = self.make_strategy()
        grid = SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15)
        quotes = {"cid": {"up": {"bid": .05}, "down": {"bid": .9}}}
        strat.tick(cfg(), 1179.9, grid, quotes)
        self.assertNotIn("last_bids", strat.runs["cid"])
        strat.tick(cfg(), 1180, grid, {})
        strat.tick(cfg(), 1200, grid, quotes)
        strat.tick(cfg(), 1203, grid, quotes)
        self.assertFalse(buys)
        self.assertEqual(strat.runs["cid"]["last_ratio"], 0)
        strat.tick(cfg(), 1300.1, grid, {})
        strat.tick(cfg(), 1300.2, grid, {})
        evaluations = [kw for e, kw in logs if e == "fivemin_eval"]
        self.assertEqual(len(evaluations), 1)
        evaluation = evaluations[0]
        self.assertEqual(evaluation["decision"], "skip")
        self.assertEqual(evaluation["bids"], {"up": .05, "down": .9})
        self.assertEqual(evaluation["ttm"], 97)
        self.assertGreater(evaluation["ttm"], 0)
        self.assertLessEqual(evaluation["ttm"], 120)
        self.assertEqual(evaluation["ratio"], 0)
        self.assertEqual(evaluation["close_ttm"], -0.1)
        self.assertEqual(evaluation["in_window_ticks"], 3)
        self.assertEqual(evaluation["book_ticks"], 2)
        self.assertEqual(evaluation["band_ticks"], 2)
        self.assertEqual(evaluation["persist_max_s"], 3)
        self.assertEqual(evaluation["best_ratio"], 0)
        self.assertEqual(evaluation["blocked_gate_below_k"], 1)
        self.assertEqual(evaluation["blocked_no_bid"], 1)
        self.assertEqual(evaluation["blocked_btc_stale"], 0)
        self.assertEqual(evaluation["blocked_no_strike"], 0)
        self.assertEqual(evaluation["miss"], {"up": {"missing": 1}, "down": {"missing": 1}})

    def test_skip_summary_no_strike_stale_btc_and_missing_reasons(self):
        strat, logs, buys, _ = self.make_strategy()
        row = strat.state["markets"]["cid"]
        row["strike"] = None
        grid = SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 0)
        for now in (1100, 1180, 1181):
            strat.tick(cfg(), now, grid, {})
        row["strike"] = 100
        q = {"cid": {"up": {"bid": .05}, "down": {"bid": .9}}}
        for now in (1200, 1203):
            strat.tick(cfg(), now, grid, q)
        strat.tick(cfg(), 1204, grid, {"cid": {"up": {"bid": None, "miss": "fetch_fail"},
                                                   "down": {"bid": None, "miss": "empty"}}})
        strat.tick(cfg(), 1300, grid, {})
        evaluation = next(fields for event, fields in logs if event == "fivemin_eval")
        self.assertEqual(evaluation["blocked_no_strike"], 2)
        self.assertEqual(evaluation["blocked_btc_stale"], 1)
        self.assertEqual(evaluation["blocked_no_bid"], 1)
        self.assertEqual(evaluation["band_ticks"], 2)
        self.assertEqual(evaluation["persist_max_s"], 3)
        self.assertIsNone(evaluation["best_ratio"])
        self.assertEqual(evaluation["miss"], {"up": {"fetch_fail": 1}, "down": {"empty": 1}})
        self.assertFalse(buys)

    def test_skip_summary_persistence_uses_entry_run_across_missing_books(self):
        strat, logs, buys, _ = self.make_strategy()
        grid = SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15)
        quotes = {"cid": {"up": {"bid": .05}, "down": {"bid": .9}}}
        for now, books in ((1200, quotes), (1203, quotes), (1204, {}),
                           (1210, quotes), (1211, quotes), (1300, {})):
            strat.tick(cfg(), now, grid, books)
        evaluation = next(fields for event, fields in logs if event == "fivemin_eval")
        self.assertEqual(evaluation["persist_max_s"], 11)
        self.assertEqual(evaluation["band_ticks"], 4)
        self.assertEqual(evaluation["blocked_no_bid"], 1)
        self.assertEqual(evaluation["best_ratio"], 0)
        self.assertFalse(buys)

    def test_both_loser_runs_choose_earlier_run_and_gate_does_not_extend_persistence(self):
        strat, logs, buys, _ = self.make_strategy()
        grid = SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15)
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
        strat.tick(cfg(fivemin_gate_k=0, fivemin_low_fill_exit=False), 1203, SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15), {"cid": {"up": {"bid": .05}, "down": {"bid": .9}}})
        self.assertEqual(strat.state["markets"]["cid"]["position"]["shares"], 10)
        strat.roll_day(1_700_000_000)
        self.assertEqual(strat.state["day"], local_day(1_700_000_000))

    def test_runtime_disable_manages_existing_position(self):
        strat, _, _, sells = self.make_strategy()
        row = strat.state["markets"]["cid"]
        row["position"] = {"token": "down", "side": "down", "shares": 10, "cost": 9,
                            "dumped": False, "proceeds": 0, "settled": False, "dry_run": False}
        strat.tick(cfg(fivemin_enabled=False), 1100, SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15), {"cid": {"up": {"bid": .8}, "down": {"bid": .19}}})
        strat.tick(cfg(fivemin_enabled=False), 1103, SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15), {"cid": {"up": {"bid": .8}, "down": {"bid": .19}}})
        self.assertEqual(sells, ["cid"])

class RunnerTests(unittest.TestCase):
    def make_runner(self, clock, discoveries, balances, token_sets, rtds=None, usable=None):
        from buy.fivemin import Runner
        io = FiveMinIO(buy=lambda *a: {}, sell=lambda *a: {}, reserves=lambda cfg: 0,
                       redeem=lambda *a: None, save=lambda *a: None)
        strategy = Strategy(io, startup_ts=0)
        gateway = SimpleNamespace(discover=lambda slugs: discoveries.append(slugs) or [market("cid", 1200), market("next", 1500)])
        chain = SimpleNamespace(pUSD_balance=lambda *a: balances.append(a) or 100)
        books = SimpleNamespace(set_tokens=lambda tokens: token_sets.append(tokens),
                                set_stale_s=lambda _: None, set_min_size=lambda n: self.assertEqual(n, 0),
                                usable=usable or (lambda token: None))
        stopped = SimpleNamespace(start=lambda: None, stop=lambda: None)
        rtds = rtds or SimpleNamespace(start=lambda: None, stop=lambda: None, drain=lambda: [], reconnect=lambda reason: None)
        return Runner(strategy, lambda: cfg(pUSD_address="cash"), gateway, chain, "funder",
                      PriceGrid(), stopped, rtds, books, clock=lambda: clock[0])

    def test_discovery_throttle_four_tokens_cash_and_cadence(self):
        clock = [1201.0]
        discoveries, balances, token_sets = [], [], []
        runner = self.make_runner(clock, discoveries, balances, token_sets)
        runner.tick()  # the tick thread never does discovery or balance I/O
        self.assertEqual((len(discoveries), len(balances)), (0, 0))
        runner._io_once(clock[0])
        self.assertEqual((len(discoveries), len(balances)), (1, 1))
        runner.tick()
        self.assertEqual(token_sets[-1], [])
        self.assertEqual(runner.strategy.balance, 100)
        self.assertEqual(runner.sleep_s(), 1.0)
        clock[0] += 19
        runner._io_once(clock[0])
        self.assertEqual(len(discoveries), 1)
        clock[0] += 1
        runner._io_once(clock[0])
        self.assertEqual(len(discoveries), 2)
        self.assertEqual(len(balances), 1)
        clock[0] += 10
        runner._io_once(clock[0])
        self.assertEqual(len(balances), 2)

    def test_rtds_reconnect_throttled_and_reset_by_samples(self):
        clock = [1201.0]
        reconnects, pending = [], []
        rtds = SimpleNamespace(start=lambda: None, stop=lambda: None,
                               drain=lambda: [pending.pop()] if pending else [],
                               reconnect=lambda reason: reconnects.append(reason))
        runner = self.make_runner(clock, [], [], [], rtds=rtds)
        clock[0] += 19
        runner._io_once(clock[0])
        self.assertEqual(reconnects, [])
        clock[0] += 1
        runner._io_once(clock[0]); runner._io_once(clock[0] + 19)
        self.assertEqual(reconnects, ["fivemin_silent"])
        runner._io_once(clock[0] + 20)
        self.assertEqual(len(reconnects), 2)
        pending.append(SimpleNamespace(obs_ts=1500.0, twap="1"))
        clock[0] += 21
        runner.tick()
        runner._io_once(clock[0] + 19)
        self.assertEqual(len(reconnects), 2)

    def test_rtds_silence_override_controls_reconnect_spacing_and_error(self):
        clock = [1201.0]
        runner = self.make_runner(clock, [], [], [])
        runner.cfg = lambda: cfg(pUSD_address="cash", fivemin_rtds_silent_s=45)
        reconnects, logs = [], []
        runner.rtds.reconnect = lambda reason: reconnects.append(reason)
        runner.strategy.io.log = lambda event, **fields: logs.append((event, fields))
        runner._io_once(1245.9)
        self.assertEqual(reconnects, [])
        runner._io_once(1246)
        runner._io_once(1290.9)
        self.assertEqual(reconnects, ["fivemin_silent"])
        runner._io_once(1291)
        self.assertEqual(len(reconnects), 2)
        self.assertIn(("fivemin_rtds_stall", {"error": "fivemin_silent silent_s=45"}), logs)

    def test_missing_quote_status_reaches_strategy_with_legacy_feed_default(self):
        clock = [1380.0]
        runner = self.make_runner(clock, [], [], [])
        runner._io_once(clock[0])
        seen = []
        runner.strategy.tick = lambda cfg, now, grid, quotes: seen.append(quotes)
        runner.tick()
        self.assertEqual(seen[-1]["cid"], {"up": {"bid": None, "miss": "missing"},
                                             "down": {"bid": None, "miss": "missing"}})
        runner.books.bid_status = lambda token: "empty" if token == "up" else "fetch_fail"
        runner.tick()
        self.assertEqual(seen[-1]["cid"]["up"]["miss"], "empty")
        self.assertEqual(seen[-1]["cid"]["down"]["miss"], "fetch_fail")

    def test_full_levels_reach_the_dump_and_gamma_goes_through_io_inbox(self):
        clock = [1201.0]
        levels = [(0.19 - i * 0.01, 10.0) for i in range(6)]
        tops = {"up": SimpleNamespace(bid=.8, ask=.81, bids=[(0.8, 5.0)] * 6, asks=[]),
                "dn": SimpleNamespace(bid=.19, ask=.20, bids=levels, asks=[])}
        runner = self.make_runner(clock, [], [], [], usable=lambda token: tops.get(token))
        seen = []
        runner.strategy.io.sell = lambda row, pos, book, cfg, now: seen.append(book) or {"shares": pos["shares"], "proceeds": 1.0}
        runner._io_once(clock[0])
        runner.tick()
        row = runner.strategy.state["markets"]["cid"]
        row["position"] = {"token": "dn", "side": "down", "shares": 10.0, "cost": 9.0, "dumped": False,
                           "proceeds": 0.0, "settled": False, "dry_run": False}
        runner.tick(); clock[0] += 3.0; runner.tick()
        self.assertEqual(len(seen[0]["bids"]), 6)
        fetched = []
        runner.gamma_fetch = lambda slug: fetched.append(slug) or "up"
        self.assertIsNone(runner.request_gamma("s"))
        self.assertEqual(fetched, [])
        runner._io_once(clock[0])
        self.assertEqual(runner.request_gamma("s"), "up")


class ReviewFixTests(unittest.TestCase):
    def make(self, startup_ts=0, durable=None):
        order = []
        io = FiveMinIO(
            buy=lambda *a: order.append("buy") or {"shares": 25, "cost": 24.0},
            sell=lambda *a: order.append("sell") or {"shares": 25, "proceeds": 5.0},
            reserves=lambda cfg: 0, redeem=lambda *a: None, save=lambda *a: None,
            durable=durable or (lambda: order.append("flush") or True),
            log=lambda event, **kw: order.append(event),
        )
        strat = Strategy(io, startup_ts=startup_ts)
        return strat, order

    def test_market_in_progress_at_startup_is_skipped(self):
        strat, order = self.make(startup_ts=1100)
        strat.add_market(market("cid", 1000), now=1000)
        strat.add_market(market("next", 1300), now=1000)
        self.assertTrue(strat.state["markets"]["cid"]["entered"])
        self.assertEqual(strat.state["markets"]["cid"]["skip_reason"], "startup")
        self.assertFalse(strat.state["markets"]["next"]["entered"])
        self.assertIn("fivemin_skip_startup_market", order)
        loaded = Strategy(strat.io, {"markets": {"old": {"condition_id": "old", "slug": "s", "start": 1000,
                                                        "end": 1300, "up_token": "u", "dn_token": "d",
                                                        "entered": False, "position": None}}}, startup_ts=1100)
        self.assertTrue(loaded.state["markets"]["old"]["entered"])

    def test_buy_and_dump_never_wait_on_a_durable_flush(self):
        strat, order = self.make(durable=lambda: (_ for _ in ()).throw(AssertionError("flush before order")))
        strat.add_market(market("cid", 1000), now=1000)
        row = strat.state["markets"]["cid"]
        row.update(strike=100.0, offset=0.0)
        strat.sample_cash(1200, 1000)
        strat.runs["cid"] = {"up": 1200.0}
        grid = SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15)
        strat.tick(cfg(fivemin_gate_k=0), 1203.0, grid, {"cid": {"up": {"bid": .05}, "down": {"bid": .9}}})
        self.assertIn("buy", order)
        q = {"cid": {"down": {"bid": .19, "bids": [(.19, 50)]}, "up": {"bid": .75}}}
        strat.tick(cfg(), 1210.0, grid, q); strat.tick(cfg(), 1213.0, grid, q)
        self.assertIn("sell", order)

    def test_start_twap_captures_at_market_boundary(self):
        strat, _ = self.make()
        strat.add_market(market("cid", 1000), now=1000)
        grid = PriceGrid()
        for ms in range(900_000, 1_001_000, 100):
            grid.add(ms, 100.0)
        strat.tick(cfg(), 1000.0, grid, {})
        self.assertEqual(strat.state["markets"]["cid"]["start_twap"], 100.0)

    def test_prune_keeps_unsettled_positions(self):
        strat, _ = self.make()
        rows = strat.state["markets"]
        base = {"slug": "s", "start": 0, "end": 300, "up_token": "u", "dn_token": "d", "entered": True}
        rows["empty"] = {**base, "condition_id": "empty", "position": None}
        rows["open"] = {**base, "condition_id": "open", "position": {"shares": 25, "settled": False, "side": "up", "dumped": True}}
        rows["done"] = {**base, "condition_id": "done", "position": {"shares": 25, "settled": True, "redeem_enqueued": True}}
        strat._prune(300 + 6 * 3600 + 1)
        self.assertEqual(set(rows), {"open"})

    def test_stale_btc_blocks_gate(self):
        strat, order = self.make()
        strat.add_market(market("cid", 1000), now=1000)
        row = strat.state["markets"]["cid"]
        row.update(strike=100.0, offset=0.0)
        strat.sample_cash(1200, 1000)
        strat.runs["cid"] = {"up": 1200.0}
        stale = SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: (1203.0 - 6) * 1000)
        strat.tick(cfg(fivemin_gate_k=0), 1203.0, stale, {"cid": {"up": {"bid": .05}, "down": {"bid": .9}}})
        self.assertNotIn("buy", order)
        self.assertIn("fivemin_btc_stale", order)

    def test_boundary_settlement_pnl_and_gamma_fallback_schedule(self):
        calls, redemptions = [], []
        io = FiveMinIO(buy=lambda *a: {}, sell=lambda *a: {}, reserves=lambda cfg: 0,
                       redeem=lambda row, now: redemptions.append(row["condition_id"]), save=lambda *a: None,
                       gamma_winner=lambda slug: calls.append(slug) or "up")
        strategy = Strategy(io, startup_ts=0)
        strategy.add_market(market("cid", 1200), now=1200)
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
        strat = Strategy(io, startup_ts=0)
        strat.add_market(market("cid", 1200), now=1200)
        row = strat.state["markets"]["cid"]
        row.update(entered=True, strike=100, offset=0)
        row["position"] = {"side": "up", "shares": 0, "cost": 31, "proceeds": 0,
                           "dumped": True, "settled": False, "dry_run": False}
        strat.boundaries[1500] = 99
        strat.tick(cfg(), 1501, PriceGrid(), {})
        self.assertIsNone(strat.state["paused_until_day"])
        clone = Strategy(io, json.loads(json.dumps(strat.state)), startup_ts=0)
        self.assertTrue(clone.state["markets"]["cid"]["entered"])
        next_day = datetime(1970, 1, 2, tzinfo=timezone.utc).timestamp()
        clone.tick(cfg(), next_day, PriceGrid(), {})
        self.assertIsNone(clone.state["paused_until_day"])
        self.assertEqual(clone.state["realized"], 0)


if __name__ == "__main__":
    unittest.main()

class CacheBoundTests(unittest.TestCase):
    def test_twap_caches_stay_bounded(self):
        grid = PriceGrid()
        for g in range(1000):
            grid.add(g * 100, 100.0 + g * 0.001)
            grid.twap60(g)
        self.assertLessEqual(len(grid._twap_cache), 8)

        io = FiveMinIO(buy=lambda *a: {}, sell=lambda *a: {}, reserves=lambda c: 0,
                       redeem=lambda *a: None, save=lambda s: None)
        strategy = Strategy(io, startup_ts=0)
        row = market("cache", 1000)
        strategy.add_market(row, now=1000)
        state_row = strategy.state["markets"]["cache"]
        state_row.update(strike=100.0, offset=0.0)
        strategy.sample_cash(1000, 1000)
        for now in range(1000, 1100):
            strategy._proxy(grid, float(now), state_row, dict(DEFAULTS, fivemin_btc_max_age_s=100000))
        self.assertLessEqual(len(strategy._twap_cache), 1)

class RequestedFollowupTests(unittest.TestCase):
    def test_defaults_and_fast_parser(self):
        self.assertEqual(DEFAULTS["fivemin_shares"], 10)
        self.assertEqual(DEFAULTS["fivemin_max_ttm_s"], 120)
        self.assertEqual(DEFAULTS["fivemin_fav_min"], 0.90)
        self.assertEqual(DEFAULTS["fivemin_persist_s"], 2.0)
        self.assertEqual(DEFAULTS["fivemin_persist_last_s"], 1.0)
        self.assertEqual(DEFAULTS["fivemin_dump_held_below"], 0.20)
        self.assertEqual(DEFAULTS["fivemin_dump_other_min"], 0.60)
        self.assertEqual(DEFAULTS["fivemin_daily_stop_usd"], -150.0)
        self.assertEqual(DEFAULTS["fivemin_btc_max_age_s"], 3.0)
        self.assertEqual(parse_agg_trade('{"T":1234,"p":"12.5","x":1}'), (1234.0, 12.5))

    def test_price_grid_twap_matches_legacy_with_gaps_and_out_of_order(self):
        import random
        rng = random.Random(7)
        rows = [(100000 + i * 100, 100 + rng.random()) for i in range(900)]
        shuffled = rows[:]
        rng.shuffle(shuffled)
        grid = PriceGrid()
        for timestamp, price in shuffled:
            grid.add(timestamp, price)
        for end in (1000, 1050, 1100, 1200, 1500, 1800):
            legacy = [grid.price_at(end - 10 * j) for j in range(60)]
            expected = None if any(v is None for v in legacy) else sum(legacy) / 60
            self.assertEqual(grid.twap60(end), expected)
            self.assertEqual(twap60(grid, end), expected)

    def test_stale_btc_age_above_three_blocks_and_up_to_three_allows(self):
        logs = []
        io = FiveMinIO(
            buy=lambda *a: {"shares": 10, "cost": 9}, sell=lambda *a: {}, reserves=lambda c: 0,
            redeem=lambda *a: None, save=lambda s: None, log=lambda e, **k: logs.append(e), durable=lambda: True,
        )
        strategy = Strategy(io, startup_ts=0)
        strategy.add_market(market("cid", 1000), now=1000)
        row = strategy.state["markets"]["cid"]
        row.update(strike=100, offset=0)
        strategy.sample_cash(1200, 1000)
        strategy.runs["cid"] = {"up": 1200}
        for age, expected in ((3.1, False), (3.0, True), (2.5, True), (1.5, True)):
            row["entered"] = False
            row["position"] = None
            strategy.runs["cid"] = {"up": 1200}
            grid = SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda age=age: (1203 - age) * 1000)
            strategy.tick(cfg(fivemin_gate_k=0), 1203, grid,
                          {"cid": {"up": {"bid": .05}, "down": {"bid": .9}}})
            self.assertEqual(bool(row.get("position")), expected)

    def test_eval_logged_once_for_entry_and_terminal_skip(self):
        events = []
        io = FiveMinIO(
            buy=lambda *a: {"shares": 10, "cost": 9}, sell=lambda *a: {}, reserves=lambda c: 0,
            redeem=lambda *a: None, save=lambda s: None, log=lambda e, **k: events.append((e, k)), durable=lambda: True,
        )
        strategy = Strategy(io, startup_ts=0)
        strategy.add_market(market("entry", 1000), now=1000)
        entry = strategy.state["markets"]["entry"]
        entry.update(strike=100, offset=0)
        strategy.sample_cash(1200, 1000)
        strategy.runs["entry"] = {"up": 1200}
        grid = SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15)
        quotes = {"entry": {"up": {"bid": .05}, "down": {"bid": .9}}}
        strategy.tick(cfg(fivemin_gate_k=0), 1203, grid, quotes)
        strategy.tick(cfg(fivemin_gate_k=0), 1204, grid, quotes)
        self.assertEqual([e for e, _ in events if e == "fivemin_eval"], ["fivemin_eval"])
        skipped = Strategy(io, startup_ts=0)
        skipped.add_market(market("skip", 1000), now=1000)
        skipped.state["markets"]["skip"]["entered"] = False
        skipped.tick(cfg(), 1301, grid, {})
        skipped.tick(cfg(), 1302, grid, {})
        skip_rows = [k for e, k in events if e == "fivemin_eval" and k.get("decision") == "skip"]
        self.assertEqual(len(skip_rows), 1)
        self.assertTrue(skip_rows[0]["reason"])

    def test_dump_thresholds(self):
        logs, sells = [], []
        io = FiveMinIO(
            buy=lambda *a: {}, sell=lambda row, pos, book, cfg, now: sells.append(1) or {"shares": 10, "proceeds": 1},
            reserves=lambda c: 0, redeem=lambda *a: None, save=lambda s: None,
            log=lambda e, **k: logs.append(e), durable=lambda: True,
        )
        strategy = Strategy(io, startup_ts=0)
        strategy.add_market(market("dump", 1000), now=1000)
        row = strategy.state["markets"]["dump"]
        row["position"] = {"side": "up", "shares": 10, "cost": 9, "proceeds": 0, "dumped": False, "settled": False}
        for now in (1100, 1102.8):
            strategy.tick(cfg(), now, SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15),
                          {"dump": {"up": {"bid": .19, "bids": []}, "down": {"bid": .60, "bids": []}}})
        self.assertFalse(sells)
        strategy.tick(cfg(), 1103, SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15),
                      {"dump": {"up": {"bid": .19, "bids": []}, "down": {"bid": .60, "bids": []}}})
        self.assertEqual(sells, [1])
        row["position"] = {"side": "up", "shares": 10, "cost": 9, "proceeds": 0, "dumped": False, "settled": False}
        strategy.runs["dump"].pop("dump", None)
        strategy.tick(cfg(), 1110, SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15),
                      {"dump": {"up": {"bid": .25, "bids": []}, "down": {"bid": .60, "bids": []}}})
        strategy.tick(cfg(), 1113, SimpleNamespace(price_at=lambda _: 100, latest_exchange_ms=lambda: 1e15),
                      {"dump": {"up": {"bid": .25, "bids": []}, "down": {"bid": .60, "bids": []}}})
        self.assertEqual(sells, [1])


class EntryDepthTests(unittest.TestCase):
    setUp = StrategyTests.setUp
    quotes = StrategyTests.quotes

    def enter(self, **extra):
        row = self.strategy.state["markets"]["cid"]
        self.strategy._enter(cfg(**extra), 1203, row, self.book, "up", "down", 1, 0, 3)
        return row

    def set_book(self):
        self.book = self.quotes()["cid"]
        self.book["down"]["asks"] = [
            {"price": "1", "size": "100"}, {"price": ".99", "size": "4"},
            {"price": ".98", "size": "3"}, {"price": ".97", "size": "2"},
        ]

    def test_depth_order_and_cumulative_sizes(self):
        self.set_book()
        self.assertEqual(ask_depth(self.book["down"]), {
            "asks_depth": [[.97, 2], [.98, 3], [.99, 4]], "cum_098": 5, "cum_099": 9})
        self.assertEqual(ask_depth({}), {"asks_depth": [], "cum_098": 0, "cum_099": 0})
        self.assertEqual(ask_depth({"asks": [(1, 5), (.98, 2), (.97, 1)]})["cum_098"], 3)
        self.enter()
        signal = next(fields for event, fields in self.logs if event == "fivemin_signal")
        self.assertEqual(signal["asks_depth"], [[.97, 2], [.98, 3], [.99, 4]])
        self.assertEqual(signal["books"]["down"]["asks"], signal["asks_depth"])

    def test_book_walk_full_partial_and_zero(self):
        self.set_book()
        for shares, limit, expected, notional in ((4, .99, 4, 2 * .97 + 2 * .98),
                                                 (10, .98, 5, 2 * .97 + 3 * .98),
                                                 (10, .96, 0, 0)):
            with self.subTest(shares=shares, limit=limit):
                result = dry_fill(self.book["down"], shares, limit)
                self.assertEqual(result["shares"], expected)
                self.assertAlmostEqual(result["cost"] - result["fees"], notional)
                self.assertAlmostEqual(result["price"], notional / expected if expected else 0)
        self.assertEqual(dry_fill({}, 10, .99)["shares"], 0)

    def test_dry_fill_logs_alert_and_settlement_include_book_fees(self):
        self.set_book()
        row = self.enter(fivemin_dry_run=True, fivemin_buy_limit=.98)
        fees = 2 * .07 * .97 * .03 + 3 * .07 * .98 * .02
        notional = 2 * .97 + 3 * .98
        position = row["position"]
        self.assertEqual(position["shares"], 5)
        self.assertAlmostEqual(position["avg_px"], notional / 5)
        self.assertAlmostEqual(position["cost"], notional + fees)
        fill = next(fields for event, fields in self.logs if event == "fivemin_fill")
        self.assertTrue(fill["dry_run"])
        self.assertEqual(fill["size"], 5)
        self.assertAlmostEqual(fill["fees"], fees)
        self.assertAlmostEqual(fill["price"], notional / 5)
        self.assertIn("[DRY]", self.alerts[-1])
        self.assertIn(f"fill 5 @ {notional / 5:.4f}", self.alerts[-1])
        self.strategy._settle(cfg(fivemin_dry_run=True), 1301, row, "down", "test")
        self.assertAlmostEqual(position["pnl"], 5 - notional - fees)
        self.assertFalse(self.strategy.spends)

    def test_dry_empty_book_logs_zero_fill(self):
        self.book = self.quotes()["cid"]
        row = self.enter(fivemin_dry_run=True)
        self.assertEqual(row["position"]["shares"], 0)
        self.assertEqual(row["position"]["cost"], 0)
        self.assertIn("[DRY]", self.alerts[-1])
        self.assertIn("fill 0 @ 0.0000", self.alerts[-1])

    def test_buy_error_and_success_save_and_log_after_buy(self):
        from unittest.mock import patch
        import buy.fivemin as module
        for fails in (False, True):
            with self.subTest(fails=fails):
                self.setUp()
                self.set_book()
                order = []
                clock = [1203.0]
                self.io.clock = lambda: clock[0]
                original_save, original_log = self.io.save, self.io.log
                self.io.save = lambda state: (order.append("save"), original_save(state))
                self.io.log = lambda event, **fields: (order.append(event), original_log(event, **fields))
                def buy(*args):
                    self.assertTrue(self.strategy.state["markets"]["cid"]["entered"])
                    self.assertEqual(order, [])
                    order.append("buy")
                    clock[0] += .125
                    if fails:
                        raise RuntimeError("exchange unavailable")
                    return {"shares": 2, "cost": 1.96}
                self.io.buy = buy
                original_copy = module.deepcopy
                def copy_after_buy(value):
                    self.assertIn("buy", order)
                    order.append("copy")
                    return original_copy(value)
                with patch.object(module, "deepcopy", side_effect=copy_after_buy):
                    row = self.enter()
                self.assertLess(order.index("buy"), order.index("save"))
                self.assertLess(order.index("buy"), order.index("fivemin_signal"))
                self.assertTrue(self.saved[-1]["markets"]["cid"]["entered"])
                event = "fivemin_order_error" if fails else "fivemin_fill"
                fields = next(fields for name, fields in self.logs if name == event)
                self.assertAlmostEqual(fields["latency_ms"], 125)
                self.assertTrue(self.alerts)
                if fails:
                    self.assertEqual(fields["error"], "exchange unavailable")
                    self.assertEqual(fields["slug"], row["slug"])
                    self.assertEqual(fields["side"], "down")
                    self.assertEqual(fields["limit"], .99)
                    self.assertIsNone(row["position"])
                    clone = Strategy(self.io, self.saved[-1], startup_ts=1204)
                    self.assertTrue(clone.state["markets"]["cid"]["entered"])
                    self.assertTrue(any(name == "fivemin_skip_startup_market" for name, _ in self.logs))

    def test_live_nofill_logs_and_alerts(self):
        self.set_book()
        self.io.buy = lambda *args: {"shares": 0, "cost": 0}
        self.enter()
        fields = next(fields for event, fields in self.logs if event == "fivemin_signal_nofill")
        self.assertEqual(fields["side"], "down")
        self.assertIn("no fill", self.alerts[-1])

    def test_window_depth_maxima_only_include_in_band_ticks(self):
        self.set_book()
        for now, size, up in ((1200, 100, .5), (1201, 7, .05), (1202, 2, .05)):
            self.book["up"]["bid"] = up
            self.book["down"]["asks"] = [(1, 99), (.99, size + 1), (.98, size)]
            self.strategy.tick(cfg(fivemin_gate_k=1000), now, self.grid, {"cid": self.book})
        self.strategy.tick(cfg(), 1301, self.grid, {})
        summary = next(fields for event, fields in self.logs if event == "fivemin_eval")
        self.assertEqual(summary["asks_depth"], [[.98, 2], [.99, 3]])
        self.assertEqual(summary["cum_098"], 2)
        self.assertEqual(summary["cum_099"], 5)
        self.assertEqual(summary["max_cum_098"], 7)
        self.assertEqual(summary["max_cum_099"], 15)


class MintDisabledLoopTests(unittest.TestCase):
    def test_disabled_returns_after_stop_check_before_maintenance(self):
        from unittest.mock import Mock
        source = (ROOT / "mintbot.py").read_text()
        # Read only the mint-loop preamble under test.
        preamble = source.split("def run_mint_cycle(", 1)[1].split("    markets = gateway.discover", 1)[0]
        tree = ast.parse("def run_mint_cycle(" + preamble)
        heartbeats = Mock()
        stop = Mock(return_value=False)
        ns = dict(MarketGateway=object, ChainReader=object, time=SimpleNamespace(time=lambda: 1200),
                  STOP_FILE=SimpleNamespace(exists=stop), write_loop_heartbeat=heartbeats,
                  _STATE_WRITER=object(), _prune_state_tick=Mock(),
                  fail_stale_submitting_intents=Mock(), reconcile_intents=Mock())
        exec(compile(tree, "mint-loop", "exec"), ns)
        self.assertEqual(ns["run_mint_cycle"]({"entry_enabled": False}, {}, None, None), "disabled")
        stop.assert_called_once_with()
        heartbeats.assert_called_once_with("mint", "disabled")
        for name in ("_prune_state_tick", "fail_stale_submitting_intents", "reconcile_intents"):
            ns[name].assert_not_called()
        stop.return_value = True
        self.assertEqual(ns["run_mint_cycle"]({"entry_enabled": False}, {}, None, None), "stopped")
        self.assertEqual(heartbeats.call_args.args, ("mint", "stopped"))


    def test_enabled_keeps_maintenance_before_wait_submit(self):
        from contextlib import nullcontext
        source = (ROOT / "mintbot.py").read_text()
        preamble = source.split("def run_mint_cycle(", 1)[1].split("    markets = gateway.discover", 1)[0]
        calls = []
        ns = dict(MarketGateway=object, ChainReader=object, time=SimpleNamespace(time=lambda: 1200),
                  STOP_FILE=SimpleNamespace(exists=lambda: False),
                  write_loop_heartbeat=lambda *args: calls.append(args), _STATE_WRITER=object(),
                  _prune_state_tick=lambda *args: calls.append("prune"),
                  fail_stale_submitting_intents=lambda *args: calls.append("fail_stale"),
                  reconcile_intents=lambda *args, **kwargs: calls.append("reconcile"),
                  os=SimpleNamespace(getenv=lambda _: "funder"), STATE_LOCK=nullcontext(),
                  skip_mint_discovery_for_sell=lambda *args: False,
                  to_checksum_address=lambda value: value,
                  commit_state=lambda *args: calls.append("commit"))
        exec(compile(ast.parse("def run_mint_cycle(" + preamble), "mint-loop", "exec"), ns)
        result = ns["run_mint_cycle"]({"entry_enabled": True},
                                       {"intents": {"cid": {"status": "submitting"}}}, None, None)
        self.assertEqual(result, "wait_submit")
        self.assertEqual(calls, ["prune", "fail_stale", "reconcile", "commit", ("mint", "wait_submit")])
