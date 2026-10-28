"""Comprehensive tests for Track 2: pre-signed CLOB orders, sub-100ms Binance ticks, and 5m sniper."""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mintbot
from buy.binance_trades import BinanceTrades, PriceGrid, parse_agg_trade, parse_price_message
from buy.fivemin import DEFAULTS, FiveMinIO, Runner, Strategy
from buy.mint_sell import DEFAULT_SELL_KNOBS, sell_dump_dual_persist_s
from buy.presign import (
     PreSignedOrderCache,
    _SECRET_CACHE,
    build_order_payload,
    configure_low_latency_sockets,
    configure_socket_nodelay,
    fast_l2_headers,
    fast_post_order,
    fast_serialize_json,
    get_presign_cache,
    select_fastest_cf_edge,
)


def _market(cid="cid1", start=1000, up="tok_up", down="tok_dn"):
    return SimpleNamespace(
        condition_id=cid,
        slug=f"btc-up-or-down-5m-{start}",
        start_ts=start,
        end_ts=start + 300,
        up_token=up,
        dn_token=down,
        active=True,
        closed=False,
        neg_risk=False,
    )


class PreSignUnitTests(unittest.TestCase):
    def setUp(self):
        get_presign_cache().clear()

    def test_fast_serialize_json_compact(self):
        payload = {"order": {"salt": 123, "side": "BUY"}, "orderType": "FAK", "deferExec": True}
        out = fast_serialize_json(payload)
        self.assertNotIn(" ", out)
        self.assertEqual(json.loads(out), payload)

    def test_fast_l2_headers_matches_py_clob_hmac_and_caches_secret(self):
        from py_clob_client_v2.signing.hmac import build_hmac_signature

        raw_secret = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("utf-8")
        _SECRET_CACHE.pop(raw_secret, None)
        creds = SimpleNamespace(
            api_key="key-1",
            api_secret=raw_secret,
            api_passphrase="pass-1",
        )
        body = '{"order":{"salt":1},"orderType":"FAK"}'
        headers = fast_l2_headers("0xabc", creds, "POST", "/order", body, timestamp=1700000000)
        expected_sig = build_hmac_signature(raw_secret, 1700000000, "POST", "/order", body)
        self.assertEqual(headers["POLY_ADDRESS"], "0xabc")
        self.assertEqual(headers["POLY_SIGNATURE"], expected_sig)
        self.assertEqual(headers["POLY_TIMESTAMP"], "1700000000")
        self.assertEqual(headers["POLY_API_KEY"], "key-1")
        self.assertEqual(headers["POLY_PASSPHRASE"], "pass-1")
        self.assertIn(raw_secret, _SECRET_CACHE)

    def test_presigned_cache_put_pop_single_use_ttl_and_lru(self):
        cache = PreSignedOrderCache(ttl_s=10.0, max_entries=2)
        client = object()
        e1 = cache.put(client, "tok1", "BUY", 10.0, 0.95, {"salt": 1}, owner="k1")
        self.assertEqual(cache.size(), 1)
        self.assertEqual(e1.token_id, "tok1")
        popped = cache.pop(client, "tok1", "buy", 10.0, 0.95, "FAK")
        self.assertIs(popped, e1)
        # Single-use: second pop returns None so order salt is never reused.
        self.assertIsNone(cache.pop(client, "tok1", "BUY", 10.0, 0.95, "FAK"))

        # TTL expiration
        short_cache = PreSignedOrderCache(ttl_s=0.01, max_entries=4)
        short_cache.put(client, "tok2", "SELL", 5.0, 0.50, {"salt": 2})
        time.sleep(0.02)
        self.assertIsNone(short_cache.pop(client, "tok2", "SELL", 5.0, 0.50))

        # Max entries LRU eviction
        cache.put(client, "a", "BUY", 1.0, 0.5, {"s": 1})
        cache.put(client, "b", "BUY", 1.0, 0.5, {"s": 2})
        cache.put(client, "c", "BUY", 1.0, 0.5, {"s": 3})
        self.assertEqual(cache.size(), 2)
        self.assertIsNone(cache.pop(client, "a", "BUY", 1.0, 0.5))
        self.assertIsNotNone(cache.pop(client, "b", "BUY", 1.0, 0.5))

    def test_presign_market_order_builds_and_stores_entry(self):
        cache = PreSignedOrderCache()
        built = []

        class DummyBuilder:
            def build_market_order(self, args, options, version=2):
                built.append((args.token_id, args.amount, args.price, args.side, options.tick_size, options.neg_risk, version))
                return {"signed": True, "amount": args.amount}

        client = SimpleNamespace(
            builder=DummyBuilder(),
            builder_config=SimpleNamespace(builder_code="bcode"),
            signer=SimpleNamespace(address=lambda: "0xsigner"),
        )
        entry = cache.presign_market_order(
            client, "tok_up", "BUY", 9.70, 0.97, tick_size="0.01", neg_risk=False, version=2
        )
        self.assertIsNotNone(entry)
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0][0], "tok_up")
        self.assertAlmostEqual(built[0][1], 9.70)
        self.assertAlmostEqual(built[0][2], 0.97)
        popped = cache.pop(client, "tok_up", "BUY", 9.70, 0.97, "FAK")
        self.assertIs(popped, entry)

    def test_fast_post_order_uses_direct_post_on_clob_client_and_fallback_otherwise(self):
        raw_secret = base64.urlsafe_b64encode(b"secret_bytes_for_test_1234567890").decode("utf-8")
        posts = []

        class ClobClient:
            host = "https://clob.polymarket.com"
            creds = SimpleNamespace(api_key="k", api_secret=raw_secret, api_passphrase="p")
            signer = SimpleNamespace(address=lambda: "0x123")

            def _post(self, url, headers=None, data=None):
                posts.append((url, headers, data))
                return {"status": "matched", "takingAmount": "10"}

            def post_order(self, *_a, **_k):
                raise AssertionError("slow post_order should not be called when fast_post_order hits")

        clob = ClobClient()
        cache = PreSignedOrderCache()
        entry = cache.put(clob, "tok", "BUY", 9.5, 0.95, {"salt": 99}, owner="0x123")
        res = fast_post_order(clob, entry.signed_order, entry=entry, order_type="FAK")
        self.assertEqual(res["status"], "matched")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][0], "https://clob.polymarket.com/order")
        self.assertEqual(posts[0][2], entry.serialized_body)
        self.assertIn("POLY_SIGNATURE", posts[0][1])

        # Fallback for mock/non-ClobClient
        fallback_client = SimpleNamespace(post_order=lambda s, order_type=None: {"status": "fallback", "order": s})
        res2 = fast_post_order(fallback_client, {"salt": 1}, entry=entry, order_type="FAK")
        self.assertEqual(res2["status"], "fallback")

    def test_socket_nodelay_and_cf_edge_probe(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)
        port = srv.getsockname()[1]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                self.assertTrue(configure_socket_nodelay(s))
                self.assertTrue(configure_low_latency_sockets(s))
            finally:
                s.close()
            info = configure_low_latency_sockets(None)
            self.assertIsInstance(info, dict)
            self.assertTrue(info["tcp_nodelay"])

            probe = select_fastest_cf_edge(
                host="localhost", candidates=["127.0.0.1"], port=port, timeout=0.5
            )
            self.assertEqual(probe["fastest_ip"], "127.0.0.1")
            self.assertIsNotNone(probe["latency_ms"])
            self.assertTrue(probe["results"][0]["ok"])
        finally:
            srv.close()


class BinanceTradesSubSecondTests(unittest.TestCase):
    def test_price_grid_500ms_window_extrema_and_jump(self):
        grid = PriceGrid()
        # Add sub-100ms ticks within a 500ms window
        grid.add(1000.0, 70000.0)
        grid.add(1120.0, 69995.0)
        grid.add(1250.0, 70010.0)
        grid.add(1420.0, 70032.0)

        self.assertEqual(grid.latest_spot(), (1420.0, 70032.0))
        ext = grid.window_extrema(window_ms=500.0)
        self.assertEqual(ext, (70032.0, 69995.0, 70032.0, 1420.0))

        jmp = grid.jump_in_window(window_ms=500.0)
        self.assertIsNotNone(jmp)
        self.assertAlmostEqual(jmp["up_jump"], 37.0)
        self.assertAlmostEqual(jmp["down_jump"], 0.0)
        self.assertAlmostEqual(jmp["spot"], 70032.0)

        # Downward jump
        grid.add(1550.0, 69980.0)
        jmp_dn = grid.jump_in_window(window_ms=500.0)
        self.assertAlmostEqual(jmp_dn["down_jump"], 52.0)

    def test_parse_agg_trade_book_ticker_coinbase_and_combined_stream(self):
        # 1. Standard Binance aggTrade
        self.assertEqual(parse_agg_trade('{"T": 1700000100, "p": "71234.5"}'), (1700000100.0, 71234.5))

        # 2. Binance bookTicker
        self.assertEqual(
            parse_price_message('{"T": 1700000200, "b": "71000.0", "a": "71002.0"}'),
            (1700000200.0, 71001.0),
        )

        # 3. Combined stream wrapper
        wrapped = json.dumps({"stream": "btcusdt@bookTicker", "data": {"E": 1700000300, "b": "72000", "a": "72004"}})
        self.assertEqual(parse_agg_trade(wrapped), (1700000300.0, 72002.0))

        # 4. Coinbase ticker
        cb = json.dumps({"type": "ticker", "price": "71500.25", "time": "2026-10-28T11:00:00.500Z"})
        parsed_cb = parse_agg_trade(cb)
        self.assertIsNotNone(parsed_cb)
        self.assertAlmostEqual(parsed_cb[1], 71500.25)

    def test_binance_trades_on_tick_and_extra_urls(self):
        grid = PriceGrid()
        ticks = []
        urls_connected = []

        class FakeWS:
            def __init__(self, messages):
                self._messages = list(messages)

            def recv(self):
                if self._messages:
                    return self._messages.pop(0)
                return ""

            def close(self):
                pass

        def fake_connect(url):
            urls_connected.append(url)
            if "fstream" in url:
                return FakeWS(['{"stream":"btcusdt@bookTicker","data":{"T":2000,"b":"70100","a":"70102"}}'])
            return FakeWS(['{"T":1950,"p":"70050"}'])

        bt = BinanceTrades(
            grid,
            url="wss://stream.binance.com:9443/ws/btcusdt@aggTrade",
            connect=fake_connect,
            on_tick=lambda ms, px: (ticks.append((ms, px)), bt.stop()),
            extra_urls=["wss://fstream.binance.com/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker"],
        )
        bt.start()
        deadline = time.time() + 2.0
        while not ticks and time.time() < deadline:
            time.sleep(0.01)
        bt.stop()
        self.assertTrue(ticks)
        self.assertIsNotNone(grid.latest_spot())


class FiveMinSniperTests(unittest.TestCase):
    def _setup_strategy(self, **cfg_overrides):
        events = []
        buys = []
        saves = []

        def fake_buy(token, shares, limit, is_dry):
            buys.append((token, shares, limit, is_dry))
            return {"shares": shares, "cost": shares * 0.94, "price": 0.94, "status": "matched", "timing": {"sign": 0.01}}

        io = FiveMinIO(
            buy=fake_buy,
            sell=lambda *_a: {"shares": 0, "proceeds": 0.0, "status": "matched"},
            reserves=lambda _cfg: 0.0,
            redeem=lambda *_a: None,
            save=lambda state: saves.append(state),
            log=lambda event, **kw: events.append((event, kw)),
            clock=lambda: 1100.0,
        )
        cfg = {
            **DEFAULTS,
            "fivemin_enabled": True,
            "fivemin_dry_run": False,
            "sniper_5m_enabled": True,
            "sniper_jump_usd": 25.0,
            "sniper_window_ms": 500,
            "sniper_min_gap_usd": 20.0,
            "sniper_max_ask": 0.97,
            "sniper_min_ask": 0.50,
            "sniper_ttm_min_s": 15,
            "sniper_ttm_max_s": 240,
            "sniper_shares": 10,
            **cfg_overrides,
        }
        strat = Strategy(io, startup_ts=900.0)
        strat.add_market(_market("cid1", start=1000), now=950.0)
        strat.sample_cash(1090.0, 500.0)
        row = strat.state["markets"]["cid1"]
        row["strike"] = 70000.0
        row["offset"] = 0.0
        return strat, row, cfg, events, buys

    def test_sniper_triggers_on_500ms_jump_and_settles(self):
        strat, row, cfg, events, buys = self._setup_strategy()
        grid = PriceGrid()
        grid.add(1099600.0, 70000.0)
        grid.add(1100000.0, 70030.0)  # +$30 jump in 400ms, +$30 above strike

        quotes = {
            "cid1": {
                "up": {"bid": 0.92, "ask": 0.94, "asks": [[0.94, 25.0]], "bids": [[0.92, 25.0]]},
                "down": {"bid": 0.06, "ask": 0.08, "asks": [[0.08, 25.0]], "bids": [[0.06, 25.0]]},
            }
        }
        strat.tick(cfg, 1100.0, grid, quotes)
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0][0], "tok_up")
        self.assertEqual(buys[0][1], 10.0)
        self.assertEqual(buys[0][2], 0.97)
        self.assertIn("sniper_5m", row)
        self.assertEqual(row["sniper_5m"]["position"]["shares"], 10.0)
        self.assertTrue(any(e == "fivemin_sniper_fill" for e, _ in events))

        # Second tick in same market does not fire again
        strat.tick(cfg, 1100.2, grid, quotes)
        self.assertEqual(len(buys), 1)

        # Settlement at market end
        strat.boundaries[1300] = 70050.0
        strat.tick(cfg, 1301.0, grid, quotes)
        self.assertTrue(row["sniper_5m"]["position"]["settled"])
        self.assertAlmostEqual(row["sniper_5m"]["position"]["pnl"], 10.0 - 9.4)
        self.assertTrue(any(e == "fivemin_sniper_settle" for e, _ in events))

    def test_sniper_skips_when_ask_above_cap_or_gap_too_small(self):
        strat, row, cfg, _events, buys = self._setup_strategy()
        grid = PriceGrid()
        grid.add(1099600.0, 70000.0)
        grid.add(1100000.0, 70030.0)

        # Ask 0.98 > sniper_max_ask 0.97 -> skip
        high_ask_quotes = {
            "cid1": {
                "up": {"bid": 0.96, "ask": 0.98, "asks": [[0.98, 25.0]]},
                "down": {"bid": 0.02, "ask": 0.04, "asks": [[0.04, 25.0]]},
            }
        }
        strat.tick(cfg, 1100.0, grid, high_ask_quotes)
        self.assertEqual(len(buys), 0)

        # Gap $10 < sniper_min_gap_usd $20 -> skip
        grid2 = PriceGrid()
        grid2.add(1099600.0, 69980.0)
        grid2.add(1100000.0, 70010.0)  # +$30 jump, but only +$10 over 70000 strike
        good_ask_quotes = {
            "cid1": {
                "up": {"bid": 0.92, "ask": 0.94, "asks": [[0.94, 25.0]]},
                "down": {"bid": 0.06, "ask": 0.08, "asks": [[0.08, 25.0]]},
            }
        }
        strat.tick(cfg, 1100.0, grid2, good_ask_quotes)
        self.assertEqual(len(buys), 0)

    def test_runner_on_binance_tick_event_driven_sniper(self):
        strat, row, cfg, _events, buys = self._setup_strategy()
        grid = PriceGrid()
        grid.add(1099600.0, 70000.0)
        grid.add(1100000.0, 70030.0)

        books = Mock()
        books.usable.side_effect = lambda tok: SimpleNamespace(
            bid=0.92 if tok == "tok_up" else 0.06,
            ask=0.94 if tok == "tok_up" else 0.08,
            bids=[[0.92, 20.0]],
            asks=[[0.94, 20.0]] if tok == "tok_up" else [[0.08, 20.0]],
            age=0.05,
        )
        rtds = SimpleNamespace(drain=lambda: [], start=lambda: None, stop=lambda: None)
        binance = SimpleNamespace(start=lambda: None, stop=lambda: None, _on_tick=None)
        runner = Runner(
            strat,
            lambda: cfg,
            Mock(),
            Mock(),
            "0xfunder",
            grid,
            binance,
            rtds,
            books,
            clock=lambda: 1100.0,
        )
        runner.current = row
        runner.on_binance_tick(1100000.0, 70030.0)
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0][0], "tok_up")

    def test_sniper_blocks_stale_book_age_ms(self):
        strat, row, cfg, _events, buys = self._setup_strategy()
        cfg["sniper_max_book_age_ms"] = 1500.0
        row["offset"] = -80.0
        row["strike"] = 69900.0
        grid = PriceGrid()
        grid.add(1099600.0, 70000.0)
        grid.add(1100000.0, 70030.0)

        stale_quotes = {
            "cid1": {
                "up": {"bid": 0.74, "ask": 0.75, "asks": [[0.75, 25.0]], "src": "rest", "age_ms": 1600.0},
                "down": {"bid": 0.24, "ask": 0.25, "asks": [[0.25, 25.0]], "src": "rest", "age_ms": 1600.0},
            }
        }
        strat.tick(cfg, 1100.0, grid, stale_quotes)
        self.assertEqual(len(buys), 0)

        fresh_quotes = {
            "cid1": {
                "up": {"bid": 0.74, "ask": 0.75, "asks": [[0.75, 25.0]], "src": "ws", "age_ms": 80.0},
                "down": {"bid": 0.24, "ask": 0.25, "asks": [[0.25, 25.0]], "src": "ws", "age_ms": 80.0},
            }
        }
        strat.tick(cfg, 1100.0, grid, fresh_quotes)
        self.assertEqual(len(buys), 1)

    def test_runner_releases_tick_lock_during_books_poll(self):
        strat, row, cfg, _events, buys = self._setup_strategy()
        row["offset"] = 0.0
        grid = PriceGrid()
        grid.add(1099600.0, 70000.0)
        grid.add(1100000.0, 70030.0)

        books = Mock()
        books.source.return_value = "ws"
        books.usable.side_effect = lambda tok: SimpleNamespace(
            bid=0.74 if tok == "tok_up" else 0.24,
            ask=0.75 if tok == "tok_up" else 0.25,
            bids=[[0.74, 20.0]],
            asks=[[0.75, 20.0]] if tok == "tok_up" else [[0.25, 20.0]],
            age=0.05,
        )
        rtds = SimpleNamespace(drain=lambda: [], start=lambda: None, stop=lambda: None)
        binance = SimpleNamespace(start=lambda: None, stop=lambda: None, _on_tick=None)
        runner = Runner(
            strat,
            lambda: cfg,
            Mock(),
            Mock(),
            "0xfunder",
            grid,
            binance,
            rtds,
            books,
            clock=lambda: 1100.0,
        )
        def slow_poll(now, up_tok, dn_tok):
            runner.on_binance_tick(1100000.0, 70030.0)

        books.poll.side_effect = slow_poll
        runner.tick()
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0][0], "tok_up")


    def test_price_grid_isolates_extra_sources_from_primary_jump_window(self):
        grid = PriceGrid()
        # Primary Spot tick at $80,500
        grid.add(1000.0, 80500.0, source="primary")
        # Extra Futures stream tick at $80,460 (-$40 basis) must NOT pollute primary jump window
        grid.add(1100.0, 80460.0, source="wss://fstream.binance.com/stream")
        # Next Primary Spot tick at $80,501 (+$1 true spot move)
        grid.add(1200.0, 80501.0, source="primary")

        jmp = grid.jump_in_window(500.0)
        self.assertIsNotNone(jmp)
        self.assertAlmostEqual(jmp["up_jump"], 1.0)
        self.assertAlmostEqual(jmp["down_jump"], 0.0)
        self.assertAlmostEqual(jmp["min"], 80500.0)

        # Querying the futures source explicitly uses its own ring buffer
        jmp_fut = grid.jump_in_window(500.0, source="wss://fstream.binance.com/stream")
        self.assertIsNotNone(jmp_fut)
        self.assertAlmostEqual(jmp_fut["spot"], 80460.0)
        self.assertAlmostEqual(jmp_fut["up_jump"], 0.0)

    def test_sniper_use_offset_default_and_blocks_when_offset_missing_or_basis_adjusted_gap_small(self):
        from buy.fivemin import DEFAULTS as FM_DEFAULTS
        self.assertIs(FM_DEFAULTS["sniper_use_offset"], True)
        self.assertIs(FM_DEFAULTS["sniper_futures_ws_enabled"], False)
        self.assertIs(mintbot.DEFAULTS["sniper_use_offset"], True)
        self.assertIs(mintbot.DEFAULTS["sniper_futures_ws_enabled"], False)

        strat, row, cfg, _events, buys = self._setup_strategy()
        grid = PriceGrid()
        # Spot jumps +$30 from 80470 to 80500, while Chainlink strike is 80400 and offset is -85.0
        # True Chainlink-adjusted price = 80500 - 85 = 80415 (+15 gap < 20 min_gap_usd)
        grid.add(1099600.0, 80470.0)
        grid.add(1100000.0, 80500.0)
        row["strike"] = 80400.0
        quotes = {
            "cid1": {
                "up": {"bid": 0.90, "ask": 0.92, "asks": [[0.92, 25.0]]},
                "down": {"bid": 0.08, "ask": 0.10, "asks": [[0.10, 25.0]]},
            }
        }
        # 1. offset is None -> blocks entry
        row["offset"] = None
        strat.tick(cfg, 1100.0, grid, quotes)
        self.assertEqual(len(buys), 0)

        # 2. offset = -85.0 -> adjusted gap is +$15 < $20 -> blocks entry
        row["offset"] = -85.0
        strat.tick(cfg, 1100.0, grid, quotes)
        self.assertEqual(len(buys), 0)

        # 3. offset = -75.0 -> adjusted gap is +$25 >= $20 -> fires entry!
        row["offset"] = -75.0
        strat.tick(cfg, 1100.0, grid, quotes)
        self.assertEqual(len(buys), 1)

    def test_buy_matched_shares_and_scale_fill_amount_preserve_cheap_ask_sweep_shares(self):
        from buy.sister_bid import buy_matched_shares
        from buy.mint_sell import buy_fill_vwap

        # Buying $9.69 at ~0.41 ask with target 10 shares yields 23.634147 shares (> 2x target)
        raw_reply = {
            "status": "matched",
            "takingAmount": "23.634147",
            "makingAmount": "9.69",
        }
        shares = buy_matched_shares(raw_reply, 10.0)
        self.assertAlmostEqual(shares, 23.634147, places=6)
        self.assertAlmostEqual(mintbot._scale_fill_amount("23.634147", 10.0), 23.634147, places=6)
        vwap = buy_fill_vwap(raw_reply, shares)
        self.assertAlmostEqual(vwap, 0.41, places=4)

        # Unscaled 6-decimal integer string still scales by 1e6
        self.assertAlmostEqual(buy_matched_shares({"takingAmount": "23634147"}, 10.0), 23.634147, places=6)
        self.assertAlmostEqual(mintbot._scale_fill_amount("23634147", 10.0), 23.634147, places=6)


class MintbotPresignAndDualDumpTests(unittest.TestCase):
    def setUp(self):
        get_presign_cache().clear()

    def test_dual_dump_persist_shortens_on_oracle_confirmation(self):
        self.assertFalse(DEFAULT_SELL_KNOBS["sell_dump_dual_enabled"])
        cfg = {
            "sell_dump_persist_s": 2.0,
            "sell_dump_dual_enabled": True,
            "sell_dump_dual_persist_s": 0.5,
            "sell_dump_dual_oracle_usd": 15.0,
        }
        # Held 'up' with BTC -$20 below strike -> adverse = +20 >= 15 -> 0.5s
        self.assertAlmostEqual(
            sell_dump_dual_persist_s(cfg, held="up", oracle_gap_usd=-20.0), 0.5
        )
        # Held 'up' with BTC -$5 below strike -> stays 2.0s
        self.assertAlmostEqual(
            sell_dump_dual_persist_s(cfg, held="up", oracle_gap_usd=-5.0), 2.0
        )
        # Held 'dn' with BTC +$18 above strike -> adverse = +18 >= 15 -> 0.5s
        self.assertAlmostEqual(
            sell_dump_dual_persist_s(cfg, held="dn", oracle_gap_usd=18.0), 0.5
        )

    def test_mintbot_fak_buy_and_sell_consume_presigned_cache(self):
        raw_secret = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("utf-8")
        posted = []
        build_calls = []

        class ClobClient:
            host = "https://clob.polymarket.com"
            creds = SimpleNamespace(api_key="k", api_secret=raw_secret, api_passphrase="p")
            signer = SimpleNamespace(address=lambda: "0xabc")
            builder_config = None
            builder = SimpleNamespace(
                build_market_order=lambda *_a, **_k: (build_calls.append(1), {"salt": 1})[1]
            )

            def get_tick_size(self, _tok):
                return "0.01"

            def get_neg_risk(self, _tok):
                return False

            def _ClobClient__resolve_version(self):
                return 2

            def _post(self, url, headers=None, data=None):
                posted.append((url, headers, data))
                return {"status": "matched", "takingAmount": "10", "makingAmount": "9.5"}

        client = ClobClient()
        # Pre-populate cache for BUY (10 shares @ 0.95 -> 9.50 USDC) and SELL (10 shares @ 0.40)
        get_presign_cache().put(client, "tok_buy", "BUY", 9.50, 0.95, {"salt": 101}, owner="0xabc")
        get_presign_cache().put(client, "tok_sell", "SELL", 10.0, 0.40, {"salt": 202}, owner="0xabc")

        with patch.object(mintbot, "_get_clob_client", return_value=client), patch.object(
            mintbot, "_fak_market_cache", {"tok_buy": ("0.01", False)}
        ), patch.object(
            mintbot, "_allowance_box", return_value={"order_options": {"tok_sell": ("0.01", False, 2)}}
        ), patch.object(mintbot, "log_event", lambda *_a, **_k: None):
            timing = {}
            bought, b_status = mintbot._fak_buy("tok_buy", 10.0, 0.95, False, timing=timing)
            self.assertEqual((bought, b_status), (10.0, "matched"))
            self.assertEqual(len(build_calls), 0)  # Skipped build_market_order on cache hit!
            self.assertEqual(len(posted), 1)

            sold, s_status = mintbot._fak_sell("tok_sell", 10.0, 0.40, False)
            self.assertEqual((sold, s_status), (9.5, "matched"))
            self.assertEqual(len(build_calls), 0)  # Skipped build_market_order on cache hit!
            self.assertEqual(len(posted), 2)


    def test_keepalive_always_on_and_schedule_order_prewarm_with_presign_specs(self):
        # 1. Test _start_clob_keepalive with sniper_5m_enabled / clob_keepalive_always_on when no intent is active
        from test_mint_cpu import _load

        state = {"intents": {}}
        requests_seen = []
        threads = []
        ticks = iter([1000.0, 1005.0, 1015.0])
        ns = _load("_start_clob_keepalive")

        def get(url, timeout):
            requests_seen.append((url, timeout))

        calls = {"n": 0}

        def sleep(delay):
            calls["n"] += 1
            if calls["n"] >= 3:
                ns["_shutdown"] = True

        ns.update(
            _shutdown=False,
            _configure_clob_http_client=lambda: SimpleNamespace(
                host="https://clob.polymarket.com", get=get
            ),
            time=SimpleNamespace(time=lambda: next(ticks), sleep=sleep),
            threading=SimpleNamespace(
                Thread=lambda **kw: SimpleNamespace(start=lambda: threads.append(kw))
            ),
        )
        ns["_start_clob_keepalive"](state, cfg={"sniper_5m_enabled": True, "clob_keepalive_interval_s": 12.0})
        threads[0]["target"]()
        # Tick 1 (1000.0 >= 12.0): pings /time; Tick 2 (1005.0 - 1000.0 = 5.0 < 12.0): skips; Tick 3 (1015.0 - 1000.0 = 15.0 >= 12.0): pings /time
        self.assertEqual(len(requests_seen), 2)
        self.assertEqual(requests_seen[0][0], "https://clob.polymarket.com/time")

        # 2. Test _schedule_order_prewarm with presign_specs
        get_presign_cache().clear()
        built = []

        class DummyClient:
            builder_config = None
            signer = SimpleNamespace(address=lambda: "0xabc")
            builder = SimpleNamespace(
                build_market_order=lambda args, opts, version=2: (
                    built.append((args.token_id, args.side, args.amount, args.price)),
                    {"salt": len(built)},
                )[1]
            )

            def get_tick_size(self, _tok):
                return "0.01"

            def get_neg_risk(self, _tok):
                return False

            def _ClobClient__resolve_version(self):
                return 2

            def update_balance_allowance(self, _p):
                pass

        c = DummyClient()
        run_threads = []
        with patch.object(mintbot, "_get_clob_client", return_value=c), patch.object(
            mintbot, "_allowance_box", return_value={"tokens": set(), "collateral": True, "warmed": set(), "order_options": {}}
        ), patch.object(
            mintbot.threading,
            "Thread",
            side_effect=lambda **kw: SimpleNamespace(start=lambda: run_threads.append(kw)),
        ):
            mintbot._schedule_order_prewarm(
                "tok_u",
                "tok_d",
                presign_specs=[{"side": "BUY", "amount": 9.70, "price": 0.97}],
            )
            run_threads[0]["target"]()
        self.assertEqual(len(built), 2)
        self.assertEqual(get_presign_cache().size(), 2)


if __name__ == "__main__":
    unittest.main()
