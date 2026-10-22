import ast
from types import SimpleNamespace

import pytest

import test_fivemin as helpers
from buy.fivemin import DEFAULTS


def make_bot():
    case = helpers.StrategyTests()
    case.setUp()
    case.row = case.strategy.state["markets"]["cid"]
    case.strategy.sample_cash(1200.0, 1000.0)
    return case


def empty_loser_quotes(fav_bid=0.95, fav_side="down", fav_asks=None, miss="empty"):
    loser = "up" if fav_side == "down" else "down"
    return {
        "cid": {
            loser: {"bid": None, "miss": miss},
            fav_side: {"bid": fav_bid, "asks": [[0.99, 100]] if fav_asks is None else fav_asks},
        }
    }


def test_empty_loser_is_zero_after_persistence_and_logs_reason():
    bot = make_bot()
    quotes = empty_loser_quotes()
    bot.strategy.tick(helpers.cfg(), 1200.0, bot.grid, quotes)
    assert not bot.buys
    bot.strategy.tick(helpers.cfg(), 1202.0, bot.grid, quotes)

    assert bot.buys == [("dn", 10.0, 0.99, False)]
    evaluation = next(fields for event, fields in bot.logs if event == "fivemin_eval")
    assert evaluation["reason"] == "signal_loser_empty_as_zero"
    assert evaluation["bids"] == {"up": None, "down": 0.95}
    assert evaluation["loser_empty_as_zero"] is True
    assert bot.strategy.runs["cid"]["loser_empty_as_zero"] == 2


@pytest.mark.parametrize("miss", ["stale", "no_book", "fetch_fail"])
def test_other_missing_book_causes_do_not_substitute(miss):
    bot = make_bot()
    quotes = empty_loser_quotes(miss=miss)
    for now in (1200.0, 1202.0, 1204.0):
        bot.strategy.tick(helpers.cfg(), now, bot.grid, quotes)

    assert not bot.buys
    assert not bot.row["entered"]
    assert "loser_empty_as_zero" not in bot.strategy.runs["cid"]


def test_feature_flag_disables_empty_loser_substitution():
    bot = make_bot()
    quotes = empty_loser_quotes()
    for now in (1200.0, 1202.0, 1204.0):
        bot.strategy.tick(helpers.cfg(fivemin_loser_empty_as_zero=False), now, bot.grid, quotes)

    assert not bot.buys
    assert not bot.row["entered"]
    assert "loser_empty_as_zero" not in bot.strategy.runs["cid"]


def test_empty_loser_does_not_qualify_below_favourite_threshold():
    bot = make_bot()
    quotes = empty_loser_quotes(fav_bid=0.85)
    for now in (1200.0, 1202.0, 1204.0):
        bot.strategy.tick(helpers.cfg(), now, bot.grid, quotes)

    assert not bot.buys
    assert not bot.row["entered"]
    assert "loser_empty_as_zero" not in bot.strategy.runs["cid"]


def test_side98_uses_raw_quotes_when_main_gate_is_below_threshold():
    bot = make_bot()
    bot.now = 1250.0
    bot.price = 115.0
    bot.past = 115.0
    bot.latest = bot.now * 1000
    bot.grid = SimpleNamespace(
        price_at=lambda g: bot.past if g == int(bot.now * 10) - 305 else bot.price,
        latest_exchange_ms=lambda: bot.latest,
    )
    bot.strategy.sample_cash(bot.now, 1000.0)
    quotes = empty_loser_quotes(fav_side="up", fav_asks=[[0.98, 100]])

    bot.strategy.runs["cid"] = {"down": bot.now - 2.0}
    bot.strategy.tick(helpers.cfg(fivemin_side98_enabled=True), bot.now, bot.grid, quotes)

    assert bot.strategy.runs["cid"]["blocked_gate_below_k"] == 1
    assert bot.buys == [("up", 20.0, 0.98, False)]
    assert bot.row["side98"]["side"] == "up"
    assert not bot.row["entered"]


@pytest.mark.parametrize("asks", [[], [[1.0, 100]], [[0.99, 0.99]], [[0.98, 0.4], [0.99, 0.5], [1.0, 100]]])
def test_empty_loser_without_minimum_ask_depth_leaves_row_untouched(asks):
    bot = make_bot()
    bot.strategy.tick(helpers.cfg(), 1200.0, bot.grid, empty_loser_quotes(fav_asks=asks))
    before = dict(bot.row)
    for now in (1202.0, 1203.0):
        bot.strategy.tick(helpers.cfg(), now, bot.grid, empty_loser_quotes(fav_asks=asks))
    assert not bot.buys
    assert bot.row == before
    assert not bot.row["entered"]
    assert bot.strategy.runs["cid"]["loser_empty_no_ask"] == 2
    assert bot.strategy.runs["cid"]["last_reason"] == "loser_empty_no_ask"
    assert not any(event == "fivemin_eval" for event, _ in bot.logs)
    bot.strategy.tick(helpers.cfg(), 1300.0, bot.grid, {})
    summary = next(fields for event, fields in bot.logs if event == "fivemin_eval")
    assert summary["decision"] == "skip"
    assert summary["loser_empty_no_ask"] == 2


@pytest.mark.parametrize("asks", [[[0.99, 1]], [[0.98, 0.4], [0.99, 0.6]], [[0.98, 2]]])
def test_empty_loser_with_minimum_ask_depth_enters(asks):
    bot = make_bot()
    for now in (1200.0, 1202.0):
        bot.strategy.tick(helpers.cfg(), now, bot.grid, empty_loser_quotes(fav_asks=asks))
    assert bot.buys == [("dn", 10.0, 0.99, False)]
    assert bot.row["entered"]


@pytest.mark.parametrize("asks", [[], [[1.0, 100]]])
def test_two_sided_signal_preserves_entry_without_eligible_asks(asks):
    bot = make_bot()
    quotes = bot.quotes()
    quotes["cid"]["down"]["asks"] = asks
    for now in (1200.0, 1202.0):
        bot.strategy.tick(helpers.cfg(), now, bot.grid, quotes)
    assert bot.buys == [("dn", 10.0, 0.99, False)]
    assert "loser_empty_no_ask" not in bot.strategy.runs["cid"]


def test_empty_loser_ask_guard_allows_side98_later_in_same_market():
    bot = make_bot()
    bot.price = 200.0
    quotes = empty_loser_quotes(fav_side="up", fav_asks=[])
    options = helpers.cfg(fivemin_side98_enabled=True)
    for now in (1200.0, 1202.0):
        bot.strategy.tick(options, now, bot.grid, quotes)
    assert not bot.buys
    assert not bot.row["entered"]
    assert bot.strategy.runs["cid"]["loser_empty_no_ask"] == 1
    quotes["cid"]["up"]["asks"] = [[0.98, 0.5]]
    bot.strategy.tick(options, 1250.0, bot.grid, quotes)
    assert bot.buys == [("up", 20.0, 0.98, False)]
    assert bot.row["side98"]["side"] == "up"
    assert bot.row["position"] is None
    assert not bot.row["entered"]
    assert bot.strategy.runs["cid"]["loser_empty_no_ask"] == 2


def test_default_and_validation_for_empty_loser_flag():
    tree = ast.parse((helpers.ROOT / "mintbot.py").read_text())
    defaults_node = next(
        node.value for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "DEFAULTS" for target in node.targets)
    )
    mint_defaults = {
        ast.literal_eval(key): ast.literal_eval(value)
        for key, value in zip(defaults_node.keys, defaults_node.values)
    }
    assert DEFAULTS["fivemin_loser_empty_as_zero"] is True
    assert mint_defaults["fivemin_loser_empty_as_zero"] is True

    validate_node = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "validate_strategy"
    )
    stop = next(
        index for index, node in enumerate(validate_node.body)
        if isinstance(node, ast.If) and ast.unparse(node.test) == "float(cfg['shares']) <= 0"
    )
    validate_node.body = validate_node.body[:stop]
    namespace = {}
    exec(compile(ast.Module(body=[validate_node], type_ignores=[]), "mintbot-validation", "exec"), namespace)
    base = helpers.cfg()
    without_flag = dict(base)
    without_flag.pop("fivemin_loser_empty_as_zero")
    namespace["validate_strategy"](without_flag)

    for value in (0, 1, "true", None):
        with pytest.raises(ValueError, match="fivemin_loser_empty_as_zero must be boolean"):
            namespace["validate_strategy"]({**base, "fivemin_loser_empty_as_zero": value})
