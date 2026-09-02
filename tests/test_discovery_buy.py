"""Gamma discovery must not veto a buy on a market we already have.

Overnight 2026-09-02: last-60s 90–96 prints were skipped as stale_discovery
because Gamma's snapshot was older than max(10s, 2×discover_cache_s).
Joel: once we are in the last window, the market exists — do not keep
re-checking Gamma for that market. Hedge stays independent of Gamma age.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from buy.entry_skip import is_late_entry_window
from buy.market import (
    MintMarket,
    MarketGateway,
    discovery_allows_buy_look,
    market_is_known_for_buy,
)

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "buybot.py"
BOT5M = ROOT / "buybot5m.py"
BOT_HR = ROOT / "buybothourly.py"

START_TS = 1_784_638_800.0
END_TS = START_TS + 300.0
LATE_WINDOW_S = 60.0


def _market(**overrides) -> MintMarket:
    fields = dict(
        condition_id="0xknown",
        slug=f"btc-updown-5m-{int(START_TS)}",
        question="Bitcoin Up or Down",
        end_ts=END_TS,
        series_slug="btc-up-or-down-5m",
        up_token="up-token",
        dn_token="dn-token",
        active=True,
        closed=False,
        accepting_orders=True,
        neg_risk=False,
        start_ts=START_TS,
    )
    fields.update(overrides)
    return MintMarket(**fields)


def _reaches_book_look(*, discovery_fresh, seconds_left, market, late_window_s=LATE_WINDOW_S):
    """Same decision the 5m buy check uses before look_book_quote."""
    return discovery_allows_buy_look(
        discovery_fresh,
        in_live_window=is_late_entry_window(seconds_left, late_window_s),
        market=market,
    )


def _gamma_event(condition_id: str, start_ts: float, up: str, dn: str) -> dict:
    end_ts = start_ts + 300.0
    start_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    end_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    slug = f"btc-updown-5m-{int(start_ts)}"
    return {
        "slug": slug,
        "title": "Bitcoin Up or Down",
        "active": True,
        "closed": False,
        "endDate": end_iso,
        "startDate": start_iso,
        "markets": [
            {
                "conditionId": condition_id,
                "slug": slug,
                "question": "Bitcoin Up or Down",
                "endDate": end_iso,
                "startDate": start_iso,
                "clobTokenIds": [up, dn],
                "outcomes": ["Up", "Down"],
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "negRisk": False,
            }
        ],
    }


class _GammaSession:
    def __init__(self):
        self.headers = {}
        self.queue: list = []

    def get(self, url, params=None, timeout=None):
        if not self.queue:
            raise RuntimeError("gamma empty")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        resp = Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = item
        return resp


class KnownLastWindowStaleGammaTests(unittest.TestCase):
    def test_known_last_60s_stale_gamma_reaches_book_look(self):
        market = _market()
        self.assertTrue(market_is_known_for_buy(market))
        self.assertTrue(
            _reaches_book_look(
                discovery_fresh=False,
                seconds_left=40.0,
                market=market,
            )
        )

    def test_known_last_60s_at_boundary_reaches_book_look(self):
        self.assertTrue(
            _reaches_book_look(
                discovery_fresh=False,
                seconds_left=60.0,
                market=_market(),
            )
        )

    def test_fresh_gamma_still_reaches_book_look(self):
        self.assertTrue(
            _reaches_book_look(
                discovery_fresh=True,
                seconds_left=40.0,
                market=_market(),
            )
        )

    def test_15m_last_window_stale_gamma_reaches_book_look(self):
        minutes_left = 2.0
        buy_window_min = 3.0
        self.assertTrue(
            discovery_allows_buy_look(
                False,
                in_live_window=0 < minutes_left <= buy_window_min,
                market=_market(),
            )
        )


class UndiscoveredMarketCannotBuyTests(unittest.TestCase):
    def test_missing_tokens_stale_gamma_cannot_buy(self):
        incomplete = _market(up_token="", dn_token="")
        self.assertFalse(market_is_known_for_buy(incomplete))
        self.assertFalse(
            _reaches_book_look(
                discovery_fresh=False,
                seconds_left=40.0,
                market=incomplete,
            )
        )

    def test_missing_end_ts_stale_gamma_cannot_buy(self):
        incomplete = _market(end_ts=0.0)
        self.assertFalse(market_is_known_for_buy(incomplete))
        self.assertFalse(
            _reaches_book_look(
                discovery_fresh=False,
                seconds_left=40.0,
                market=incomplete,
            )
        )

    def test_empty_cache_has_no_book_look(self):
        cached: list[MintMarket] = []
        allowed = [
            m
            for m in cached
            if _reaches_book_look(
                discovery_fresh=False,
                seconds_left=40.0,
                market=m,
            )
        ]
        self.assertEqual(allowed, [])

    def test_none_market_cannot_buy(self):
        self.assertFalse(market_is_known_for_buy(None))
        self.assertFalse(
            discovery_allows_buy_look(
                False, in_live_window=True, market=None,
            )
        )


class StaleGammaOutsideLiveWindowStillBlocksTests(unittest.TestCase):
    def test_known_market_before_last_60s_still_blocks(self):
        self.assertFalse(
            _reaches_book_look(
                discovery_fresh=False,
                seconds_left=90.0,
                market=_market(),
            )
        )

    def test_expired_ttm_does_not_open_a_buy_look(self):
        self.assertFalse(
            _reaches_book_look(
                discovery_fresh=False,
                seconds_left=0.0,
                market=_market(),
            )
        )


class GammaRefreshStillUpdatesDirectoryTests(unittest.TestCase):
    def test_discover_replaces_cached_list_on_next_good_snapshot(self):
        session = _GammaSession()
        gateway = MarketGateway(
            gamma_url="https://gamma.test",
            data_api_url="https://data.test",
            session=session,
            discover_cache_s=0.0,
            stale_cache_s=120.0,
        )
        first_start = START_TS
        second_start = START_TS + 300.0
        session.queue = [
            [_gamma_event("0xaaa", first_start, "up-a", "dn-a")],
        ]
        first = gateway.discover(["btc-up-or-down-5m"])
        self.assertTrue(gateway.discovery_fresh)
        self.assertEqual([m.condition_id for m in first], ["0xaaa"])

        session.queue = [
            [_gamma_event("0xbbb", second_start, "up-b", "dn-b")],
        ]
        second = gateway.discover(["btc-up-or-down-5m"])
        self.assertTrue(gateway.discovery_fresh)
        self.assertEqual([m.condition_id for m in second], ["0xbbb"])
        self.assertEqual(first[0].up_token, "up-a")
        self.assertEqual(second[0].up_token, "up-b")

    def test_failed_refresh_keeps_last_good_list_and_marks_stale(self):
        session = _GammaSession()
        gateway = MarketGateway(
            gamma_url="https://gamma.test",
            data_api_url="https://data.test",
            session=session,
            discover_cache_s=0.0,
            stale_cache_s=120.0,
        )
        session.queue = [[_gamma_event("0xaaa", START_TS, "up-a", "dn-a")]]
        first = gateway.discover(["btc-up-or-down-5m"])
        self.assertTrue(gateway.discovery_fresh)

        session.queue = [RuntimeError("gamma timeout")]
        second = gateway.discover(["btc-up-or-down-5m"])
        self.assertFalse(gateway.discovery_fresh)
        self.assertEqual([m.condition_id for m in second], ["0xaaa"])
        self.assertTrue(
            discovery_allows_buy_look(
                False,
                in_live_window=True,
                market=second[0],
            )
        )


class BotWiringTests(unittest.TestCase):
    def test_buy_bots_use_discovery_allow_helper_not_bare_freshness_veto(self):
        for bot in (BOT5M, BOT, BOT_HR):
            src = bot.read_text()
            self.assertIn("discovery_allows_buy_look(", src, bot.name)
            buy_idx = src.index("# --- BUY CHECK")
            buy_chunk = src[buy_idx:buy_idx + 2500]
            self.assertIn("discovery_allows_buy_look(", buy_chunk, bot.name)
            self.assertNotIn(
                "if not _discovery_fresh:",
                buy_chunk,
                bot.name,
            )
        five_m = BOT5M.read_text()
        buy_5m = five_m[five_m.index("# --- BUY CHECK"):]
        self.assertIn("in_live_window=late_slice", buy_5m)
        fifteen = BOT.read_text()
        buy_15 = fifteen[fifteen.index("# --- BUY CHECK"):]
        self.assertIn("in_live_window=0 < minutes_left <= BUY_WINDOW_MIN", buy_15)
        hourly = BOT_HR.read_text()
        buy_hr = hourly[hourly.index("# --- BUY CHECK"):]
        self.assertIn("in_live_window=bool(bands)", buy_hr)

    def test_5m_stale_discovery_log_only_after_helper_rejects(self):
        src = BOT5M.read_text()
        buy_idx = src.index("# --- BUY CHECK")
        buy_chunk = src[buy_idx:buy_idx + 2500]
        helper_at = buy_chunk.index("discovery_allows_buy_look(")
        log_at = buy_chunk.index('"stale_discovery"')
        book_at = src.index("look_book_quote(", buy_idx)
        self.assertLess(helper_at, log_at)
        self.assertLess(buy_idx + log_at, book_at)

    def test_hedge_path_ignores_gamma_freshness(self):
        for bot in (BOT5M, BOT, BOT_HR):
            src = bot.read_text()
            hedge_idx = src.index("# --- HEDGE") if "# --- HEDGE" in src else src.index("held_size > 0.01")
            buy_idx = src.index("# --- BUY CHECK")
            hedge_chunk = src[hedge_idx:buy_idx]
            self.assertNotIn("_discovery_fresh", hedge_chunk, bot.name)
            self.assertNotIn("discovery_allows_buy_look", hedge_chunk, bot.name)

    def test_background_gamma_refresh_still_scheduled(self):
        for bot in (BOT5M, BOT, BOT_HR):
            src = bot.read_text()
            self.assertIn("def _discover_markets_snapshot():", src, bot.name)
            self.assertIn(
                "_markets_future = _io_executor.submit(_discover_markets_snapshot)",
                src,
                bot.name,
            )
            self.assertIn("if refreshed_markets:", src, bot.name)
            self.assertIn("_cached_markets = refreshed_markets", src, bot.name)

    def test_15m_entry_window_open_does_not_require_fresh_gamma_alone(self):
        src = BOT.read_text()
        self.assertIn("discovery_allows_buy_look(", src)
        window_idx = src.index("_entry_window_open = bool(")
        window_chunk = src[window_idx:window_idx + 500]
        self.assertIn("discovery_allows_buy_look(", window_chunk)
        self.assertNotIn("and _discovery_fresh", window_chunk)


class KnownFlagTests(unittest.TestCase):
    def test_duplicate_tokens_are_not_known(self):
        self.assertFalse(
            market_is_known_for_buy(_market(up_token="same", dn_token="same"))
        )

    def test_namespace_with_tokens_and_end_is_known(self):
        self.assertTrue(
            market_is_known_for_buy(
                SimpleNamespace(end_ts=END_TS, up_token="u", dn_token="d")
            )
        )


if __name__ == "__main__":
    unittest.main()
