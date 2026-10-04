"""Probability model: elapsed TWAP, variance, and calibration edges."""

from __future__ import annotations

import math
import unittest

from buy.lock_fair import (
    expected_close,
    fair_up,
    normal_cdf,
    resample_1s,
    side_z,
    sigma_1s,
    sigma_from_prices,
    signed_move,
    taker_fee,
)


class FairTests(unittest.TestCase):
    def test_normal_cdf_matches_known_values(self):
        self.assertAlmostEqual(normal_cdf(0.0), 0.5, places=9)
        self.assertAlmostEqual(normal_cdf(1.0), 0.841344746, places=6)
        self.assertAlmostEqual(normal_cdf(-1.0), 1.0 - 0.841344746, places=6)

    def test_taker_fee_is_the_backtest_curve(self):
        self.assertAlmostEqual(taker_fee(0.8), 0.07 * 0.8 * 0.2, places=9)
        self.assertEqual(taker_fee(0.0), 0.0)
        self.assertEqual(taker_fee(1.0), 0.0)

    def test_elapsed_twap_matches_q1e_discrete_path(self):
        # tau=30 → 31 known seconds and 29 copies of the last print.
        prices = [100.0 + i * 0.5 for i in range(31)]
        expected, scale = expected_close(prices, 30)
        path = prices + [prices[-1]] * 29
        self.assertEqual(len(path), 60)
        self.assertAlmostEqual(expected, sum(path) / 60.0, places=9)
        self.assertAlmostEqual(scale, (30 ** 3) / 10800.0, places=12)
        self.assertAlmostEqual(scale, 2.5, places=9)

    def test_constant_path_stays_constant(self):
        expected, _scale = expected_close([110.0] * 31, 30)
        self.assertAlmostEqual(expected, 110.0, places=9)

    def test_before_the_lock_uses_the_live_price(self):
        expected, scale = expected_close([50.0, 51.0], 100)
        self.assertEqual(expected, 51.0)
        self.assertEqual(scale, 60.0)

    def test_locked_above_strike_is_near_certain(self):
        fair = fair_up(
            strike=100.0,
            elapsed_prices=[110.0] * 50,
            tau_s=10.0,
            sigma=0.05,
            noise_frac=0.0,
        )
        self.assertGreater(fair["p_up"], 0.999)
        self.assertAlmostEqual(fair["expected"], 110.0, places=6)

    def test_at_the_strike_is_a_coin_flip(self):
        fair = fair_up(
            strike=100.0,
            elapsed_prices=[100.0] * 31,
            tau_s=30.0,
            sigma=1.0,
            noise_frac=0.0,
        )
        self.assertAlmostEqual(fair["p_up"], 0.5, places=6)

    def test_sigma_uses_the_wider_of_the_two_windows(self):
        # 400 one-second steps of +1 then a quiet tail would still see the
        # long window. Here the short window is flat and the long one is not.
        quiet = [100.0] * 30
        jump = [100.0 + i for i in range(30)]
        sigma, n = sigma_from_prices(jump + quiet, short_n=10, long_n=80)
        self.assertGreater(n, 10)
        self.assertGreater(sigma, 0.1)
        flat, _n = sigma_from_prices([5.0] * 50, short_n=10, long_n=20)
        self.assertAlmostEqual(flat, 1e-9, places=12)

    def test_side_z_uses_the_last_minute_variance(self):
        scored = side_z(strike=100.0, expected=100.0, sigma=1.0, tau_s=30.0, noise_frac=0.0)
        self.assertEqual(scored["side"], "up")
        self.assertAlmostEqual(scored["z_side"], 0.0, places=9)
        self.assertAlmostEqual(scored["variance"], (30.0 ** 3) / 10800.0, places=12)
        favourite = side_z(strike=100.0, expected=90.0, sigma=1.0, tau_s=30.0, noise_frac=0.0)
        self.assertEqual(favourite["side"], "down")
        self.assertGreater(favourite["z_side"], 0.0)
        self.assertAlmostEqual(favourite["z_side"], -favourite["z"], places=9)
        tiny = side_z(strike=100.0, expected=100.0, sigma=1.0, tau_s=0.2, noise_frac=0.0)
        self.assertAlmostEqual(tiny["variance"], (0.5 ** 3) / 10800.0, places=12)

    def test_sigma_1s_is_the_population_std_with_a_price_floor(self):
        sigma, n = sigma_1s([100.0, 101.0, 99.0, 100.0])
        self.assertEqual(n, 3)
        self.assertGreater(sigma, 1e-6 * 100.0)
        move = signed_move(102.0, 100.0, sigma=1.0)
        self.assertAlmostEqual(move, 2.0, places=9)

    def test_resample_keeps_one_price_per_second(self):
        samples = [(1000.2, 10.0), (1001.4, 12.0), (1001.8, 13.0), (1003.0, 14.0)]
        self.assertEqual(resample_1s(samples), [10.0, 13.0, 13.0, 14.0])


if __name__ == "__main__":
    unittest.main()
