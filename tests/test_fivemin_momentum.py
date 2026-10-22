import ast
import math
from types import SimpleNamespace

import pytest

import test_fivemin as helpers
from buy.fivemin import DEFAULTS, EPS


@pytest.fixture
def bot():
    case = helpers.StrategyTests()
    case.setUp()
    case.now = 1250.0
    case.row = case.strategy.state["markets"]["cid"]
    case.row.update(start_twap=100.0, strike=100.0, offset=10.0)
    case.price, case.past = 150.0, 170.0
    case.fav = "up"
    case.grid = SimpleNamespace(
        price_at=lambda g: case.past if g == math.floor(case.now * 10) - 305 else case.price,
        twap60=lambda g: case.price,
        latest_exchange_ms=lambda: case.now * 1000,
    )
    case.strategy.sample_cash(case.now, 1000)
    return case


def tick(bot, **extra):
    other = "down" if bot.fav == "up" else "up"
    quotes = {"cid": {bot.fav: {"bid": .95, "asks": [[.98, 100]]},
                       other: {"bid": .05}}}
    cfg = helpers.cfg(fivemin_momentum_block_enabled=True, **extra)
    bot.strategy.tick(cfg, bot.now, bot.grid, quotes)


def mature(bot, **extra):
    tick(bot, **extra)
    bot.now += 1
    tick(bot, **extra)


def test_defaults_and_validation():
    tree = ast.parse((helpers.ROOT / "mintbot.py").read_text())
    node = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "DEFAULTS" for t in n.targets))
    defaults = {ast.literal_eval(k): v for k, v in zip(node.keys, node.values)}
    for key, value in {"fivemin_momentum_block_enabled": False,
                       "fivemin_momentum_gate_aware": False,
                       "fivemin_momentum_max_adverse_30s_usd": 15.0}.items():
        assert DEFAULTS[key] == ast.literal_eval(defaults[key]) == value
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "validate_strategy")
    # Validate the 5m prefix without importing mintbot's credential-loading runtime.
    stop = next(i for i, n in enumerate(func.body) if isinstance(n, ast.If)
                and ast.unparse(n.test) == "float(cfg['shares']) <= 0")
    func.body = func.body[:stop]
    ns = {}
    exec(compile(ast.Module(body=[func], type_ignores=[]), "mintbot-validation", "exec"), ns)
    validate = ns["validate_strategy"]
    validate(helpers.cfg())
    validate(helpers.cfg(fivemin_momentum_block_enabled=True, fivemin_momentum_max_adverse_30s_usd=0))
    for value in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="fivemin_momentum_max_adverse_30s_usd"):
            validate(helpers.cfg(fivemin_momentum_max_adverse_30s_usd=value))
    for value in (0, 1, "true", None):
        with pytest.raises(ValueError, match="fivemin_momentum_block_enabled"):
            validate(helpers.cfg(fivemin_momentum_block_enabled=value))
    for value in (0, 1, "true", None):
        with pytest.raises(ValueError, match="fivemin_momentum_gate_aware must be boolean"):
            validate(helpers.cfg(fivemin_momentum_gate_aware=value))


def test_off_by_default_enters_despite_large_adverse_move(bot):
    quotes = {"cid": {"up": {"bid": .95}, "down": {"bid": .05}}}
    for now in (1250.0, 1251.0):
        bot.now = now
        bot.strategy.tick(helpers.cfg(), now, bot.grid, quotes)
    assert bot.buys == [("up", 10.0, .99, False)]
    fill = next(fields for event, fields in bot.logs if event == "fivemin_fill")
    assert fill["btc_mom30_usd"] == -20
    assert not any(event.startswith("fivemin_momentum_") for event, _ in bot.logs)


def test_blocks_adverse_logs_once_and_preserves_persist_runs(bot):
    mature(bot)
    run = bot.strategy.runs["cid"]
    assert not bot.buys and not bot.row["entered"]
    assert bot.row["position"] is None
    assert run["last_reason"] == "momentum_block"
    assert run["blocked_momentum"] == 1
    assert run["down"] == 1250.0
    assert run["up"] is None
    assert not run.get("eval_logged")
    fields = [fields for event, fields in bot.logs if event == "fivemin_momentum_block"]
    assert fields == [{"slug": bot.row["slug"], "condition_id": "cid", "side": "up",
                       "adverse_30s_usd": 20.0, "gap_usd": 60.0,
                       "gate_ratio": 60 / 49, "ttc_s": 49.0}]
    bot.now += .2
    tick(bot)
    assert run["blocked_momentum"] == 2
    assert run["down"] == 1250.0
    assert sum(event == "fivemin_momentum_block" for event, _ in bot.logs) == 1


def test_gate_aware_off_matches_existing_block(bot):
    mature(bot, fivemin_momentum_gate_aware=False)
    run = bot.strategy.runs["cid"]
    assert not bot.buys and not bot.row["entered"]
    assert run["last_reason"] == "momentum_block"
    assert run["blocked_momentum"] == 1
    fields = [fields for event, fields in bot.logs if event.startswith("fivemin_momentum_")]
    assert fields == [{"slug": bot.row["slug"], "condition_id": "cid", "side": "up",
                       "adverse_30s_usd": 20.0, "gap_usd": 60.0,
                       "gate_ratio": 60 / 49, "ttc_s": 49.0}]


def test_gate_aware_small_headroom_still_blocks(bot):
    mature(bot, fivemin_momentum_gate_aware=True)
    run = bot.strategy.runs["cid"]
    assert not bot.buys and not bot.row["entered"]
    assert run["last_reason"] == "momentum_block"
    assert not any(event == "fivemin_momentum_pass_headroom" for event, _ in bot.logs)
    fields = [fields for event, fields in bot.logs if event == "fivemin_momentum_block"]
    assert fields == [{"slug": bot.row["slug"], "condition_id": "cid", "side": "up",
                       "adverse_30s_usd": 20.0, "gap_usd": 60.0,
                       "gate_ratio": 60 / 49, "ttc_s": 49.0}]


def test_gate_aware_large_headroom_enters_and_logs_pass_once(bot):
    bot.price, bot.past = 300.0, 320.0
    extra = {"fivemin_momentum_gate_aware": True}
    bot.row["main_attempts"] = 3
    mature(bot, **extra)
    for now in (1251.2, 1251.4):
        bot.now = now
        tick(bot, **extra)
    run = bot.strategy.runs["cid"]
    assert not bot.buys and not bot.row["entered"]
    assert not any(event == "fivemin_momentum_block" for event, _ in bot.logs)
    passes = [fields for event, fields in bot.logs if event == "fivemin_momentum_pass_headroom"]
    assert len(passes) == 1
    assert passes[0]["side"] == "up" and passes[0]["adverse_30s_usd"] == 20.0
    assert passes[0]["gap_usd"] == 210.0 and passes[0]["ttc_s"] == 49.0
    assert passes[0]["gate_ratio"] == pytest.approx(210 / 49)
    assert passes[0]["headroom"] == pytest.approx(210 - 20 - 49)
    bot.row["main_attempts"] = 0
    bot.now += .2
    tick(bot, **extra)
    assert bot.buys and bot.row["entered"]
    assert run["last_reason"] != "momentum_block"
    bot.now += .2
    tick(bot, **extra)
    assert sum(event == "fivemin_momentum_pass_headroom" for event, _ in bot.logs) == 1


@pytest.mark.parametrize("mom30", [-15.0, -15.0 - EPS / 2, 10.0])
@pytest.mark.parametrize("fav", ["up", "down"])
def test_threshold_and_favourable_moves_allow_both_sides(bot, fav, mom30):
    bot.fav = fav
    sign = 1 if fav == "up" else -1
    if fav == "down":
        bot.row["strike"] = 220.0
    bot.past = bot.price - sign * mom30
    mature(bot)
    assert bot.buys
    fill = next(fields for event, fields in bot.logs if event == "fivemin_fill")
    assert fill["btc_mom30_usd"] == pytest.approx(mom30)


def test_blocked_then_calm_tick_enters_without_new_persistence(bot):
    mature(bot)
    assert not bot.buys and not bot.row["entered"]
    bot.past = bot.price
    bot.now += .2
    tick(bot)
    assert bot.buys and bot.row["entered"]
    assert bot.strategy.runs["cid"]["down"] == 1250.0


def test_missing_past_allows_entry_with_one_missing_log(bot):
    bot.past = None
    mature(bot)
    assert bot.buys
    assert sum(event == "fivemin_momentum_data_missing" for event, _ in bot.logs) == 1
    fill = next(fields for event, fields in bot.logs if event == "fivemin_fill")
    assert fill["btc_mom30_usd"] is None
    bot.now += .2
    tick(bot)
    assert sum(event == "fivemin_momentum_data_missing" for event, _ in bot.logs) == 1


@pytest.mark.parametrize("change", ["stale", "missing_latest", "missing_spot", "missing_past"])
def test_shared_helper_stale_and_missing_data(bot, change):
    if change == "stale":
        bot.grid.latest_exchange_ms = lambda: (bot.now - 3.01) * 1000
    elif change == "missing_latest":
        bot.grid.latest_exchange_ms = lambda: None
    elif change == "missing_spot":
        bot.grid.price_at = lambda g: None if g == math.floor(bot.now * 10) - 5 else 170
    else:
        bot.past = None
    assert bot.strategy._btc_mom30(helpers.cfg(), bot.now, bot.grid, "up") is None


def test_blocked_main_signal_falls_through_to_side98(bot):
    mature(bot, fivemin_side98_enabled=True, fivemin_side98_max_adverse_30s_usd=25)
    assert not bot.row["entered"]
    assert bot.buys == [("up", 20.0, .98, False)]
    assert bot.row["side98"]["mom30"] == -20


@pytest.mark.parametrize("change", ["disable", "threshold"])
def test_momentum_settings_hot_reload_on_next_tick(bot, change):
    mature(bot)
    assert not bot.buys
    bot.now += .2
    if change == "disable":
        quotes = {"cid": {"up": {"bid": .95}, "down": {"bid": .05}}}
        bot.strategy.tick(helpers.cfg(), bot.now, bot.grid, quotes)
    else:
        tick(bot, fivemin_momentum_max_adverse_30s_usd=20)
    assert bot.buys


def test_nofill_logs_include_momentum(bot):
    bot.past = 140.0
    bot.io.buy = lambda *args: {"shares": 0, "cost": 0, "status": "unfilled"}
    mature(bot)
    for event in ("fivemin_fill", "fivemin_signal_nofill"):
        fields = next(fields for logged, fields in bot.logs if logged == event)
        assert fields["btc_mom30_usd"] == 10
