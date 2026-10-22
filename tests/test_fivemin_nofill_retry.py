"""Main-entry retries use injected I/O and AST-only mintbot configuration."""
import ast

import pytest

import test_fivemin as helpers
from buy.fivemin import DEFAULTS, Runner, pending_position, positions


@pytest.fixture
def bot():
    case = helpers.StrategyTests()
    case.setUp()
    case.row = case.strategy.state["markets"]["cid"]
    case.strategy.runs["cid"] = {"up": 1198.0}
    case.books = case.quotes()
    case.result = {"shares": 0, "cost": 0, "status": "unfilled", "timing": {"post": 12}}

    def buy(*args):
        assert case.row["entered"]
        assert case.row["main_attempts"] == len(case.buys) + 1
        case.buys.append(args)
        return dict(case.result)

    case.io.buy = buy
    return case


def tick(bot, now=1200.0, **options):
    bot.strategy.tick(helpers.cfg(**options), now, bot.grid, bot.books)


def test_real_nofill_preserves_market_and_persists_retry_state(bot):
    tick(bot)
    assert len(bot.buys) == 1
    assert bot.row["entered"] is False
    assert bot.row["position"] is None
    assert bot.row["main_attempts"] == bot.row["nofill_count"] == 1
    assert bot.row["last_main_attempt_ts"] == 1200.0
    assert not bot.strategy.runs["cid"]["eval_logged"]
    assert not bot.strategy.spends
    assert positions(bot.row) == []
    assert not pending_position(bot.row)
    assert not Runner._has_position(bot.row)
    assert "cid" in bot.strategy._active
    saved = bot.saved[-1]["markets"]["cid"]
    assert saved == bot.row
    assert bot.strategy.state["realized"] == 0
    assert bot.strategy.state["paused_until_day"] is None
    for event in ("fivemin_signal", "fivemin_fill", "fivemin_signal_nofill"):
        fields = next(fields for name, fields in bot.logs if name == event)
        assert fields["attempt"] == 1
    assert len(bot.alerts) == 1


def test_retry_waits_for_cooldown_and_logs_later_enter_eval(bot):
    tick(bot)
    bot.result = {"shares": 10, "cost": 9.9, "status": "matched"}
    for now in (1200.2, 1201.999):
        tick(bot, now)
        assert len(bot.buys) == 1
        assert bot.strategy.runs["cid"]["last_reason"] == "nofill_cooldown"
    tick(bot, 1202.0)
    assert len(bot.buys) == 2
    assert bot.row["entered"]
    assert bot.row["position"]["shares"] == 10
    assert bot.row["main_attempts"] == 2
    assert bot.row["nofill_count"] == 1
    evaluations = [fields for name, fields in bot.logs if name == "fivemin_eval"]
    assert [fields["decision"] for fields in evaluations] == ["enter", "enter"]
    for event in ("fivemin_signal", "fivemin_fill"):
        assert [fields["attempt"] for name, fields in bot.logs if name == event] == [1, 2]
    assert len(bot.strategy.spends) == 1


@pytest.mark.parametrize("cap", [1, 3])
def test_attempt_cap_and_one_nofill_alert_per_market(bot, cap):
    for index in range(cap + 3):
        tick(bot, 1200.0 + index * 2, fivemin_max_attempts_per_market=cap)
    assert len(bot.buys) == cap
    assert bot.row["main_attempts"] == bot.row["nofill_count"] == cap
    assert not bot.row["entered"]
    assert bot.row["position"] is None
    assert bot.strategy.runs["cid"]["last_reason"] == "max_attempts"
    assert len(bot.alerts) == 1
    assert [fields["attempt"] for name, fields in bot.logs if name == "fivemin_signal_nofill"] == list(range(1, cap + 1))


@pytest.mark.parametrize("guard", ["cooldown", "cap"])
def test_side98_can_fire_after_main_nofill(bot, guard):
    bot.price = 0.0
    bot.books["cid"]["down"]["asks"] = [[0.98, 100]]
    bot.strategy.runs["cid"] = {"up": 1248.0}
    bot.strategy.sample_cash(1250.0, 1000.0)
    options = {"fivemin_side98_enabled": True}
    if guard == "cap":
        options["fivemin_max_attempts_per_market"] = 1
    tick(bot, 1250.0, **options)
    assert len(bot.buys) == 1
    assert not bot.row.get("side98")
    bot.result = {"shares": 20, "cost": 19.6, "status": "matched"}
    # side98 has its own attempt counter and must bypass the main-entry assertion.
    bot.io.buy = lambda *args: bot.buys.append(args) or dict(bot.result)
    tick(bot, 1250.2, **options)
    assert [order[2] for order in bot.buys] == [0.99, 0.98]
    assert bot.row["side98"]["position"]["shares"] == 20
    assert not bot.row["entered"]
    assert bot.row["position"] is None
    assert bot.row["main_attempts"] == 1
    assert bot.strategy.runs["cid"]["last_reason"] == ("max_attempts" if guard == "cap" else "nofill_cooldown")


def test_partial_fill_stays_entered_at_actual_size(bot):
    bot.result = {"shares": 2.5, "cost": 2.45, "status": "partial"}
    tick(bot)
    tick(bot, 1203.0)
    assert len(bot.buys) == 1
    assert bot.row["entered"]
    assert bot.row["position"]["shares"] == 2.5
    assert bot.row["position"]["cost"] == 2.45
    assert "nofill_count" not in bot.row
    assert pending_position(bot.row)


def test_dry_zero_fill_keeps_existing_consumed_market_behavior(bot):
    tick(bot, fivemin_dry_run=True)
    tick(bot, 1203.0, fivemin_dry_run=True)
    assert len(bot.buys) == 1
    assert bot.row["entered"]
    assert bot.row["position"]["shares"] == 0
    assert bot.row["position"]["dry_run"]
    assert "nofill_count" not in bot.row
    assert not any(name == "fivemin_signal_nofill" for name, _ in bot.logs)


def test_nofill_expiry_skip_eval_and_prune_without_settlement(bot):
    tick(bot)
    bot.strategy.boundaries[1300] = 101
    tick(bot, 1300.0)
    assert "cid" not in bot.strategy._active
    summary = [fields for name, fields in bot.logs if name == "fivemin_eval"][-1]
    assert summary["decision"] == "skip"
    assert summary["main_attempts"] == summary["nofill_count"] == 1
    assert summary["loser_empty_no_ask"] == 0
    assert bot.strategy.state["realized"] == 0
    assert not any(name in ("fivemin_settle", "redeem", "fivemin_daily_stop") for name, _ in bot.logs)
    tick(bot, 1901.0)
    assert "cid" not in bot.strategy.state["markets"]
    assert "cid" not in bot.strategy.runs


def test_retry_settings_hot_reload_each_tick(bot):
    tick(bot, fivemin_nofill_retry_s=10, fivemin_max_attempts_per_market=1)
    tick(bot, 1201.0, fivemin_nofill_retry_s=10, fivemin_max_attempts_per_market=3)
    assert len(bot.buys) == 1
    assert bot.strategy.runs["cid"]["last_reason"] == "nofill_cooldown"
    tick(bot, 1201.0, fivemin_nofill_retry_s=0, fivemin_max_attempts_per_market=3)
    assert len(bot.buys) == 2
    tick(bot, 1202.0, fivemin_nofill_retry_s=0, fivemin_max_attempts_per_market=2)
    assert len(bot.buys) == 2
    assert bot.strategy.runs["cid"]["last_reason"] == "max_attempts"


@pytest.mark.parametrize("gate", ["ratio", "momentum", "persistence", "ask_depth"])
def test_each_retry_rechecks_signal_requirements(bot, gate):
    tick(bot)
    options = {}
    if gate == "ratio":
        bot.price = 100.0
    elif gate == "momentum":
        bot.grid.price_at = lambda g: 0.0 if g == 12015 else -20.0 if g == 11715 else 0.0
        options["fivemin_momentum_block_enabled"] = True
    elif gate == "persistence":
        options["fivemin_persist_s"] = 10
    else:
        bot.books["cid"]["up"] = {"bid": None, "miss": "empty"}
    tick(bot, 1202.0, **options)
    assert len(bot.buys) == 1
    assert not bot.row["entered"]
    assert bot.row["main_attempts"] == 1


def mintbot_config():
    tree = ast.parse((helpers.ROOT / "mintbot.py").read_text())
    defaults_node = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == "DEFAULTS" for target in node.targets))
    defaults = {ast.literal_eval(key): ast.literal_eval(value)
                for key, value in zip(defaults_node.keys, defaults_node.values)}
    validate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "validate_strategy")
    stop = next(index for index, node in enumerate(validate.body)
                if isinstance(node, ast.If) and ast.unparse(node.test) == "float(cfg['shares']) <= 0")
    validate.body = validate.body[:stop]
    namespace = {}
    exec(compile(ast.Module(body=[validate], type_ignores=[]), "mintbot-validation", "exec"), namespace)
    return defaults, namespace["validate_strategy"]


def test_retry_defaults_match_and_validate_when_omitted():
    defaults, validate = mintbot_config()
    for key, expected in (("fivemin_nofill_retry_s", 2.0), ("fivemin_max_attempts_per_market", 3)):
        assert DEFAULTS[key] == defaults[key] == expected
    config = helpers.cfg()
    config.pop("fivemin_nofill_retry_s")
    config.pop("fivemin_max_attempts_per_market")
    validate(config)


@pytest.mark.parametrize("key,value", [
    ("fivemin_nofill_retry_s", -0.01), ("fivemin_nofill_retry_s", float("nan")),
    ("fivemin_nofill_retry_s", float("inf")), ("fivemin_nofill_retry_s", float("-inf")),
    ("fivemin_max_attempts_per_market", 0), ("fivemin_max_attempts_per_market", 21),
    ("fivemin_max_attempts_per_market", 1.5), ("fivemin_max_attempts_per_market", True),
    ("fivemin_max_attempts_per_market", float("nan")), ("fivemin_max_attempts_per_market", float("inf")),
])
def test_invalid_retry_settings_rejected(key, value):
    _, validate = mintbot_config()
    with pytest.raises(ValueError, match=key):
        validate(helpers.cfg(**{key: value}))


@pytest.mark.parametrize("delay,cap", [(0, 1), (2.5, 20), (1000000, 3)])
def test_retry_setting_boundaries_accepted(delay, cap):
    _, validate = mintbot_config()
    validate(helpers.cfg(fivemin_nofill_retry_s=delay, fivemin_max_attempts_per_market=cap))
