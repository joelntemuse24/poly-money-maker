"""Slugs, Chainlink TWAP resolution, and the strike check."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from buy.lock_config import DEFAULTS, apply_defaults
from buy.lock_markets import (
    enabled_keys,
    event_slug,
    parse_lock_event,
    resolution_is_chainlink_twap60,
    rtds_symbol,
    series_slug,
    strike_status,
    symbols_for,
)
from buy.oracle_log import (
    RTDS_SYMBOL,
    RtdsTwapFeed,
    SUBSCRIBE_FRAME,
    parse_rtds_live,
    parse_rtds_message,
    parse_rtds_price_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]


def _event(asset="btc", duration="15m", start=1_791_144_000, source=None, price=None):
    if source is None:
        source = f"https://data.chain.link/streams/{asset}-usd-twap-60s-streams"
    meta = {} if price is None else {"priceToBeat": price}
    return {
        "slug": f"{asset}-updown-{duration}-{start}",
        "title": f"{asset} up or down",
        "eventMetadata": meta,
        "markets": [
            {
                "slug": f"{asset}-updown-{duration}-{start}",
                "conditionId": "0x" + "ab" * 32,
                "question": "Up or Down",
                "outcomes": "[\"Up\", \"Down\"]",
                "clobTokenIds": "[\"111\", \"222\"]",
                "resolutionSource": source,
                "acceptingOrders": True,
            }
        ],
    }


class MarketTests(unittest.TestCase):
    def test_slug_and_series_patterns(self):
        self.assertEqual(event_slug("btc", "15m", 1_791_144_000), "btc-updown-15m-1791144000")
        self.assertEqual(series_slug("btc", "15m"), "btc-up-or-down-15m")
        self.assertEqual(event_slug("eth", "15m", 1_791_144_000), "eth-updown-15m-1791144000")
        self.assertEqual(event_slug("sol", "15m", 1_791_144_000), "sol-updown-15m-1791144000")
        self.assertEqual(event_slug("xrp", "15m", 1_791_144_000), "xrp-updown-15m-1791144000")
        self.assertEqual(event_slug("btc", "5m", 1_791_144_600), "btc-updown-5m-1791144600")
        self.assertEqual(rtds_symbol("btc"), "btc/usd")
        self.assertEqual(rtds_symbol("eth"), "eth/usd")
        self.assertEqual(rtds_symbol("sol"), "sol/usd")
        self.assertEqual(rtds_symbol("xrp"), "xrp/usd")

    def test_resolution_requires_the_chainlink_60s_stream(self):
        self.assertTrue(
            resolution_is_chainlink_twap60("https://data.chain.link/streams/btc-usd-twap-60s-streams")
        )
        self.assertTrue(
            resolution_is_chainlink_twap60("https://data.chain.link/streams/eth-usd-twap-60s-streams")
        )
        self.assertFalse(resolution_is_chainlink_twap60("https://data.chain.link/streams/btc-usd"))
        self.assertFalse(resolution_is_chainlink_twap60(""))
        ok = parse_lock_event(_event())
        self.assertIsNotNone(ok)
        assert ok is not None
        self.assertTrue(ok.resolution_ok)
        self.assertEqual(ok.end_ts - ok.start_ts, 900)
        self.assertEqual(ok.up_token, "111")
        self.assertEqual(ok.lane, "btc_15m")
        bad = parse_lock_event(_event(source="https://data.chain.link/streams/btc-usd"))
        assert bad is not None
        self.assertFalse(bad.resolution_ok)

    def test_requested_books_are_on_and_alt_5m_stays_off(self):
        keys = enabled_keys(DEFAULTS)
        self.assertEqual(keys, ["btc_15m", "btc_5m"])
        self.assertEqual(symbols_for(DEFAULTS), ["btc/usd"])
        example = json.loads((ROOT / "lockbot.example.json").read_text(encoding="utf-8"))
        cfg = apply_defaults(example)
        self.assertTrue(cfg["dry_run"])
        self.assertTrue(cfg["strategy1_enabled"])
        self.assertTrue(cfg["strategy2_enabled"])
        self.assertEqual(cfg["combined_per_market_usd"], 20.0)
        self.assertEqual(cfg["clip_usd"], 5.0)
        self.assertEqual(cfg["max_open_exposure_usd"], 60.0)
        self.assertEqual(cfg["daily_loss_stop_usd"], 60.0)
        self.assertEqual(cfg["ask_min"], 0.02)
        self.assertEqual(cfg["max_pay"], 0.97)
        self.assertNotIn("p_min", example)
        self.assertEqual(cfg["market_rules"]["btc_15m"]["Z"], 0.0)
        self.assertEqual(cfg["market_rules"]["btc_15m"]["Pmax"], 0.97)
        self.assertEqual(cfg["market_rules"]["btc_5m"]["Z"], 0.25)
        self.assertEqual(cfg["market_rules"]["btc_5m"]["Pmax"], 0.90)
        self.assertIsNone(cfg["s2_q_edge_min"])
        for name in ("eth_15m", "sol_15m", "xrp_15m", "eth_5m", "sol_5m", "xrp_5m"):
            self.assertFalse(cfg["markets"][name])

    def test_strike_must_match_gamma_when_both_exist(self):
        strike, reason = strike_status(100.0, None)
        self.assertEqual((strike, reason), (100.0, "rtds"))
        strike, reason = strike_status(None, 100.0)
        self.assertEqual((strike, reason), (100.0, "gamma"))
        strike, reason = strike_status(None, None)
        self.assertEqual(reason, "strike_unknown")
        strike, reason = strike_status(100.0, 100.005, match_usd=0.01, match_rel=0.0001)
        self.assertEqual(reason, "match")
        strike, reason = strike_status(100.0, 101.0, match_usd=0.01, match_rel=0.0001)
        self.assertEqual(reason, "strike_mismatch")
        self.assertIsNone(strike)
        # XRP-sized relative tolerance: 1bp of 1.50 is $0.00015, absolute floor $0.01.
        strike, reason = strike_status(1.50, 1.505, match_usd=0.01, match_rel=0.0001)
        self.assertEqual(reason, "match")

    def test_default_parser_still_ignores_other_symbols(self):
        eth = {
            "topic": "crypto_prices_twap_sixty",
            "type": "update",
            "payload": {
                "symbol": "eth/usd",
                "timestamp": 1_790_063_444_000,
                "window_s": 60,
                "full_accuracy_value": "2700000000000000000000",
            },
        }
        self.assertEqual(parse_rtds_message(json.dumps(eth)), [])
        self.assertEqual(parse_rtds_message(json.dumps(eth), symbols=["eth/usd"])[0].symbol, "eth/usd")
        live = {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "payload": {
                "symbol": "eth/usd",
                "timestamp": 1_790_063_444_000,
                "value": 2700.5,
                "full_accuracy_value": "2700500000000000000000",
            },
        }
        self.assertIsNone(parse_rtds_live(json.dumps(live)))
        self.assertIsNotNone(parse_rtds_live(json.dumps(live), symbols=["eth/usd"]))
        feed = RtdsTwapFeed()
        self.assertEqual(feed.symbols, (RTDS_SYMBOL,))
        self.assertEqual(feed.subscribe_frame(), SUBSCRIBE_FRAME)

    def test_history_keeps_the_snapshot_and_a_later_print(self):
        feed = RtdsTwapFeed(symbols=("eth/usd",), history_s=1200, clock=lambda: 5000.0)
        snapshot = {
            "topic": "crypto_prices",
            "type": "subscribe",
            "payload": {
                "symbol": "eth/usd",
                "data": [
                    {"timestamp": 4990, "value": 2700.0},
                    {"timestamp": 4991, "value": 2701.0},
                ],
            },
        }
        feed.handle_message(json.dumps(snapshot))
        update = {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "payload": {
                "symbol": "eth/usd",
                "timestamp": 4992,
                "value": 2702.0,
                "full_accuracy_value": "2702000000000000000000",
            },
        }
        feed.handle_message(json.dumps(update))
        hist = feed.live_history()
        self.assertEqual([round(row[2], 4) for row in hist], [2700.0, 2701.0, 2702.0])
        self.assertEqual(feed.latest_live().obs_ts, 4992.0)
        self.assertEqual(parse_rtds_price_snapshot(json.dumps(snapshot), symbols=["btc/usd"]), [])


if __name__ == "__main__":
    unittest.main()
