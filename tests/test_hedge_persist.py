"""Unit tests for the 5m persist hedge gate (no network)."""

from __future__ import annotations

import unittest

from buy.hedge_gate import (
    hedge_persist_ready,
    hedge_winner_rest_fresh,
    hedge_winner_rest_rememberable,
)


class HedgePersistReadyTests(unittest.TestCase):
    def test_reset_when_book_fails(self):
        fire, armed, why = hedge_persist_ready(
            False, now_s=10.0, armed_ts=8.0, persist_s=2.0,
        )
        self.assertFalse(fire)
        self.assertIsNone(armed)
        self.assertEqual(why, "reset")

    def test_immediate_when_persist_off(self):
        fire, armed, why = hedge_persist_ready(
            True, now_s=10.0, armed_ts=None, persist_s=0.0,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "immediate")

    def test_toxic_skips_persist(self):
        fire, _armed, why = hedge_persist_ready(
            True, now_s=10.0, armed_ts=None, persist_s=2.0, toxic=True,
        )
        self.assertTrue(fire)
        self.assertEqual(why, "immediate")

    def test_arms_on_first_qualify(self):
        fire, armed, why = hedge_persist_ready(
            True, now_s=10.0, armed_ts=None, persist_s=2.0,
        )
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "armed")

    def test_waits_until_persist_elapsed(self):
        fire, armed, why = hedge_persist_ready(
            True, now_s=11.5, armed_ts=10.0, persist_s=2.0,
        )
        self.assertFalse(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "waiting")

    def test_fires_after_persist(self):
        fire, armed, why = hedge_persist_ready(
            True, now_s=12.0, armed_ts=10.0, persist_s=2.0,
        )
        self.assertTrue(fire)
        self.assertEqual(armed, 10.0)
        self.assertEqual(why, "ready")


class HedgeWinnerRestFreshTests(unittest.TestCase):
    def test_remember_only_comfortable_winners(self):
        self.assertTrue(
            hedge_winner_rest_rememberable(0.90, threshold=0.70, cushion=0.10),
        )
        self.assertFalse(
            hedge_winner_rest_rememberable(0.80, threshold=0.70, cushion=0.10),
        )
        self.assertFalse(
            hedge_winner_rest_rememberable(0.75, threshold=0.70, cushion=0.10),
        )
        self.assertFalse(
            hedge_winner_rest_rememberable(None, threshold=0.70, cushion=0.10),
        )

    def test_skip_rest_while_ttl_holds(self):
        self.assertTrue(
            hedge_winner_rest_fresh(
                0.92, 10.0, now_s=11.5, threshold=0.70, ttl_s=2.0,
            ),
        )
        self.assertFalse(
            hedge_winner_rest_fresh(
                0.92, 10.0, now_s=12.0, threshold=0.70, ttl_s=2.0,
            ),
        )

    def test_near_threshold_keeps_resting(self):
        self.assertFalse(
            hedge_winner_rest_fresh(
                0.75, 10.0, now_s=10.5, threshold=0.70, ttl_s=2.0,
            ),
        )

    def test_disabled_ttl_or_missing_snapshot(self):
        self.assertFalse(
            hedge_winner_rest_fresh(
                0.92, 10.0, now_s=10.5, threshold=0.70, ttl_s=0.0,
            ),
        )
        self.assertFalse(
            hedge_winner_rest_fresh(
                0.92, None, now_s=10.5, threshold=0.70, ttl_s=2.0,
            ),
        )

    def test_negative_age_is_fail_closed(self):
        self.assertFalse(
            hedge_winner_rest_fresh(
                0.92, 12.0, now_s=10.0, threshold=0.70, ttl_s=2.0,
            ),
        )


if __name__ == "__main__":
    unittest.main()
