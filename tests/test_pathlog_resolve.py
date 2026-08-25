"""Unit tests for cheap pathlog resolve selection (no Gamma network)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pathlog
from buy.market import MintMarket


NOW = 1_000_000.0


def _write_stub(
    tick_dir: Path,
    slug: str,
    end_ts: float,
    *,
    resolved: bool = False,
    ticks: int = 1,
) -> Path:
    path = tick_dir / f"{slug}.jsonl"
    rows = [
        {
            "e": "open",
            "slug": slug,
            "end": end_ts,
            "series": "btc-up-or-down-5m",
        }
    ]
    for i in range(ticks):
        rows.append({"e": "tick", "ts": end_ts - 10 + i, "ua": 0.80, "da": 0.20})
    if resolved:
        rows.append(
            {"e": "resolved", "ts": end_ts + 30, "winner": "up", "src": "gamma"}
        )
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    )
    return path


def _market(end_ts: float, start_ts: float) -> MintMarket:
    return MintMarket(
        condition_id="0x1",
        slug="btc-updown-5m-live",
        question="q",
        end_ts=end_ts,
        series_slug="btc-up-or-down-5m",
        up_token="up",
        dn_token="dn",
        active=True,
        closed=False,
        accepting_orders=True,
        neg_risk=False,
        start_ts=start_ts,
    )


class PlanResolvesTests(unittest.TestCase):
    def setUp(self):
        pathlog.reset_resolve_memory()
        self.tmp = tempfile.TemporaryDirectory()
        self.tick_dir = Path(self.tmp.name)
        self._tick_patch = patch.object(pathlog, "TICK_DIR", self.tick_dir)
        self._tick_patch.start()

    def tearDown(self):
        self._tick_patch.stop()
        pathlog.reset_resolve_memory()
        self.tmp.cleanup()

    def test_prefers_newest_ended_markets(self):
        _write_stub(self.tick_dir, "old-due", NOW - 200)
        _write_stub(self.tick_dir, "mid-due", NOW - 90)
        _write_stub(self.tick_dir, "new-due", NOW - 40)
        plan = pathlog.plan_resolves(NOW, max_per_cycle=2)
        slugs = [item["slug"] for item in plan["due"]]
        self.assertEqual(slugs, ["new-due", "mid-due"])
        self.assertEqual(plan["pending"], 3)
        self.assertEqual(plan["resolve_capped"], 1)

    def test_skips_files_still_inside_grace(self):
        _write_stub(self.tick_dir, "in-grace", NOW - 10)
        _write_stub(self.tick_dir, "past-grace", NOW - 60)
        plan = pathlog.plan_resolves(NOW, grace_s=20)
        slugs = [item["slug"] for item in plan["due"]]
        self.assertEqual(slugs, ["past-grace"])
        self.assertEqual(plan["pending"], 2)
        self.assertNotIn("in-grace", pathlog._gave_up_slugs)
        self.assertNotIn("in-grace", pathlog._resolved_slugs)

    def test_give_up_after_max_age_without_opening_gamma(self):
        _write_stub(self.tick_dir, "ancient", NOW - 10_000)
        _write_stub(self.tick_dir, "fresh-due", NOW - 60)
        plan = pathlog.plan_resolves(NOW, max_age_s=2 * 3600)
        slugs = [item["slug"] for item in plan["due"]]
        self.assertEqual(slugs, ["fresh-due"])
        self.assertIn("ancient", pathlog._gave_up_slugs)
        self.assertEqual(plan["resolve_skipped_old"], 1)
        self.assertEqual(plan["pending"], 1)

    def test_caps_due_list_at_max_per_cycle(self):
        for i in range(10):
            _write_stub(self.tick_dir, f"due-{i:02d}", NOW - 30 - i)
        plan = pathlog.plan_resolves(NOW, max_per_cycle=4)
        self.assertEqual(len(plan["due"]), 4)
        self.assertEqual(plan["pending"], 10)
        self.assertEqual(plan["resolve_capped"], 6)
        self.assertEqual(plan["due"][0]["slug"], "due-00")

    def test_resolved_and_give_up_are_not_reopened(self):
        _write_stub(self.tick_dir, "done", NOW - 80, resolved=True)
        _write_stub(self.tick_dir, "too-old", NOW - 20_000)
        first = pathlog.plan_resolves(NOW)
        self.assertEqual(first["opened"], 2)
        self.assertIn("done", pathlog._resolved_slugs)
        self.assertIn("too-old", pathlog._gave_up_slugs)
        with patch.object(
            pathlog, "tick_file_bookends", wraps=pathlog.tick_file_bookends
        ) as reader:
            second = pathlog.plan_resolves(NOW)
        self.assertEqual(second["opened"], 0)
        self.assertEqual(reader.call_count, 0)
        self.assertEqual(second["due"], [])

    def test_does_not_full_scan_via_file_has_event(self):
        _write_stub(self.tick_dir, "due-a", NOW - 50)
        with patch.object(
            pathlog, "file_has_event", wraps=pathlog.file_has_event
        ) as scanner:
            pathlog.plan_resolves(NOW)
        scanner.assert_not_called()

    def test_vm_scale_old_stubs_are_given_up_once(self):
        """Live VM had ~2313 pending stubs; those must not be re-opened or Gamma'd."""
        n = 2313
        for i in range(n):
            _write_stub(self.tick_dir, f"stub-{i:04d}", NOW - 20_000)
        _write_stub(self.tick_dir, "just-closed", NOW - 40)
        first = pathlog.plan_resolves(NOW)
        self.assertEqual(first["resolve_skipped_old"], n)
        self.assertEqual([item["slug"] for item in first["due"]], ["just-closed"])
        self.assertEqual(first["opened"], n + 1)
        self.assertEqual(first["resolve_capped"], 0)
        second = pathlog.plan_resolves(NOW)
        self.assertEqual(second["opened"], 1)
        self.assertEqual(second["resolve_skipped_old"], 0)
        self.assertEqual([item["slug"] for item in second["due"]], ["just-closed"])


class ApplyResolvesAndCycleTests(unittest.TestCase):
    def setUp(self):
        pathlog.reset_resolve_memory()
        self.tmp = tempfile.TemporaryDirectory()
        self.tick_dir = Path(self.tmp.name)
        self.heartbeat = self.tick_dir / "heartbeat"
        self.stop = self.tick_dir / "no-stop"
        self._patches = [
            patch.object(pathlog, "TICK_DIR", self.tick_dir),
            patch.object(pathlog, "HEARTBEAT_FILE", self.heartbeat),
            patch.object(pathlog, "STOP_FILE", self.stop),
            patch("pathlog.time.time", return_value=NOW),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in self._patches:
            item.stop()
        pathlog.reset_resolve_memory()
        self.tmp.cleanup()

    def _cycle(self, *, gamma, markets=None):
        gateway = MagicMock()
        gateway.discover.return_value = list(markets or [])
        session = MagicMock()
        with patch.object(pathlog, "gamma_winner", side_effect=gamma) as mocked:
            status = pathlog.run_cycle(gateway, session)
        return status, mocked

    def test_run_cycle_skips_gamma_for_old_stubs(self):
        _write_stub(self.tick_dir, "ancient", NOW - 10_000)
        status, mocked = self._cycle(gamma=lambda *_: "up")
        self.assertEqual(status, "ok")
        mocked.assert_not_called()
        self.assertIn("ancient", pathlog._gave_up_slugs)
        payload = json.loads(self.heartbeat.read_text())
        self.assertEqual(payload["resolve_skipped_old"], 1)
        self.assertEqual(payload["resolved"], 0)
        self.assertEqual(payload["pending"], 0)

    def test_run_cycle_gamma_cap_newest_first(self):
        for i in range(9):
            _write_stub(self.tick_dir, f"m{i}", NOW - 30 - i * 10)
        calls = []

        def gamma(_session, slug):
            calls.append(slug)
            return None

        status, mocked = self._cycle(gamma=gamma)
        self.assertEqual(status, "ok")
        self.assertEqual(mocked.call_count, pathlog.RESOLVE_MAX_PER_CYCLE)
        self.assertEqual(calls, ["m0", "m1", "m2", "m3"])
        payload = json.loads(self.heartbeat.read_text())
        self.assertEqual(payload["resolve_capped"], 5)
        self.assertEqual(payload["pending"], 9)
        self.assertEqual(payload["resolved"], 0)

    def test_run_cycle_skips_grace_and_does_not_call_gamma(self):
        _write_stub(self.tick_dir, "in-grace", NOW - 5)
        status, mocked = self._cycle(gamma=lambda *_: "down")
        self.assertEqual(status, "ok")
        mocked.assert_not_called()
        payload = json.loads(self.heartbeat.read_text())
        self.assertEqual(payload["pending"], 1)
        self.assertEqual(payload["resolved"], 0)

    def test_sampling_runs_before_resolve(self):
        _write_stub(self.tick_dir, "just-ended", NOW - 40)
        order = []

        def sample(market, now):
            order.append("sample")
            return None

        def gamma(_session, slug):
            order.append("gamma")
            return None

        gateway = MagicMock()
        gateway.discover.return_value = [_market(NOW + 60, NOW - 10)]
        session = MagicMock()
        with patch.object(pathlog, "sample_market", side_effect=sample), patch.object(
            pathlog, "gamma_winner", side_effect=gamma
        ):
            pathlog.run_cycle(gateway, session)
        self.assertEqual(order, ["sample", "gamma"])

    def test_apply_resolves_stamps_winner_and_caches(self):
        path = _write_stub(self.tick_dir, "fill-me", NOW - 40)
        plan = pathlog.plan_resolves(NOW)
        self.assertEqual(len(plan["due"]), 1)
        with patch.object(pathlog, "gamma_winner", return_value="up") as gamma:
            stamped = pathlog.apply_resolves(
                MagicMock(), plan["due"], NOW, resolved=pathlog._resolved_slugs
            )
        self.assertEqual(stamped, 1)
        self.assertEqual(gamma.call_count, 1)
        self.assertIn("fill-me", pathlog._resolved_slugs)
        lines = path.read_text().strip().splitlines()
        last = json.loads(lines[-1])
        self.assertEqual(last["e"], "resolved")
        self.assertEqual(last["winner"], "up")
        with patch.object(pathlog, "gamma_winner") as gamma_again:
            again = pathlog.plan_resolves(NOW)
            pathlog.apply_resolves(MagicMock(), again["due"], NOW)
        gamma_again.assert_not_called()
        self.assertEqual(again["opened"], 0)


class ClassifyResolveStatusTests(unittest.TestCase):
    def test_due_between_grace_and_max_age(self):
        first = {"e": "open", "end": NOW - 60}
        self.assertEqual(
            pathlog.classify_resolve_status(first, {"e": "tick"}, NOW),
            "due",
        )

    def test_grace_window(self):
        first = {"e": "open", "end": NOW - 10}
        self.assertEqual(
            pathlog.classify_resolve_status(first, {"e": "tick"}, NOW, grace_s=20),
            "grace",
        )

    def test_older_than_max_age(self):
        first = {"e": "open", "end": NOW - 10_000}
        self.assertEqual(
            pathlog.classify_resolve_status(
                first, {"e": "tick"}, NOW, max_age_s=7200
            ),
            "give_up",
        )


if __name__ == "__main__":
    unittest.main()
