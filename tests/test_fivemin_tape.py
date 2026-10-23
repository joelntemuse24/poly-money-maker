import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_fivemin as fivemin_helpers
from buy.fivemin_tape import BookTape

ROOT = Path(__file__).resolve().parents[1]


def cfg(**extra):
    value = dict(fivemin_helpers.DEFAULTS)
    value.update(extra)
    return value


class FakeGrid:
    def __init__(self, fail=False, twap=100.0, prices=None):
        self.fail = fail
        self.twap = twap
        self.prices = {9995: 101.0, 9695: 100.0, 9895: 100.5} if prices is None else prices

    def price_at(self, g):
        if self.fail:
            raise RuntimeError("boom")
        return self.prices.get(g, 100.0)

    def twap60(self, g):
        if self.fail:
            raise RuntimeError("boom")
        return self.twap


def row(end=1120.0, cid="cid", slug="slug-1", strike=100.0, offset=0.5):
    return {"condition_id": cid, "slug": slug, "end": end, "strike": strike, "offset": offset}


def book(bids=None, asks=None):
    return {"bids": [] if bids is None else bids, "asks": [] if asks is None else asks}


def quotes(up=None, down=None):
    out = {}
    if up is not None:
        out["up"] = up
    if down is not None:
        out["down"] = down
    return {"cid": out}


def read_lines(root):
    out = []
    for path in sorted(Path(root).glob("*.jsonl")):
        out.extend(json.loads(line) for line in path.read_text().splitlines() if line)
    return out


def flushed(tape, root):
    """Flush and read back everything written so far."""
    tape.close()
    return read_lines(root)


def test_sample_fields_and_top5_truncation(tmp_path):
    tape = BookTape(tmp_path)
    up_bids = [[0.50, 10], (0.51, 5.0), {"price": 0.52, "size": 7}, [0.53, 3],
               [0.54, 2], [0.55, 1]]
    up_asks = [[0.60, 10], [0.61, 5], [0.62, 7], [0.63, 3], [0.58, 2], [0.59, 1]]
    tape.sample(cfg(), 1000.0, [row()],
                quotes(up=book(up_bids, up_asks), down=book()), FakeGrid(), None)

    written = flushed(tape, tmp_path)
    assert len(written) == 1
    line = written[0]
    assert line["t"] == "s"
    assert line["ts"] == 1000.0
    assert line["slug"] == "slug-1"
    assert line["ttm"] == 120.0
    assert line["strike"] == 100.0
    assert line["spot"] == 101.0
    assert line["proxy"] == 100.5
    assert line["fav"] == "up"
    assert line["gate_ratio"] == pytest.approx(0.5 / 120.0)
    assert line["d30"] == pytest.approx(1.0)
    assert line["d10"] == pytest.approx(0.5)
    assert line["up"]["b"] == [[0.55, 1.0], [0.54, 2.0], [0.53, 3.0], [0.52, 7.0], [0.51, 5.0]]
    assert line["up"]["a"] == [[0.58, 2.0], [0.59, 1.0], [0.60, 10.0], [0.61, 5.0], [0.62, 7.0]]
    assert line["dn"] == {"b": [], "a": []}


def test_rate_limit_two_changed_samples_throttle(tmp_path):
    tape = BookTape(tmp_path)
    grid = FakeGrid(prices={})
    tape.sample(cfg(), 1000.0, [row()], quotes(up=book([[0.5, 1]])), grid, None)
    tape.sample(cfg(), 1000.1, [row()], quotes(up=book([[0.4, 1]])), grid, None)
    assert len(flushed(tape, tmp_path)) == 1
    tape.sample(cfg(), 1000.25, [row()], quotes(up=book([[0.4, 1]])), grid, None)
    assert len(flushed(tape, tmp_path)) == 2


def test_unchanged_book_writes_nothing_new(tmp_path):
    tape = BookTape(tmp_path)
    grid = FakeGrid(prices={})
    tape.sample(cfg(), 1000.0, [row()], quotes(up=book([[0.5, 1]])), grid, None)
    tape.sample(cfg(), 1010.0, [row()], quotes(up=book([[0.5, 1]])), grid, None)
    assert len(flushed(tape, tmp_path)) == 1


def test_outside_window_records_nothing(tmp_path):
    tape = BookTape(tmp_path)
    tape.sample(cfg(), 1000.0, [row(end=1200.0)], quotes(up=book([[0.5, 1]])), FakeGrid(), None)
    assert not list(Path(tmp_path).glob("*.jsonl"))


def test_flag_off_writes_no_file_and_no_events(tmp_path):
    tape = BookTape(tmp_path)
    off = cfg(fivemin_book_record=False)
    tape.sample(off, 1000.0, [row()], quotes(up=book([[0.5, 1]])), FakeGrid(), None)
    tape.event("fivemin_fill", {"slug": "slug-1", "ts": 1001.0, "price": 0.99})
    assert not list(Path(tmp_path).glob("*.jsonl"))


def test_failing_grid_does_not_raise_and_logs_once(tmp_path):
    logs = []
    tape = BookTape(tmp_path, log=lambda event, **fields: logs.append((event, fields)))
    grid = FakeGrid(fail=True)
    tape.sample(cfg(), 1000.0, [row()], quotes(up=book([[0.5, 1]])), grid, None)
    tape.sample(cfg(), 1000.3, [row()], quotes(up=book([[0.5, 1]])), grid, None)
    errors = [fields for event, fields in logs if event == "fivemin_book_tape_error"]
    assert len(errors) == 1
    assert "boom" in errors[0]["error"]


def test_event_lines_and_end_record(tmp_path):
    tape = BookTape(tmp_path)
    grid = FakeGrid()
    tape.sample(cfg(), 1000.0, [row()], quotes(up=book([[0.5, 1]])), grid, None)
    tape.event("fivemin_fill", {
        "slug": "slug-1", "ts": 1001.0, "side": "up", "price": 0.99, "shares": 10,
        "reason": "signal", "adverse_30s_usd": 1.5, "headroom": None,
        "status": "matched", "books": {"up": {}},
    })
    tape.sample(cfg(), 1130.0, [], {}, grid, None)

    written_lines = read_lines(tmp_path)
    events = [line for line in written_lines if line["t"] == "e"]
    assert len(events) == 1
    assert events[0] == {"t": "e", "ev": "fivemin_fill", "ts": 1001.0, "slug": "slug-1",
                         "side": "up", "price": 0.99, "reason": "signal",
                         "adverse_30s_usd": 1.5, "shares": 10}
    end = next(line for line in written_lines if line["t"] == "end")
    assert end["slug"] == "slug-1"
    assert [event["ev"] for event in end["events"]] == ["fivemin_fill"]
    assert end["events"][0]["price"] == 0.99


def test_prune_removes_only_old_files(tmp_path):
    now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc).timestamp()
    today = datetime.fromtimestamp(now, timezone.utc).date()
    old = Path(tmp_path) / f"{(today - timedelta(days=20)).isoformat()}.jsonl"
    recent = Path(tmp_path) / f"{(today - timedelta(days=3)).isoformat()}.jsonl"
    old.write_text("{}\n")
    recent.write_text("{}\n")
    BookTape(tmp_path).prune(now)
    assert not old.exists()
    assert recent.exists()


def _validate():
    tree = ast.parse((ROOT / "mintbot.py").read_text())
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
    return namespace["validate_strategy"]


def test_validate_strategy_book_record_flag_and_window():
    validate = _validate()
    base = fivemin_helpers.cfg()
    validate(base)
    validate({**base, "fivemin_book_record": False})
    validate({**base, "fivemin_book_record_window_s": 1})
    validate({**base, "fivemin_book_record_window_s": 300})
    for value in (0, 1, "true", None):
        with pytest.raises(ValueError, match="fivemin_book_record must be boolean"):
            validate({**base, "fivemin_book_record": value})
    for value in (0, 301):
        with pytest.raises(ValueError, match="invalid fivemin_book_record_window_s"):
            validate({**base, "fivemin_book_record_window_s": value})


def test_tape_defaults_and_example_are_present():
    assert fivemin_helpers.DEFAULTS["fivemin_book_record"] is True
    assert fivemin_helpers.DEFAULTS["fivemin_book_record_window_s"] == 150
    tree = ast.parse((ROOT / "mintbot.py").read_text())
    defaults_node = next(
        node.value for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "DEFAULTS" for target in node.targets)
    )
    mint_defaults = {ast.literal_eval(key): ast.literal_eval(value)
                     for key, value in zip(defaults_node.keys, defaults_node.values)}
    assert mint_defaults["fivemin_book_record"] is True
    assert mint_defaults["fivemin_book_record_window_s"] == 150
    example = json.loads((ROOT / "strategy_mint.example.json").read_text())
    assert example["fivemin_book_record"] is True
    assert example["fivemin_book_record_window_s"] == 150


class RaisingTape:
    def __init__(self):
        self.events = []

    def sample(self, *args, **kwargs):
        raise RuntimeError("boom")

    def prune(self, now):
        raise RuntimeError("boom")

    def event(self, name, fields):
        self.events.append(name)

    def close(self):
        pass


def make_runner(clock, tape):
    from buy.fivemin import FiveMinIO, Runner, Strategy
    from buy.binance_trades import PriceGrid

    io = FiveMinIO(buy=lambda *a: {}, sell=lambda *a: {}, reserves=lambda cfg: 0,
                   redeem=lambda *a: None, save=lambda *a: None,
                   log=lambda event, **fields: None)
    strategy = Strategy(io, startup_ts=0)
    strategy.state["markets"]["cid"] = {
        "condition_id": "cid", "slug": "btc-updown-5m-1000", "start": 1000, "end": 1300,
        "up_token": "up", "dn_token": "dn", "entered": False, "position": None,
    }
    strategy._active.add("cid")
    no_feed = SimpleNamespace(start=lambda: None, stop=lambda: None, drain=lambda: [],
                              reconnect=lambda reason: None)
    books = SimpleNamespace(set_tokens=lambda tokens: None, set_stale_s=lambda _: None,
                            set_min_size=lambda _: None, usable=lambda token: None,
                            start=lambda: None, stop=lambda: None)
    return Runner(strategy, lambda: cfg(), None, None, "", PriceGrid(), no_feed, no_feed,
                  books, clock=lambda: clock[0], tape=tape)


def test_runner_tick_survives_tape_failure_and_forwards_record_events():
    tape = RaisingTape()
    runner = make_runner([1200.0], tape)
    runner.tick()
    runner.strategy.io.log("fivemin_signal", slug="btc-updown-5m-1000")
    runner.strategy.io.log("some_unrecorded_event", slug="btc-updown-5m-1000")
    assert tape.events == ["fivemin_signal"]
    runner.stop()
