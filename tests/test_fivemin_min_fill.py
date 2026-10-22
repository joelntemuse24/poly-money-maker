"""Pre-send fill floor and low-fill exit for the isolated 5m policy."""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from buy.fivemin import DEFAULTS, FiveMinIO, Strategy


def cfg(**extra):
    value = dict(DEFAULTS)
    value.update({"fivemin_enabled": True, "dry_run": False, "redeem_enabled": True})
    value.update(extra)
    return value


def market(cid="cid", start=1000):
    return SimpleNamespace(condition_id=cid, slug=f"btc-up-or-down-5m-{start}",
                           start_ts=start, end_ts=start + 300, up_token="up", dn_token="dn",
                           active=True, closed=False, neg_risk=False)


def quotes(up=0.05, down=0.90, up_asks=(), down_asks=()):
    return {"cid": {"up": {"bid": up, "asks": list(up_asks), "bids": []},
                    "down": {"bid": down, "asks": list(down_asks), "bids": []}}}


@pytest.fixture
def bot():
    logs, saved, buys, sells, alerts = [], [], [], [], []
    fills = {"avg": 0.99}

    def buy(token, shares, limit, dry):
        buys.append((token, shares, limit, dry))
        return {"shares": shares, "cost": shares * fills["avg"],
                "price": fills["avg"], "status": "matched"}

    def sell(row, pos, book, cfg_, now):
        sells.append((row["condition_id"], dict(pos), dict(book)))
        return {"shares": pos["shares"], "proceeds": pos["shares"] * book["bid"],
                "price": book["bid"], "status": "matched"}

    io = FiveMinIO(
        buy=buy, sell=sell, reserves=lambda cfg_: 0,
        redeem=lambda row, now: logs.append(("redeem", row["condition_id"])),
        save=lambda state: saved.append(json.loads(json.dumps(state))),
        log=lambda event, **kw: logs.append((event, kw)),
        alert=lambda text, **kw: alerts.append(text), durable=lambda: True,
    )
    strategy = Strategy(io, startup_ts=0)
    strategy.add_market(market(), now=1000)
    row = strategy.state["markets"]["cid"]
    row.update(strike=100.0, offset=0.0)
    strategy.sample_cash(1200, 1000)
    strategy.runs["cid"] = {"up": 1200.0}
    case = SimpleNamespace(strategy=strategy, io=io, row=row, now=1203.0,
                           grid=SimpleNamespace(price_at=lambda _: 100.0,
                                                latest_exchange_ms=lambda: 1e15),
                           books=quotes(), logs=logs, saved=saved, buys=buys,
                           sells=sells, alerts=alerts, fills=fills)
    return case


@pytest.fixture
def side98_bot(bot):
    bot.now = 1250.0
    bot.price = 115.0
    bot.past = 115.0
    bot.grid = SimpleNamespace(
        price_at=lambda g: bot.past if g == math.floor(bot.now * 10) - 305 else bot.price,
        latest_exchange_ms=lambda: bot.now * 1000,
    )
    bot.books = {"cid": {"up": {"bid": .89, "asks": [[.98, 100]], "bids": []},
                         "down": {"bid": .11, "asks": [[.12, 100]], "bids": []}}}
    bot.strategy.sample_cash(bot.now, 1000)
    return bot


def tick(bot, **extra):
    bot.strategy.tick(cfg(fivemin_gate_k=0, **extra), bot.now, bot.grid, bot.books)


def skips(bot, event="fivemin_min_fill_skip"):
    return [fields for name, fields in bot.logs if name == event]


def test_main_best_ask_below_floor_skips_without_consuming_attempt(bot):
    bot.books = quotes(down_asks=[[.94, 5]])
    tick(bot)
    bot.now = 1204.0
    tick(bot)
    assert not bot.buys
    assert not bot.row.get("entered")
    assert not bot.row.get("main_attempts")
    logged = skips(bot)
    assert len(logged) == 1
    assert logged[0]["kind"] == "main"
    assert logged[0]["side"] == "down"
    assert logged[0]["best_ask"] == .94
    assert logged[0]["floor"] == .95
    assert logged[0]["depth"] == 5
    assert logged[0]["slug"] == bot.row["slug"]


@pytest.mark.parametrize("best", [.95, .96, .99])
def test_main_best_ask_at_or_above_floor_enters(bot, best):
    bot.books = quotes(down_asks=[[best, 5]])
    tick(bot)
    assert bot.buys == [("dn", 10.0, .99, False)]
    assert bot.row["entered"]
    assert bot.row["position"]["shares"] == 10
    assert not skips(bot)


def test_later_tick_enters_after_earlier_skip(bot):
    bot.books = quotes(down_asks=[[.94, 5]])
    tick(bot)
    assert not bot.buys
    bot.now = 1204.0
    bot.books["cid"]["down"]["asks"] = [[.96, 5]]
    tick(bot)
    assert bot.buys == [("dn", 10.0, .99, False)]
    assert bot.row["entered"]
    assert len(skips(bot)) == 1


def test_side98_below_floor_skipped_without_attempt(side98_bot):
    bot = side98_bot
    bot.strategy.tick(cfg(fivemin_side98_enabled=True, fivemin_min_fill_px=.99),
                      bot.now, bot.grid, bot.books)
    bot.now += .2
    bot.strategy.tick(cfg(fivemin_side98_enabled=True, fivemin_min_fill_px=.99),
                      bot.now, bot.grid, bot.books)
    assert not bot.buys
    assert not bot.row.get("side98")
    logged = skips(bot)
    assert len(logged) == 1
    assert logged[0]["kind"] == "side98"
    assert logged[0]["side"] == "up"
    assert logged[0]["best_ask"] == .98
    assert logged[0]["floor"] == .99


def test_zero_floor_disables_the_check(bot):
    bot.books = quotes(down_asks=[[.50, 5]])
    tick(bot, fivemin_min_fill_px=0)
    assert bot.buys == [("dn", 10.0, .99, False)]
    assert not skips(bot)


def test_low_avg_fill_sells_full_position_once(bot):
    bot.fills["avg"] = .90
    bot.books = quotes(down_asks=[[.95, 5]])
    tick(bot)
    assert bot.buys == [("dn", 10.0, .99, False)]
    assert len(bot.sells) == 1
    cid, sold_position, book = bot.sells[0]
    assert cid == "cid"
    assert sold_position["shares"] == 10
    assert sold_position["side"] == "down"
    assert book["bid"] == .90
    position = bot.row["position"]
    assert position["dumped"] is True
    assert position["low_fill_exit"] is True
    assert position["shares"] == 0
    assert position["proceeds"] == pytest.approx(9.0)
    logged = [fields for name, fields in bot.logs if name == "fivemin_low_fill_exit"]
    assert len(logged) == 1
    assert logged[0]["kind"] == "main"
    assert logged[0]["avg_fill"] == pytest.approx(.90)
    assert logged[0]["floor"] == pytest.approx(.95)
    assert logged[0]["shares"] == pytest.approx(10)
    assert logged[0]["proceeds"] == pytest.approx(9.0)
    assert logged[0]["pnl"] == pytest.approx(0.0)
    assert logged[0]["status"] == "matched"
    assert "LOW FILL EXIT" in bot.alerts[-1]
    assert bot.strategy.state["realized"] == 0


def test_fill_avg_at_floor_does_not_sell(bot):
    bot.fills["avg"] = .95
    bot.books = quotes(down_asks=[[.95, 5]])
    tick(bot)
    assert bot.buys
    assert not bot.sells
    assert bot.row["position"]["dumped"] is False
    assert not skips(bot, "fivemin_low_fill_exit")


def test_low_fill_exit_flag_off_keeps_position(bot):
    bot.fills["avg"] = .80
    bot.books = quotes(down_asks=[[.95, 5]])
    tick(bot, fivemin_low_fill_exit=False)
    assert bot.buys
    assert not bot.sells
    assert bot.row["position"]["shares"] == 10
    assert bot.row["position"]["dumped"] is False
    assert bot.row["position"].get("low_fill_exit") is None


def test_low_fill_exit_blocks_later_dump_from_selling_again(bot):
    bot.fills["avg"] = .90
    bot.books = quotes(down_asks=[[.95, 5]])
    tick(bot)
    assert len(bot.sells) == 1
    bot.books = quotes(up=.80, down=.19)
    bot.now = 1204.0
    tick(bot)
    bot.now = 1207.5
    tick(bot)
    assert len(bot.sells) == 1
    assert not any(name == "fivemin_dump" for name, _ in bot.logs)
    assert bot.row["position"]["shares"] == 0
