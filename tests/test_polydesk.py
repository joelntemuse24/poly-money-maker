"""Unit tests for the Polydesk glance helpers (no window, no network)."""

from __future__ import annotations

import unittest

from widget.polydesk import (
    format_age,
    format_usd,
    looks_like_address,
    parse_value,
    summarize_positions,
)


class AddressTests(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(
            looks_like_address("0x56687bf447db6ffa42ffe2204a05edaa20f55839")
        )

    def test_rejects_short_or_empty(self):
        self.assertFalse(looks_like_address(""))
        self.assertFalse(looks_like_address("0x1234"))
        self.assertFalse(looks_like_address("56687bf447db6ffa42ffe2204a05edaa20f55839"))
        self.assertFalse(looks_like_address("0xYourProxyWallet"))
        self.assertFalse(looks_like_address("0x0000000000000000000000000000000000000000"))


class ParseValueTests(unittest.TestCase):
    def test_array_payload(self):
        self.assertEqual(
            parse_value([{"user": "0xabc", "value": 12.5}]),
            12.5,
        )

    def test_object_payload(self):
        self.assertEqual(parse_value({"value": 0}), 0.0)

    def test_bad(self):
        self.assertIsNone(parse_value([]))
        self.assertIsNone(parse_value({"value": "nope"}))


class PositionsTests(unittest.TestCase):
    def test_flat_when_dust(self):
        holding, count, labels = summarize_positions(
            [{"size": 0.001, "title": "BTC", "outcome": "Up", "conditionId": "a"}]
        )
        self.assertFalse(holding)
        self.assertEqual(count, 0)
        self.assertEqual(labels, ())

    def test_holding_dedupes_market(self):
        rows = [
            {
                "size": 2.5,
                "title": "Bitcoin Up or Down",
                "outcome": "Down",
                "conditionId": "0x1",
            },
            {
                "size": 1.0,
                "title": "Bitcoin Up or Down",
                "outcome": "Up",
                "conditionId": "0x1",
            },
            {
                "size": 3.0,
                "title": "Ethereum Up or Down",
                "outcome": "Up",
                "conditionId": "0x2",
            },
        ]
        holding, count, labels = summarize_positions(rows)
        self.assertTrue(holding)
        self.assertEqual(count, 3)
        self.assertEqual(len(labels), 2)
        self.assertTrue(labels[0].startswith("Down"))


class FormatTests(unittest.TestCase):
    def test_usd(self):
        self.assertEqual(format_usd(None), "—")
        self.assertEqual(format_usd(2.5), "$2.50")
        self.assertEqual(format_usd(1250), "$1,250")

    def test_age(self):
        self.assertEqual(format_age(100, now=102), "live")
        self.assertEqual(format_age(100, now=120), "20s ago")


if __name__ == "__main__":
    unittest.main()
