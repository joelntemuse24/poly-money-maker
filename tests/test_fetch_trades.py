"""Unit tests for Data API trade-history fetch (no network)."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import requests

from check_fetch_trades import (
    OFFSET_CAP,
    OffsetCapError,
    fetch_all_trades,
    fetch_page,
    load_existing_keys,
    main,
    next_window_end,
    normalize_trade,
    parse_args,
    summarize_csv,
    trade_dedup_key,
)
from check_participation import Market, load_csv_buys, match_csv_to_markets


def _raw(
    *,
    ts: int,
    side: str = "BUY",
    size: float = 3.0,
    price: float = 0.80,
    asset: str = "token-up",
    tx: str = "0xabc",
    title: str = "Bitcoin Up or Down - August 19, 12:40AM-12:45AM ET",
    outcome: str = "Down",
    slug: str = "btc-updown-5m-1",
) -> dict:
    return {
        "proxyWallet": "0xuser",
        "side": side,
        "asset": asset,
        "conditionId": "0xcond",
        "size": size,
        "price": price,
        "timestamp": ts,
        "title": title,
        "slug": slug,
        "outcome": outcome,
        "outcomeIndex": 1,
        "transactionHash": tx,
        "name": "",
        "pseudonym": "",
        "bio": "",
        "icon": "",
        "eventSlug": "",
        "profileImage": "",
        "profileImageOptimized": "",
    }


class FakeResponse:
    def __init__(self, payload, status: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status
        if text is not None:
            self.text = text
        elif isinstance(payload, (dict, list)):
            self.text = json.dumps(payload)
        else:
            self.text = ""

    def json(self):
        if self.status_code >= 400 and not isinstance(self._payload, (dict, list)):
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code}")
            err.response = self
            raise err


class FakeSession:
    """Serves newest-first pages; 400s when offset exceeds cap."""

    def __init__(self, trades: list[dict], offset_cap: int = OFFSET_CAP):
        self.trades = list(trades)
        self.offset_cap = offset_cap
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        offset = int(params.get("offset") or 0)
        limit = int(params.get("limit") or 100)
        start = params.get("start")
        end = params.get("end")
        if offset > self.offset_cap:
            return FakeResponse(
                {"error": "max historical trades offset of 10000 exceeded"},
                status=400,
                text='{"error":"max historical trades offset of 10000 exceeded"}',
            )
        rows = list(self.trades)
        if start is not None:
            rows = [t for t in rows if t["timestamp"] >= int(start)]
        if end is not None:
            rows = [t for t in rows if t["timestamp"] <= int(end)]
        rows.sort(key=lambda t: t["timestamp"], reverse=True)
        page = rows[offset : offset + limit]
        return FakeResponse(page)


class NormalizeTests(unittest.TestCase):
    def test_maps_participation_columns(self):
        row = normalize_trade(_raw(ts=1_700_000_000, size=10.0, price=0.25))
        self.assertEqual(row["timestamp"], "1700000000")
        self.assertEqual(row["action"], "Buy")
        self.assertEqual(row["tokenName"], "Down")
        self.assertEqual(row["marketName"], row["title"])
        self.assertEqual(row["tokenAmount"], "10.0")
        self.assertEqual(float(row["usdcAmount"]), 2.5)
        self.assertEqual(row["hash"], "0xabc")
        self.assertTrue(row["timestampUtc"].startswith("2023-"))

    def test_sell_action(self):
        row = normalize_trade(_raw(ts=10, side="SELL"))
        self.assertEqual(row["action"], "Sell")
        self.assertEqual(row["side"], "SELL")

    def test_zero_notional_still_written(self):
        row = normalize_trade(_raw(ts=10, size=0.0, price=0.0))
        self.assertEqual(float(row["usdcAmount"]), 0.0)
        self.assertNotEqual(row["usdcAmount"], "")


class DedupeAndWindowTests(unittest.TestCase):
    def test_dedup_key_stable(self):
        a = normalize_trade(_raw(ts=50, size=1.5))
        b = normalize_trade(_raw(ts=50, size=1.5))
        c = normalize_trade(_raw(ts=50, size=1.6))
        self.assertEqual(trade_dedup_key(a), trade_dedup_key(b))
        self.assertNotEqual(trade_dedup_key(a), trade_dedup_key(c))

    def test_next_window_end_overlaps_then_steps(self):
        self.assertEqual(next_window_end(100, made_progress=True), 100)
        self.assertEqual(next_window_end(100, made_progress=False), 99)

    def test_next_window_end_rejects_nonpositive(self):
        with self.assertRaises(ValueError):
            next_window_end(0, made_progress=True)


class FetchPageTests(unittest.TestCase):
    def test_offset_cap_400_is_clean(self):
        session = FakeSession([], offset_cap=10)
        with self.assertRaises(OffsetCapError):
            fetch_page(
                session,
                user="0xabc",
                limit=1,
                offset=11,
                start=1,
                end=None,
                taker_only=None,
                side=None,
                market=None,
                timeout=5.0,
            )

    def test_other_400_still_raises(self):
        class Boom:
            def get(self, *args, **kwargs):
                return FakeResponse({"error": "bad user"}, status=400, text='{"error":"bad user"}')

        with self.assertRaises(requests.HTTPError):
            fetch_page(
                Boom(),
                user="0xnope",
                limit=1,
                offset=0,
                start=1,
                end=None,
                taker_only=None,
                side=None,
                market=None,
                timeout=5.0,
            )


class FetchLoopTests(unittest.TestCase):
    def test_time_windows_and_dedupe(self):
        # Newest-first tape. offset_cap=2 with limit=2 fills a window of 4
        # (offset 0 and 2), then the next window must walk end=oldest.
        trades = [
            _raw(ts=100 - i, size=1.0 + i * 0.01, tx=f"0x{i:02x}", asset="a")
            for i in range(7)
        ]
        session = FakeSession(trades, offset_cap=2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.csv"
            stats = fetch_all_trades(
                session,
                user="0xuser",
                out_path=path,
                limit=2,
                start=1,
                sleep_s=0,
                offset_cap=2,
                max_windows=20,
            )
            self.assertEqual(stats["unique_new"], 7)
            self.assertGreaterEqual(stats["windows"], 2)
            self.assertGreaterEqual(stats["offset_cap_hits"], 1)
            self.assertTrue(any(c["params"].get("end") for c in session.calls))
            self.assertTrue(all(int(c["params"]["start"]) == 1 for c in session.calls))

            # Re-run is idempotent.
            stats2 = fetch_all_trades(
                session,
                user="0xuser",
                out_path=path,
                limit=2,
                start=1,
                sleep_s=0,
                offset_cap=2,
                max_windows=20,
            )
            self.assertEqual(stats2["unique_new"], 0)
            self.assertEqual(stats2["unique_total"], 7)
            self.assertEqual(len(load_existing_keys(path)), 7)

            buys = load_csv_buys(str(path))
            self.assertEqual(len(buys), 7)
            self.assertEqual(buys[0]["market"], trades[0]["title"])
            self.assertAlmostEqual(buys[0]["usdc"], 1.0 * 0.80, places=5)

    def test_small_history_single_window(self):
        trades = [_raw(ts=50, tx="0x1"), _raw(ts=40, tx="0x2", side="SELL")]
        session = FakeSession(trades, offset_cap=100)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.csv"
            stats = fetch_all_trades(
                session,
                user="0xuser",
                out_path=path,
                limit=10,
                sleep_s=0,
                offset_cap=100,
            )
            self.assertEqual(stats["windows"], 1)
            self.assertEqual(stats["offset_cap_hits"], 0)
            summary = summarize_csv(path)
            self.assertEqual(summary["rows"], 2)
            self.assertEqual(summary["buys"], 1)
            self.assertEqual(summary["sells"], 1)
            self.assertEqual(summary["series"].get("5m"), 2)

    def test_taker_only_false_forwarded(self):
        session = FakeSession([_raw(ts=1, tx="0x1")], offset_cap=10)
        with tempfile.TemporaryDirectory() as tmp:
            fetch_all_trades(
                session,
                user="0xuser",
                out_path=Path(tmp) / "t.csv",
                limit=10,
                sleep_s=0,
                taker_only=False,
            )
        self.assertEqual(session.calls[0]["params"]["takerOnly"], "false")


class CliAndParticipationTests(unittest.TestCase):
    def test_parse_taker_only_false(self):
        args = parse_args(["--user", "0xabc", "--taker-only", "false"])
        self.assertIs(args.taker_only, False)

    def test_main_requires_user(self):
        rc = main(["--user", "", "--out", "/tmp/nope.csv"])
        self.assertEqual(rc, 2)

    def test_written_csv_loadable_by_participation(self):
        row = normalize_trade(_raw(ts=1_787_000_000, size=39.29, price=0.0624))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hist.csv"
            from check_fetch_trades import CSV_COLUMNS, csv_field, write_csv_header

            write_csv_header(path)
            with path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
                writer.writerow({k: csv_field(row, k) for k in CSV_COLUMNS})
            buys = load_csv_buys(str(path))
            self.assertEqual(len(buys), 1)
            self.assertEqual(buys[0]["leg"], "down")
            self.assertAlmostEqual(buys[0]["tokens"], 39.29)
            self.assertAlmostEqual(buys[0]["usdc"], 39.29 * 0.0624, places=5)
            self.assertEqual(buys[0]["ts"], 1_787_000_000.0)
            self.assertEqual(buys[0]["slug"], "btc-updown-5m-1")

    def test_csv_matches_5m_by_slug_when_title_is_generic(self):
        slug = "btc-updown-5m-1787851800"
        buys = [
            {
                "ts": 1787852000.0,
                "market": "BTC Up or Down 5m",
                "slug": slug,
                "leg": "up",
                "usdc": 2.50,
                "tokens": 3.0,
                "avg": 0.833,
                "norm": "btc up or down 5m",
            }
        ]
        markets = [
            Market(
                condition_id="cid-1",
                slug=slug,
                question="Bitcoin Up or Down - August 27, 5:30PM-5:35PM ET",
                start_ts=1787851800.0,
                end_ts=1787852100.0,
                up_token="u",
                dn_token="d",
                bot="5m",
            )
        ]
        hits = match_csv_to_markets(buys, markets)
        self.assertEqual(set(hits), {"cid-1"})
        self.assertIn("csv", hits["cid-1"].sources)


if __name__ == "__main__":
    unittest.main()
