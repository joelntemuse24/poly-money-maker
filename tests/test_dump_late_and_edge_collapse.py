"""Late dump persist + edge-collapse allow-sell contract."""
import unittest


def dump_persist_for_ttm(minutes_left, *, normal=8.0, late_ttm_s=15.0, late_persist=2.0):
    ttm_s = float(minutes_left) * 60.0
    if late_ttm_s > 0 and ttm_s <= late_ttm_s + 1e-12:
        return float(late_persist)
    return float(normal)


def edge_collapse_allows(peak, now_favor, *, peak_need=40.0, now_max=10.0, why="oracle_still_winning_small"):
    if peak_need <= 0 or now_max <= 0:
        return False
    if why not in ("oracle_still_winning_small", "oracle_still_winning"):
        return False
    return peak + 1e-12 >= peak_need and now_favor is not None and 0 < now_favor <= now_max + 1e-12


class DumpLateAndEdgeCollapseTests(unittest.TestCase):
    def test_late_dump_persist_shortens(self):
        self.assertEqual(dump_persist_for_ttm(1.0), 8.0)
        self.assertEqual(dump_persist_for_ttm(0.25), 2.0)  # 15s
        self.assertEqual(dump_persist_for_ttm(0.1), 2.0)
        self.assertEqual(dump_persist_for_ttm(0.0), 2.0)

    def test_edge_collapse_10pm_shape(self):
        self.assertTrue(edge_collapse_allows(42.4, 2.38))
        self.assertFalse(edge_collapse_allows(42.4, 28.0))  # still large
        self.assertFalse(edge_collapse_allows(20.0, 2.0))  # peak too small
        self.assertFalse(edge_collapse_allows(42.4, 0.0))
