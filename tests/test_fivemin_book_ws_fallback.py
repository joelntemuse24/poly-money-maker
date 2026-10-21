"""5m websocket fallback tests with injected feeds and no live clients."""
import ast
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from buy.clob_book_ws import ClobBookFeed, Top
from buy.fivemin import DEFAULTS, settings
from buy.fivemin_books import RestBookFeed, WsFallbackBookFeed


SOURCE = Path(__file__).resolve().parents[1] / "mintbot.py"
BOOK = (.1, 10, [{"price": .1, "size": 10}], .2, 5, [], None)


def fake_pair(clock, tops=None, healthy=True):
    tops = {} if tops is None else tops
    ws = Mock()
    ws.healthy.return_value = healthy
    ws.usable.side_effect = tops.get
    rest = Mock()
    rest.usable.return_value = Top(bid=.1, ask=.2)
    rest.reason.return_value = None
    logs = Mock()
    feed = WsFallbackBookFeed(ws, rest, clock=lambda: clock[0], log=logs)
    return feed, ws, rest, logs


def snapshot(ws, token):
    ws.apply_raw(json.dumps({"event_type": "book", "asset_id": token,
                            "bids": [["0.10", "10"], ["0.12", "6"]],
                            "asks": [["0.30", "8"], ["0.20", "5"]]}))


def test_snapshot_deltas_removal_ordering_and_wanted_tokens():
    ws = ClobBookFeed(clock=lambda: 1000)
    ws.set_tokens(["up", "dn"])
    ws.set_min_size(0)
    ws._healthy = True
    snapshot(ws, "up")
    snapshot(ws, "ignored")
    ws.apply_raw(json.dumps({"event_type": "price_change", "price_changes": [
        {"asset_id": "up", "side": "SELL", "price": "0.20", "size": "0"},
        {"asset_id": "up", "side": "SELL", "price": "0.25", "size": "4"},
        {"asset_id": "up", "side": "BUY", "price": "0.12", "size": "0"},
        {"asset_id": "up", "side": "BUY", "price": "0.11", "size": "7"},
        {"asset_id": "ignored", "side": "BUY", "price": "0.15", "size": "3"},
    ]}))
    top = ws.usable("up")
    assert (top.bid, top.bid_sz, top.ask, top.ask_sz) == (.11, 7, .25, 4)
    assert [row["price"] for row in top.bids] == [.11, .1]
    assert [row["price"] for row in top.asks] == [.25, .3]
    assert set(ws._books) == {"up"}
    snapshot(ws, "dn")
    ws.set_tokens(["dn"])
    assert set(ws._books) == {"dn"}
    assert ws.usable("up") is None


def test_stale_ws_falls_back_to_rest_with_separate_freshness():
    clock = [1000.0]
    ws = ClobBookFeed(clock=lambda: clock[0])
    rest = RestBookFeed(Mock(return_value=(BOOK, BOOK)), clock=lambda: clock[0])
    limit = [1.0]
    feed = WsFallbackBookFeed(ws, rest, ws_stale_s=lambda: limit[0], clock=lambda: clock[0])
    feed.set_tokens(["up", "dn"])
    feed.set_stale_s(30)
    feed.set_min_size(0)
    ws._healthy = True
    snapshot(ws, "up")
    snapshot(ws, "dn")
    feed.poll(clock[0], "up", "dn")
    rest.fetch_pair.assert_not_called()
    clock[0] += 1.01
    feed.poll(clock[0], "up", "dn")
    rest.fetch_pair.assert_called_once_with("up", "dn", fail={})
    assert feed.usable("up") is rest.usable("up")
    assert feed.reason("up") is None
    assert ws._stale_s == 1 and rest._stale_s == 30
    limit[0] = 2
    feed.poll(clock[0], "up", "dn")
    assert ws._stale_s == 2
    assert rest.fetch_pair.call_count == 1
    assert feed.usable("up").bid == .12
    limit[0] = .5
    feed.set_tokens(["up", "dn"])
    assert ws._stale_s == .5


def test_disconnected_rest_and_transition_logs_once():
    clock = [0.0]
    feed, ws, rest, logs = fake_pair(clock, healthy=False)
    for now in (0, 1, 30, 90):
        clock[0] = now
        feed.poll(now, "up", "dn")
    assert rest.poll.call_count == 4
    assert logs.call_args_list == [
        (("fivemin_ws_disconnect",), {}),
        (("fivemin_ws_fallback",), {"token_count": 2, "reason": "disconnected"}),
    ]
    assert feed.usable("up") is rest.usable.return_value
    rest.reason.return_value = "fetch_fail"
    assert feed.reason("up") == "fetch_fail"


def test_fresh_ws_skips_rest_and_mixed_books_prefer_ws():
    top = Top(bid=.1, ask=.2)
    tops = {"up": top, "dn": top}
    feed, ws, rest, logs = fake_pair([1000.0], tops)
    feed.poll(1000, "up", "dn")
    feed.poll(1000, "up", "dn")
    rest.poll.assert_not_called()
    logs.assert_called_once_with("fivemin_ws_connect")
    assert feed.usable("up") is top
    assert feed.reason("up") is None
    rest.reason.assert_not_called()
    del tops["dn"]
    feed.poll(1000, "up", "dn")
    rest.poll.assert_called_once_with(1000, "up", "dn")
    assert feed.usable("up") is top
    assert feed.usable("dn") is rest.usable.return_value
    logs.assert_called_with("fivemin_ws_fallback", token_count=1, reason="stale_or_missing")


def test_transitions_rate_limited_per_event_and_recovery():
    clock = [0.0]
    top = Top(bid=.1)
    tops = {"up": top, "dn": top}
    feed, ws, rest, logs = fake_pair(clock, tops)
    for now, healthy in ((0, True), (1, False), (2, True), (3, False),
                         (30, True), (31, False), (61, False)):
        clock[0] = now
        ws.healthy.return_value = healthy
        tops.clear()
        if healthy:
            tops.update(up=top, dn=top)
        feed.poll(now, "up", "dn")
    names = [call.args[0] for call in logs.call_args_list]
    assert names.count("fivemin_ws_connect") == 2
    assert names.count("fivemin_ws_disconnect") == 2
    assert names.count("fivemin_ws_fallback") == 2


def test_lifecycle_and_settings_forwarded_to_both():
    feed, ws, rest, _ = fake_pair([0.0])
    feed.start()
    feed.set_tokens(iter(["up", "dn"]))
    feed.set_min_size(0)
    feed.set_stale_s(30)
    feed.stop()
    for child in (ws, rest):
        child.start.assert_called_once_with()
        child.stop.assert_called_once_with()
        child.set_tokens.assert_called_once_with(["up", "dn"])
        child.set_min_size.assert_called_once_with(0)
    ws.set_stale_s.assert_called_once_with(1)
    rest.set_stale_s.assert_called_once_with(30)


def startup_expression():
    tree = ast.parse(SOURCE.read_text())
    return next(node for node in ast.walk(tree) if isinstance(node, ast.IfExp)
                and isinstance(node.test, ast.Call) and node.test.args
                and isinstance(node.test.args[0], ast.Constant)
                and node.test.args[0].value == "fivemin_book_ws")


@pytest.mark.parametrize("flag", [False, True])
def test_startup_flag_off_keeps_rest_flag_on_wraps_and_hot_reloads(flag):
    cfg = {"fivemin_book_ws": flag}
    ws_ctor = Mock(return_value=Mock())
    error = Mock()
    fetch = Mock(return_value=(BOOK, BOOK))
    log = Mock()
    feed = eval(compile(ast.Expression(startup_expression()), str(SOURCE), "eval"),
                dict(cfg=cfg, ClobBookFeed=ws_ctor, RestBookFeed=RestBookFeed,
                     WsFallbackBookFeed=WsFallbackBookFeed, settings=settings,
                     current_cfg=lambda: cfg, _fetch_books=fetch, log_event=log,
                     fivemin_ws_error=error))
    rest = feed.rest if flag else feed
    assert isinstance(rest, RestBookFeed)
    if flag:
        assert isinstance(feed, WsFallbackBookFeed)
        ws_ctor.assert_called_once_with(on_error=error, custom_features=False)
        assert feed.ws_stale_s() == 1
        cfg["fivemin_book_ws_stale_s"] = .25
        assert feed.ws_stale_s() == .25
    else:
        ws_ctor.assert_not_called()
    rest.poll(1000, "up", "dn")
    fetch.assert_called_once_with("up", "dn", 0, {})


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_ws_stale_validation_rejects_invalid(value):
    tree = ast.parse(SOURCE.read_text())
    validate = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "validate_strategy")
    ns = {}
    exec(compile(ast.Module(body=[validate], type_ignores=[]), str(SOURCE), "exec"), ns)
    with pytest.raises(ValueError, match="fivemin_book_ws_stale_s"):
        ns["validate_strategy"]({"fivemin_binance_url": "wss://example", "fivemin_book_ws_stale_s": value})


def test_ws_stale_defaults_and_small_positive_validation():
    tree = ast.parse(SOURCE.read_text())
    defaults = next(node for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "DEFAULTS" for t in node.targets))
    assert ast.literal_eval(defaults.value)["fivemin_book_ws_stale_s"] == DEFAULTS["fivemin_book_ws_stale_s"] == 1.0
    validate = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "validate_strategy")
    # The isolated 5m validation prefix ends before the unrelated mint configuration.
    end = next(i for i, node in enumerate(validate.body) if isinstance(node, ast.If)
               and 'cfg[\'shares\']' in ast.unparse(node.test))
    prefix = ast.Module(body=validate.body[:end], type_ignores=[])
    exec(compile(prefix, str(SOURCE), "exec"),
         {"cfg": {"fivemin_binance_url": "wss://example", "fivemin_book_ws_stale_s": .001}})


def test_subscribe_payload_can_skip_custom_features():
    from buy.clob_book_ws import subscribe_payload
    assert subscribe_payload(["a"])["custom_feature_enabled"] is True
    assert subscribe_payload(["a"], False)["custom_feature_enabled"] is False
