"""Held dump limits add depth while preserving observed bids and sizing."""

from __future__ import annotations

import ast
import json
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from buy.mint_sell import dump_deep_limit
from test_mint_cpu import _dump_cfg, _dump_harness, _fill_fak, _held_after_scrap


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"


def _functions(*names, **extras):
    tree = ast.parse(MINT.read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = {"__name__": __name__, **extras}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(MINT), "exec"), ns)
    return ns


class DeepLimitTests(unittest.TestCase):
    def test_helper_cases(self):
        for bid, expected in ((0.55, 0.35), (0.35, 0.15), (0.30, 0.10),
                              (0.24, 0.05), (0.173, 0.05), (0.06, 0.05),
                              (0.04, 0.04), (0.453, 0.25), (0, 0), (-0.1, 0)):
            with self.subTest(bid=bid):
                self.assertEqual(dump_deep_limit(bid), expected)
        self.assertEqual(dump_deep_limit(0.453, offset=0), 0.453)
        self.assertEqual(dump_deep_limit(0.453, offset=-1), 0.453)
        self.assertEqual(dump_deep_limit(0.55, offset=0.10, floor=0.20), 0.45)
        self.assertEqual(dump_deep_limit(0.30, floor=0.17), 0.17)
        self.assertEqual(dump_deep_limit(0.30, clob_min=0.115), 0.12)
        self.assertEqual(dump_deep_limit(0.04, clob_min=0.10), 0.04)

    def test_initial_and_every_refire_preserve_seen_bid(self):
        for offset, expected in ((0.20, [0.35, 0.10, 0.05]), (0, [0.55, 0.30, 0.173]),
                                 (None, [0.55, 0.30, 0.173])):
            with self.subTest(offset=offset):
                posts, events = [], []
                books = iter([(0.30, 100, []), (0.173, 100, [])])

                def fak(token, size, price, dry, capture=None, decision_at=None):
                    posts.append((size, price))
                    return (size if len(posts) == 3 else 1.0), "matched"

                ns = _functions(
                    "_run_dump_fak_with_refire", "_run_fak_ladder", "_sell_fak_with_fallback",
                    _fak_sell=fak,
                    _fetch_book=lambda *_a: next(books),
                    _io_unlocked=nullcontext,
                    _log_sell_book_depth=lambda **_kw: None,
                    log_event=lambda event, **kw: events.append((event, kw)),
                    console=SimpleNamespace(print=lambda *_a: None),
                    sell_fill_vwap=lambda *_a: None,
                    is_balance_allowance_reject=lambda _status: False,
                    dump_fast_retry_eligible=lambda **_kw: True,
                    time=SimpleNamespace(time=lambda: 1000),
                )
                kwargs = {} if offset is None else {"limit_offset": offset, "limit_floor": 0.05}
                result = ns["_run_dump_fak_with_refire"](
                    token_id="up", size=10, initial_bid=0.55, initial_bids=[], held="up",
                    slug="slug", condition_id="cid", ttm_s=120, floor=0.01,
                    min_bid_size=1, retries=2, ladder_step=0.04, ladder_rungs=4,
                    dry_run=False, tol=0.01, **kwargs,
                )
                self.assertEqual([price for _size, price in posts], expected)
                self.assertEqual([size for size, _price in posts], [10, 9, 8])
                self.assertEqual(result[-1], 0.173)
                first = next(kw for event, kw in events if event == "sell_dump_limit")
                self.assertEqual((first["bid"], first["limit"]), (0.55, expected[0]))
                refires = [kw for event, kw in events if event == "sell_dump_fast_refire_attempt"]
                self.assertEqual([row["bid"] for row in refires], [0.30, 0.173])
                self.assertEqual([row["limit"] for row in refires], expected[1:])
                self.assertEqual([row["limits"] for row in refires], [[px] for px in expected[1:]])

    def test_default_config_posts_deep_limit_and_logs_first_and_last_limits(self):
        ns, events, fak_calls, clock = _dump_harness(_fill_fak)
        cfg = _dump_cfg()
        cfg.pop("sell_dump_limit_offset")
        self.assertNotIn("sell_dump_limit_offset", cfg)
        intent = _held_after_scrap(
            clock["now"] + 200.0, sell_dump_armed_at=clock["now"] - 2.0,
        )
        state = {"intents": {"cid-deep": intent}}
        ns["remember_persisted_state"](state)
        ns["_manage_sells_locked"](cfg, state, object())
        self.assertEqual(fak_calls[0]["token_id"], "up-tok")
        self.assertEqual(fak_calls[0]["price"], 0.31)
        self.assertTrue(intent["sold_dump"])
        done = next(row for row in events if row["event"] == "sell_dump_done")
        self.assertEqual(done["bid"], 0.51)
        self.assertEqual(done["limit"], 0.31)
        self.assertEqual(done["first_limit"], 0.31)

    def test_only_held_dump_passes_settings_and_parsing_defaults(self):
        tree = ast.parse(MINT.read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_run_dump_fak_with_refire"]
        configured = []
        for call in calls:
            keywords = {kw.arg: kw.value for kw in call.keywords}
            if "limit_offset" in keywords or "limit_floor" in keywords:
                configured.append(call)
                self.assertEqual(ast.unparse(keywords["fills"]), "dump_fills")
                self.assertEqual(ast.unparse(keywords["limit_offset"]), "dump_limit_offset")
                self.assertEqual(ast.unparse(keywords["limit_floor"]), "dump_limit_floor")
        self.assertEqual(len(configured), 1)
        manage = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                      and n.name == "_manage_sells_locked")
        blocks = [n for n in ast.walk(manage) if isinstance(n, ast.Try)
                  and isinstance(n.body[0], ast.Assign)
                  and isinstance(n.body[0].targets[0], ast.Name)
                  and n.body[0].targets[0].id in ("dump_limit_offset", "dump_limit_floor")]
        self.assertEqual(len(blocks), 2)
        for cfg, expected in (({}, (0.20, 0.05)),
                              ({"sell_dump_limit_offset": None, "sell_dump_limit_floor": "bad"}, (0.20, 0.05)),
                              ({"sell_dump_limit_offset": 0, "sell_dump_limit_floor": "0.08"}, (0, 0.08)),
                              ({"sell_dump_limit_offset": 0.12, "sell_dump_limit_floor": 0.07}, (0.12, 0.07))):
            ns = {"cfg": cfg, "isfinite": __import__("math").isfinite}
            exec(compile(ast.Module(body=blocks, type_ignores=[]), str(MINT), "exec"), ns)
            self.assertEqual((ns["dump_limit_offset"], ns["dump_limit_floor"]), expected)
        example = json.loads((ROOT / "strategy_mint.example.json").read_text())
        self.assertEqual(example["sell_dump_limit_offset"], 0.20)
        self.assertEqual(example["sell_dump_limit_floor"], 0.05)
