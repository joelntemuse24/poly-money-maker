"""Side98 uses isolated order callbacks and never imports the mintbot runtime."""
import ast
import copy
import math
from types import SimpleNamespace

import pytest

import test_fivemin as helpers
from buy.fivemin import DEFAULTS, Runner, Strategy, dry_fill, local_day


@pytest.fixture
def bot():
    case = helpers.StrategyTests()
    case.setUp()
    case.row = case.strategy.state["markets"]["cid"]
    case.now = 1250.0
    case.price = 115.0
    case.past = 115.0
    case.latest = case.now * 1000
    case.grid = SimpleNamespace(
        price_at=lambda g: case.past if g == math.floor(case.now * 10) - 305 else case.price,
        latest_exchange_ms=lambda: case.latest,
    )
    case.books = {"cid": {"up": {"bid": .89, "asks": [[.98, 100]]},
                          "down": {"bid": .11, "asks": [[.12, 100]]}}}
    case.strategy.sample_cash(case.now, 1000)
    return case


def tick(bot, **extra):
    bot.strategy.tick(helpers.cfg(fivemin_side98_enabled=True, **extra),
                      bot.now, bot.grid, bot.books)


def test_defaults_disabled_and_mintbot_defaults_match(bot):
    expected = dict(fivemin_side98_enabled=False, fivemin_side98_shares=20,
                    fivemin_side98_price=.98, fivemin_side98_min_gap_usd=15.0,
                    fivemin_side98_max_ttc_s=60, fivemin_side98_max_adverse_30s_usd=5.0)
    tree = ast.parse((helpers.ROOT / "mintbot.py").read_text())
    node = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "DEFAULTS" for t in n.targets))
    defaults = {ast.literal_eval(k): v for k, v in zip(node.keys, node.values)}
    for key, value in expected.items():
        assert DEFAULTS[key] == value
        assert ast.literal_eval(defaults[key]) == value
    bot.strategy.tick(helpers.cfg(), bot.now, bot.grid, bot.books)
    assert not bot.buys
    assert not bot.row.get("side98")


def test_fill_outside_main_prefilter_one_attempt_and_notification(bot):
    tick(bot)
    tick(bot)
    assert bot.buys == [("up", 20.0, .98, False)]
    assert not bot.row["entered"]
    assert bot.row["position"] is None
    assert bot.row["side98"]["position"]["kind"] == "side98"
    assert bot.strategy.spends == [{"ts": bot.now, "cost": 19.6}]
    assert bot.alerts[-1].startswith("5m 98 SIDE ORDER")
    fields = next(kw for event, kw in bot.logs if event == "fivemin_side98_fill")
    assert fields["gap"] == 15
    assert fields["mom30"] == 0
    assert fields["ttm"] == 50
    assert "timing" in fields


@pytest.mark.parametrize("change", ["ask99", "ask97", "gap", "ttc", "adverse", "stale", "missing_latest", "missing_spot", "missing_past", "missing_offset", "missing_strike", "missing_asks", "missing_bid"])
def test_each_threshold_blocks(bot, change):
    if change.startswith("ask"):
        bot.books["cid"]["up"]["asks"] = [[.99 if change == "ask99" else .97, 100]]
    elif change == "gap":
        bot.price = 114.99
    elif change == "ttc":
        bot.now = 1239.9
    elif change == "adverse":
        bot.past = 120.01
    elif change == "stale":
        bot.latest = (bot.now - 3.01) * 1000
    elif change == "missing_latest":
        bot.latest = None
    elif change == "missing_spot":
        bot.grid.price_at = lambda g: None if g == math.floor(bot.now * 10) - 5 else 115
    elif change == "missing_past":
        bot.past = None
    elif change == "missing_offset":
        bot.row["offset"] = None
        bot.row["start_twap"] = None
        bot.grid.price_at = lambda g: None
    elif change == "missing_strike":
        bot.row["strike"] = None
    elif change == "missing_asks":
        bot.books["cid"]["up"]["asks"] = []
    elif change == "missing_bid":
        bot.books["cid"] = {}
    tick(bot)
    assert not bot.buys
    assert not bot.row.get("side98")


def test_inclusive_thresholds_and_offset_spot_down_direction(bot):
    bot.now = 1240
    bot.strategy.sample_cash(bot.now, 1000)
    bot.latest = (bot.now - 3) * 1000
    bot.row["offset"] = 10
    bot.price, bot.past = 75, 70
    bot.books["cid"] = {"down": {"bid": .89, "asks": [[.98, 100]]}, "up": {"bid": .11}}
    tick(bot)
    assert bot.buys == [("dn", 20.0, .98, False)]
    assert bot.row["side98"]["gap"] == 15
    assert bot.row["side98"]["mom30"] == -5


def test_side98_independent_main_time_window(bot):
    tick(bot, fivemin_max_ttm_s=20)
    assert len(bot.buys) == 1


@pytest.mark.parametrize("blocked", [False, True])
def test_main_signal_has_priority_even_when_main_order_blocked(bot, blocked):
    bot.price = 200
    bot.books["cid"]["up"]["bid"] = .97
    bot.books["cid"]["down"]["bid"] = .02
    bot.strategy.runs["cid"] = {"down": 1240}
    tick(bot, fivemin_enabled=not blocked)
    assert bot.row["entered"]
    assert not bot.row.get("side98")
    assert len(bot.buys) == (0 if blocked else 1)
    if not blocked:
        assert bot.buys[0][2] == .99


def test_side98_when_main_gate_below_threshold_then_main_later(bot):
    bot.books["cid"]["up"]["bid"] = .97
    bot.books["cid"]["down"]["bid"] = .02
    bot.strategy.runs["cid"] = {"down": 1240}
    tick(bot)
    side = bot.row["side98"]["position"]
    bot.now += 1
    bot.price = 200
    tick(bot)
    assert [order[2] for order in bot.buys] == [.98, .99]
    assert bot.row["side98"]["position"] is side
    assert bot.row["position"]["shares"] == 10


@pytest.mark.parametrize("outcome", ["nofill", "error", "disabled", "daily_stop", "cash", "expired"])
def test_attempt_consumed_for_every_order_outcome(bot, outcome):
    options = {}
    if outcome == "nofill":
        bot.io.buy = lambda *a: bot.buys.append(a) or {"shares": 0, "cost": 0, "timing": {"test": 1}}
    elif outcome == "error":
        def fail(*args):
            bot.buys.append(args)
            raise RuntimeError("exchange unavailable")
        bot.io.buy = fail
    elif outcome == "disabled":
        options["fivemin_enabled"] = False
    elif outcome == "daily_stop":
        bot.strategy.roll_day(bot.now)
        bot.strategy.state["paused_until_day"] = local_day(bot.now)
    elif outcome == "cash":
        bot.strategy.sample_cash(bot.now, 20)
    elif outcome == "expired":
        bot.io.clock = lambda: bot.row["end"]
    tick(bot, **options)
    tick(bot, **options)
    assert bot.row["side98"]["attempted"]
    assert len(bot.buys) == (1 if outcome in ("nofill", "error") else 0)
    assert not bot.row["entered"]
    event = "fivemin_side98_" + ("order_error" if outcome == "error" else "nofill" if outcome == "nofill" else "blocked")
    fields = next(kw for name, kw in bot.logs if name == event)
    if outcome == "nofill":
        assert fields["timing"] == {"test": 1}
        assert "98 SIDE ORDER" in bot.alerts[-1]
    elif outcome not in ("error",):
        assert fields["reason"] == outcome


def test_dry_fill_walks_book_and_has_no_real_spend(bot):
    bot.books["cid"]["up"]["asks"] = [[.99, 100], [.98, 3]]
    tick(bot, fivemin_dry_run=True)
    assert bot.buys == [("up", 20.0, .98, True)]
    pos = bot.row["side98"]["position"]
    result = dry_fill(bot.books["cid"]["up"], 20, .98)
    assert pos["shares"] == result["shares"] == 3
    assert pos["cost"] == result["cost"]
    assert pos["dry_run"]
    assert not bot.strategy.spends
    assert "98 SIDE ORDER" in bot.alerts[-1]


def test_side_only_loss_settles_once_and_trips_daily_stop(bot):
    tick(bot)
    bot.strategy.boundaries[1300] = 99
    bot.now = 1301
    tick(bot, fivemin_daily_stop_usd=-10)
    tick(bot, fivemin_daily_stop_usd=-10)
    pos = bot.row["side98"]["position"]
    assert pos["settled"] and pos["redeem_enqueued"]
    assert pos["pnl"] == pytest.approx(-19.6)
    assert bot.strategy.state["realized"] == pytest.approx(-19.6)
    assert bot.strategy.state["paused_until_day"] == local_day(bot.now)
    assert sum(event == "fivemin_side98_settle" for event, _ in bot.logs) == 1
    assert sum(event == "redeem" for event, _ in bot.logs) == 1
    assert not bot.sells
    assert "cid" not in bot.strategy._active


def test_both_positions_share_gamma_winner_and_redeem_once(bot):
    tick(bot)
    bot.row["position"] = {**bot.row["side98"]["position"], "kind": "main", "side": "down", "cost": 8, "shares": 10}
    bot.row["entered"] = True
    calls = []
    bot.io.gamma_winner = lambda slug: calls.append(slug) or "up"
    bot.now = 1419
    tick(bot)
    assert not calls
    bot.now = 1420
    tick(bot)
    assert len(calls) == 1
    assert all(p["settled"] and p["redeem_enqueued"] for p in (bot.row["position"], bot.row["side98"]["position"]))
    assert sum(event == "redeem" for event, _ in bot.logs) == 1
    assert bot.strategy.state["realized"] == pytest.approx(.4 - 8)


def test_side_survives_restart_prune_and_disabled_redemption(bot):
    tick(bot)
    bot.row["skip_reason"] = "startup"
    state = copy.deepcopy(bot.strategy.state)
    clone = Strategy(bot.io, state, startup_ts=2000)
    assert "cid" in clone._active
    clone._prune(2000)
    assert "cid" in clone.state["markets"]
    row = clone.state["markets"]["cid"]
    assert Runner._has_position(row)
    clone.boundaries[1300] = 120
    clone.tick(helpers.cfg(redeem_enabled=False), 2001, bot.grid, {})
    assert row["side98"]["position"]["settled"]
    assert "cid" in clone._active
    assert Runner._has_position(row)
    clone._prune(2100)
    assert "cid" in clone.state["markets"]
    clone.tick(helpers.cfg(), 2101, bot.grid, {})
    assert row["side98"]["position"]["redeem_enqueued"]
    assert "cid" not in clone._active
    clone._prune(2200)
    assert "cid" not in clone.state["markets"]


def test_side_position_is_held_without_dump(bot):
    tick(bot)
    bot.books["cid"] = {"up": {"bid": .01}, "down": {"bid": .9}}
    bot.now += 1
    tick(bot)
    bot.now += 4
    tick(bot)
    assert not bot.sells
    assert not bot.row["side98"]["position"]["dumped"]


@pytest.mark.parametrize("key,values", [
    ("enabled", [0, 1, "true", None]),
    ("shares", [0, 201, float("nan"), float("inf")]),
    ("price", [0, 1]), ("min_gap_usd", [-1]),
    ("max_ttc_s", [-1, 301]), ("max_adverse_30s_usd", [-1]),
])
def test_mintbot_validation_rejects_bad_side98_values(key, values):
    tree = ast.parse((helpers.ROOT / "mintbot.py").read_text())
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "validate_strategy")
    # Isolate the existing 5m validation preamble from unrelated runtime validators.
    stop = next(i for i, n in enumerate(func.body) if isinstance(n, ast.If)
                and ast.unparse(n.test) == "float(cfg['shares']) <= 0")
    func.body = func.body[:stop]
    ns = {}
    exec(compile(ast.Module(body=[func], type_ignores=[]), "mintbot-validation", "exec"), ns)
    base = helpers.cfg()
    ns["validate_strategy"](base)
    for value in values:
        with pytest.raises(ValueError, match="fivemin_side98_" + key):
            ns["validate_strategy"]({**base, "fivemin_side98_" + key: value})
