"""Diagnostic logging for the isolated 5m strategy; never imports mintbot."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from buy.clob_book_ws import ClobBookFeed
from buy.fivemin import FiveMinIO, Runner, Strategy, decision_book_fields
from buy.fivemin_books import RestBookFeed, WsFallbackBookFeed

import test_fivemin as helpers

BOOK = (.10, 10.0, [{"price": ".10", "size": "10"}], .20, 5.0, [{"price": ".20", "size": "5"}], .03)

REJECT = {"shares": 0, "cost": 0, "status": "reject:400:no orders found to match", "timing": {"post": 12}}
GRID = SimpleNamespace(price_at=lambda g: 0.0, latest_exchange_ms=lambda: 1e15)


def make_case():
    case = SimpleNamespace()
    case.logs, case.buys, case.alerts = [], [], []
    case.clock = [1200.0]
    case.io = FiveMinIO(
        buy=lambda token, shares, limit, dry: case.buys.append((token, shares, limit, dry)) or dict(case.result),
        sell=lambda *a: {}, reserves=lambda cfg: 0, redeem=lambda *a: None,
        save=lambda state: None,
        log=lambda event, **kw: case.logs.append((event, kw)),
        alert=lambda text, **kw: case.alerts.append(text),
        durable=lambda: True, clock=lambda: case.clock[0],
    )
    case.io.rest_book = None
    case.result = dict(REJECT)
    case.strategy = Strategy(case.io, startup_ts=0)
    case.strategy.add_market(helpers.market(), now=1000)
    row = case.strategy.state["markets"]["cid"]
    row["strike"] = 100.0
    row["offset"] = 0.0
    case.strategy.sample_cash(1200, 1000)
    return case


def main_quotes(extra=None):
    down = {"bid": .90, "bids": [], "asks": [[.97, 2], [.98, 3], [.99, 4]],
            "age_ms": 35.2, "src": "ws"}
    up = {"bid": .05, "bids": [], "asks": [], "age_ms": 40.0, "src": "rest"}
    quotes = {"cid": {"up": up, "down": down}}
    if extra:
        quotes.update(extra)
    return quotes


def main_tick(case, now=1200.0, quotes=None, **options):
    case.strategy.runs["cid"] = {"up": 1198.0}
    case.strategy.sample_cash(1200, 1000)
    case.strategy.tick(helpers.cfg(fivemin_gate_k=0, **options), now, GRID,
                       main_quotes() if quotes is None else quotes)


def join_refetch_threads(strategy, timeout=5.0):
    for thread in strategy._refetch_threads:
        thread.join(timeout=timeout)
    return [thread for thread in strategy._refetch_threads if thread.is_alive()]


def events(case, name):
    return [kw for event, kw in case.logs if event == name]


def log_events(logs, name):
    return [kw for event, kw in logs if event == name]


def test_decision_fields_on_main_fill_and_nofill_events():
    case = make_case()
    case.io.buy = lambda *args: {"shares": 10, "cost": 9.9, "status": "matched"}
    main_tick(case)
    fill = events(case, "fivemin_fill")[0]
    assert fill["book_age_ms"] == 35.2
    assert fill["book_src"] == "ws"
    assert fill["decision_ask_depth"] == 9
    assert fill["decision_best_ask"] == .97
    case = make_case()
    main_tick(case)
    nofill = events(case, "fivemin_signal_nofill")[0]
    assert nofill["attempt"] == 1
    assert nofill["book_age_ms"] == 35.2
    assert nofill["book_src"] == "ws"
    assert nofill["decision_ask_depth"] == 9
    assert nofill["decision_best_ask"] == .97


def test_decision_fields_default_to_rest_with_unknown_age():
    case = make_case()
    quotes = main_quotes()
    quotes["cid"]["down"] = {"bid": .90}
    main_tick(case, quotes=quotes)
    nofill = events(case, "fivemin_signal_nofill")[0]
    assert nofill["book_age_ms"] is None
    assert nofill["book_src"] == "rest"
    assert nofill["decision_ask_depth"] == 0
    assert nofill["decision_best_ask"] is None
    assert join_refetch_threads(case.strategy) == []


def test_decision_book_fields_helper():
    book = {"asks": [{"price": "1", "size": "100"}, {"price": ".99", "size": "4"},
                     {"price": ".98", "size": "3"}], "age_ms": 12.0, "src": "ws"}
    fields = decision_book_fields(book, .99)
    assert fields == {"book_age_ms": 12.0, "book_src": "ws",
                      "decision_ask_depth": 7, "decision_best_ask": .98}
    assert decision_book_fields({}, .99) == {"book_age_ms": None, "book_src": "rest",
                                             "decision_ask_depth": 0, "decision_best_ask": None}


def test_side98_nofill_decision_fields():
    case = make_case()
    case.clock[0] = case.now = 1250.0
    case.price = case.past = 115.0
    case.latest = case.now * 1000
    case.grid = SimpleNamespace(
        price_at=lambda g: case.past if g == int(case.now * 10) - 305 else case.price,
        latest_exchange_ms=lambda: case.latest)
    case.strategy.runs.clear()
    case.strategy.sample_cash(case.now, 1000)
    case.quotes = {"cid": {"up": {"bid": .89, "bids": [], "asks": [[.98, 100]],
                                  "age_ms": 12.5, "src": "rest"},
                           "down": {"bid": .11, "bids": [], "asks": [[.12, 100]]}}}
    case.strategy.tick(helpers.cfg(fivemin_side98_enabled=True), case.now,
                       case.grid, case.quotes)
    nofill = events(case, "fivemin_side98_nofill")[0]
    assert nofill["book_age_ms"] == 12.5
    assert nofill["book_src"] == "rest"
    assert nofill["decision_ask_depth"] == 100
    assert nofill["decision_best_ask"] == .98
    assert join_refetch_threads(case.strategy) == []


def join_refetch_threads(strategy, timeout=5.0):
    for thread in strategy._refetch_threads:
        thread.join(timeout=timeout)
    return [thread for thread in strategy._refetch_threads if thread.is_alive()]


def events(case, name):
    return [kw for event, kw in case.logs if event == name]


def log_events(logs, name):
    return [kw for event, kw in logs if event == name]


def test_post_reject_refetch_is_fire_and_forget():
    case = make_case()
    gate = threading.Event()
    started = threading.Event()
    tokens = []

    def slow_fetch(token):
        tokens.append(token)
        started.set()
        gate.wait(5)
        return {"asks": [[.10, 4], [.99, 20]]}

    case.io.rest_book = slow_fetch
    main_tick(case)
    assert len(case.buys) == 1
    started.wait(2)
    assert tokens == ["dn"]
    assert events(case, "fivemin_post_reject_book") == []
    assert [thread.daemon for thread in case.strategy._refetch_threads] == [True]
    gate.set()
    assert join_refetch_threads(case.strategy) == []
    post = events(case, "fivemin_post_reject_book")[0]
    assert post["token"] == "dn"
    assert post["slug"] == case.strategy.state["markets"]["cid"]["slug"]
    assert post["condition_id"] == "cid"
    assert post["side"] == "down"
    assert post["limit"] == .99
    assert post["post_reject_best_ask"] == .10
    assert post["post_reject_depth"] == 24
    assert post["refetch_ms"] >= 0


def test_post_reject_refetch_one_per_reject_and_survives_cooldown():
    case = make_case()
    fetched = []

    def fetch(token):
        fetched.append(token)
        return {"asks": [[.20, 7]]}

    case.io.rest_book = fetch
    main_tick(case)
    main_tick(case, 1201.0)
    assert len(case.buys) == 1
    assert case.strategy.runs["cid"]["last_reason"] == "nofill_cooldown"
    assert join_refetch_threads(case.strategy) == []
    assert fetched == ["dn"]
    assert len(events(case, "fivemin_post_reject_book")) == 1


def test_post_reject_refetch_swallow_errors_and_missed_wiring():
    case = make_case()
    calls = []

    def failing_fetch(token):
        calls.append(token)
        raise RuntimeError("clob offline")

    case.io.rest_book = failing_fetch
    main_tick(case)
    assert join_refetch_threads(case.strategy) == []
    assert calls == ["dn"]
    assert events(case, "fivemin_post_reject_book") == []
    assert not any(event == "fivemin_post_reject_book" for event, _ in case.logs)
    plain = make_case()
    assert plain.io.rest_book is None
    main_tick(plain)
    assert plain.strategy._refetch_threads == []
    assert not any(event == "fivemin_post_reject_book" for event, _ in plain.logs)


def test_ws_connect_logs_disconnect_duration():
    clock = [0.0]
    logs = []
    state = {"healthy": False}
    ws = SimpleNamespace(healthy=lambda: state["healthy"], set_stale_s=lambda s: None,
                         usable=lambda token: None)
    rest = SimpleNamespace(poll=lambda *a: None)
    feed = WsFallbackBookFeed(ws, rest, clock=lambda: clock[0],
                              log=lambda event, **kw: logs.append((event, kw)))
    feed.poll(0.0, "up", "dn")
    clock[0] = 1.0
    feed.poll(1.0, "up", "dn")
    state["healthy"] = True
    clock[0] = 10.75
    feed.poll(10.75, "up", "dn")
    connects = log_events(logs, "fivemin_ws_connect")
    assert connects == [{"ws_down_ms": 10750.0}]
    state["healthy"] = False
    clock[0] = 12.0
    feed.poll(12.0, "up", "dn")
    state["healthy"] = True
    clock[0] = 45.25
    feed.poll(45.25, "up", "dn")
    connects = log_events(logs, "fivemin_ws_connect")
    assert len(connects) == 2
    assert connects[-1]["ws_down_ms"] == 33250.0
    disconnected = log_events(logs, "fivemin_ws_disconnect")
    assert disconnected == [{"ws_down_ms": 0.0}]             # second transition < 30s apart


def test_feed_stale_event_for_disconnected_ws():
    clock = [1000.0]
    logs = []
    rest_calls = []
    ws = ClobBookFeed(clock=lambda: clock[0])
    ws._healthy = False
    ws._last_rx = 995.0
    rest = SimpleNamespace(poll=lambda now, u, d, *a: rest_calls.append(clock[0]))
    feed = WsFallbackBookFeed(ws, rest, clock=lambda: clock[0], ws_stale_s=lambda: 20.0,
                              log=lambda event, **kw: logs.append((event, kw)))
    feed.poll(1000.0, "up", "dn")
    assert log_events(logs, "fivemin_ws_feed_stale") == [{"last_ws_msg_age_ms": 5000.0}]
    assert rest_calls == [1000.0]
    assert not any(event == "fivemin_book_quiet_token" for event, _ in logs)
    assert not any(event == "fivemin_ws_fallback" for event, _ in logs)
    clock[0] = 1004.0
    ws._healthy = True
    feed.poll(1004.0, "up", "dn")
    assert log_events(logs, "fivemin_ws_connect")[-1]["ws_down_ms"] == 4000.0
    assert len(log_events(logs, "fivemin_ws_feed_stale")) == 1


def test_quiet_token_event_when_feed_alive_but_token_silent():
    clock = [1000.0]
    logs = []
    rest_calls = []
    ws = ClobBookFeed(clock=lambda: clock[0])
    ws._healthy = True
    ws.set_tokens(["up", "dn"])
    ws.apply_raw(json.dumps({"event_type": "book", "asset_id": "dn",
                             "bids": [["0.90", "10"]], "asks": [["0.95", "5"]]}))
    rest = SimpleNamespace(poll=lambda now, u, d, *a: rest_calls.append(clock[0]))
    feed = WsFallbackBookFeed(ws, rest, clock=lambda: clock[0], ws_stale_s=lambda: 40.0,
                              log=lambda event, **kw: logs.append((event, kw)))
    feed.poll(1000.0, "up", "dn")
    quiet = log_events(logs, "fivemin_book_quiet_token")
    assert quiet == [{"token": "up", "last_token_msg_age_ms": None, "ws_book_empty": False}]
    assert rest_calls == [1000.0]
    assert not any(event == "fivemin_ws_fallback" for event, _ in logs)
    feed.poll(1000.4, "up", "dn")
    feed.poll(1000.8, "up", "dn")
    assert len(log_events(logs, "fivemin_book_quiet_token")) == 1
    assert len(rest_calls) == 3


def test_fresh_empty_ws_book_is_valid_empty_reading():
    clock = [1000.0]
    logs = []
    rest_calls = []
    ws = ClobBookFeed(clock=lambda: clock[0])
    ws._healthy = True
    ws.set_tokens(["up", "dn"])
    ws.set_min_size(0)
    ws.apply_raw(json.dumps({"event_type": "book", "asset_id": "dn",
                             "bids": [["0.90", "10"]], "asks": [["0.95", "5"]]}))
    ws.apply_raw(json.dumps({"event_type": "book", "asset_id": "up", "bids": [], "asks": []}))
    rest = SimpleNamespace(poll=lambda now, u, d, *a: rest_calls.append(clock[0]))
    feed = WsFallbackBookFeed(ws, rest, clock=lambda: clock[0], ws_stale_s=lambda: 1.0,
                              log=lambda event, **kw: logs.append((event, kw)))
    feed.poll(1000.0, "up", "dn")
    feed.poll(1000.9, "up", "dn")
    assert log_events(logs, "fivemin_ws_feed_stale") == []
    assert log_events(logs, "fivemin_book_quiet_token") == []
    assert not any(event == "fivemin_ws_fallback" for event, _ in logs)
    assert rest_calls == []                                  # no REST refetch
    assert feed.usable("up") is None
    assert feed.reason("up") == "empty"
    assert feed.usable("dn") is not None
    assert feed.reason("dn") is None
    clock[0] = 1001.3                                        # beyond ws_stale_s(1.0)
    feed.poll(1001.3, "up", "dn")
    assert rest_calls == [1001.3]                            # stale now falls back
    quiet = log_events(logs, "fivemin_book_quiet_token")
    assert quiet[0] == {"token": "up", "last_token_msg_age_ms": 1300.0,
                        "ws_book_empty": True}


def test_fresh_empty_ws_book_keeps_loser_empty_as_zero():
    case = make_case()
    case.strategy.runs["cid"] = {"up": 1198.0}
    case.strategy.sample_cash(1200, 1000)
    quotes = {"cid": {"up": {"bid": None, "miss": "empty"},
                      "down": {"bid": .90, "bids": [], "asks": [[.99, 100]]}}}
    case.strategy.tick(helpers.cfg(), 1200.0, GRID, quotes)
    assert case.buys == [("dn", 10.0, 0.99, False)]
    evaluation = [kw for event, kw in case.logs if event == "fivemin_eval"]
    assert evaluation[0]["reason"] == "signal_loser_empty_as_zero"
    assert evaluation[0]["bids"] == {"up": None, "down": 0.90}


def test_stale_empty_ws_book_falls_back_to_rest_with_quiet_event():
    clock = [1000.0]
    logs = []
    rest_calls = []
    ws = ClobBookFeed(clock=lambda: clock[0])
    ws._healthy = True
    ws.set_tokens(["up", "dn"])
    ws.set_min_size(0)
    ws.apply_raw(json.dumps({"event_type": "book", "asset_id": "up",
                             "bids": [["0.90", "10"]], "asks": [["0.95", "5"]]}))
    ws.apply_raw(json.dumps({"event_type": "book", "asset_id": "dn", "bids": [], "asks": []}))
    rest = SimpleNamespace(poll=lambda now, u, d, *a: rest_calls.append(clock[0]))
    feed = WsFallbackBookFeed(ws, rest, clock=lambda: clock[0], ws_stale_s=lambda: 1.0,
                              log=lambda event, **kw: logs.append((event, kw)))
    feed.poll(1000.0, "up", "dn")
    assert rest_calls == []                                  # dn fresh empty is valid
    assert log_events(logs, "fivemin_book_quiet_token") == []
    clock[0] = 1002.0                                        # dn book now stale
    ws.apply_raw(json.dumps({"event_type": "book", "asset_id": "up",
                             "bids": [["0.90", "10"]], "asks": [["0.95", "5"]]}))
    feed.poll(1002.0, "up", "dn")
    assert rest_calls == [1002.0]
    quiet = log_events(logs, "fivemin_book_quiet_token")
    assert quiet == [{"token": "dn", "last_token_msg_age_ms": 2000.0, "ws_book_empty": True}]


def test_token_health_missing_book_and_price_change_refresh():
    clock = [2000.0]
    ws = ClobBookFeed(clock=lambda: clock[0])
    assert ws.token_health("ghost") == {"ws_book_empty": False, "last_ws_msg_age_ms": None}
    ws.set_tokens(["live"])
    ws.apply_raw(json.dumps({"event_type": "price_change",
                             "price_changes": [{"asset_id": "live", "side": "BUY",
                                                "price": "0.10", "size": "3"}]}))
    health = ws.token_health("live")
    assert health["ws_book_empty"] is False
    assert health["last_ws_msg_age_ms"] == 0.0


def test_rest_book_source_is_rest():
    clock = [1000.0]
    books = RestBookFeed(lambda u, d: (BOOK, BOOK), clock=lambda: clock[0])
    assert books.source("up") == "rest"


def test_runner_wires_rest_book_and_stamps_feed_source_age():
    clock = [1400.0]
    io = FiveMinIO(buy=lambda *a: {}, sell=lambda *a: {}, reserves=lambda c: 0,
                   redeem=lambda *a: None, save=lambda s: None)
    strategy = Strategy(io, startup_ts=0)
    strategy.add_market(helpers.market(cid="cid", start=1200), now=1200)
    books = RestBookFeed(lambda u, d, fail=None: (BOOK, BOOK), clock=lambda: clock[0])
    rtds = SimpleNamespace(start=lambda: None, stop=lambda: None, drain=lambda: [],
                           reconnect=lambda reason: None)
    runner = Runner(strategy, lambda: helpers.cfg(pUSD_address="cash"), SimpleNamespace(),
                    SimpleNamespace(), "", None, None, rtds, books, clock=lambda: clock[0])
    assert io.rest_book is not None
    assert runner._rest_refetch_book("up") == {"bids": BOOK[2], "asks": BOOK[5]}
    assert runner._rest_refetch_book("ghost") is None
    seen = []
    strategy.tick = lambda cfg, now, grid, quotes: seen.append(quotes)
    runner.tick()
    up = seen[0]["cid"]["up"]
    assert up["src"] == "rest"
    assert up["age_ms"] == 30.0
    assert up["bid"] == .10


def test_runner_rest_refetch_ignores_failed_pairs():
    clock = [1400.0]
    strategy = Strategy(FiveMinIO(buy=lambda *a: {}, sell=lambda *a: {},
                                  reserves=lambda c: 0, redeem=lambda *a: None,
                                  save=lambda s: None), startup_ts=0)
    strategy.add_market(helpers.market(cid="cid", start=1200), now=1200)

    def failing(u, d, fail=None):
        if fail is not None:
            fail["failed"] = True
        return (BOOK, BOOK)

    books = RestBookFeed(lambda u, d, fail=None: (BOOK, BOOK), clock=lambda: clock[0])
    rest_ws = SimpleNamespace(rest=books)
    rtds = SimpleNamespace(start=lambda: None, stop=lambda: None, drain=lambda: [],
                           reconnect=lambda reason: None)
    runner = Runner(strategy, lambda: helpers.cfg(pUSD_address="cash"), SimpleNamespace(),
                    SimpleNamespace(), "", None, None, rtds, rest_ws, clock=lambda: clock[0])
    assert runner._rest_refetch_book("up") == {"bids": BOOK[2], "asks": BOOK[5]}
    books.fetch_pair = failing
    assert runner._rest_refetch_book("up") is None
    assert runner._rest_refetch_book("dn") is None
