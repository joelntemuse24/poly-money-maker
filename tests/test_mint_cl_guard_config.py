"""load_strategy/hot reload must keep the sniper Chainlink guard keys.

These four keys are defined in buy/fivemin.py DEFAULTS but were missing from
mintbot.DEFAULTS, so load_strategy() dropped them from strategy_mint.json.
"""
import json

import pytest

import mintbot
from buy import fivemin

CL_KEYS = ("sniper_cl_guard", "sniper_cl_guard_mode",
           "sniper_cl_min_gap_usd", "sniper_cl_max_age_s")


def _strategy(**overrides) -> dict:
    raw = json.loads((mintbot.REPO / "strategy_mint.example.json").read_text())
    raw.update(overrides)
    return raw


def _write(path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("key", CL_KEYS)
def test_defaults_define_cl_guard_keys(key):
    assert key in mintbot.DEFAULTS


def test_load_strategy_keeps_cl_guard_keys(tmp_path, monkeypatch):
    path = tmp_path / "strategy_mint.json"
    _write(path, _strategy(sniper_cl_guard=True,
                           sniper_cl_guard_mode="projected",
                           sniper_cl_min_gap_usd=12.5,
                           sniper_cl_max_age_s=2.0,
                           sniper_gap_estimator="e3",
                           sniper_e3_lag_s=0.75))
    monkeypatch.setattr(mintbot, "STRATEGY_FILE", path)

    cfg = mintbot.load_strategy()
    assert cfg["sniper_cl_guard"] is True
    assert cfg["sniper_cl_guard_mode"] == "projected"
    assert cfg["sniper_cl_min_gap_usd"] == 12.5
    assert cfg["sniper_cl_max_age_s"] == 2.0
    assert cfg["sniper_gap_estimator"] == "e3"
    assert cfg["sniper_e3_lag_s"] == 0.75

    # fivemin.settings() is the merge the sniper sees at runtime.
    merged = fivemin.settings(cfg)
    assert merged["sniper_cl_guard"] is True
    assert merged["sniper_cl_guard_mode"] == "projected"
    assert merged["sniper_cl_min_gap_usd"] == 12.5
    assert merged["sniper_cl_max_age_s"] == 2.0


def test_load_strategy_cl_guard_defaults_when_absent(tmp_path, monkeypatch):
    path = tmp_path / "strategy_mint.json"
    raw = _strategy()
    for key in CL_KEYS:
        raw.pop(key, None)
    _write(path, raw)
    monkeypatch.setattr(mintbot, "STRATEGY_FILE", path)

    cfg = mintbot.load_strategy()
    assert cfg["sniper_cl_guard"] is False
    assert cfg["sniper_cl_guard_mode"] == "raw"
    assert cfg["sniper_cl_min_gap_usd"] == 0.0
    assert cfg["sniper_cl_max_age_s"] == 5.0
    assert fivemin.settings(cfg)["sniper_cl_guard_mode"] == "raw"


@pytest.mark.parametrize("overrides", [
    {"sniper_cl_guard": "true"},
    {"sniper_cl_guard": 1},
    {"sniper_cl_guard_mode": "live"},
    {"sniper_cl_guard_mode": None},
    {"sniper_cl_min_gap_usd": "5"},
    {"sniper_cl_min_gap_usd": True},
    {"sniper_cl_min_gap_usd": float("nan")},
    {"sniper_cl_max_age_s": "5"},
    {"sniper_cl_max_age_s": False},
    {"sniper_cl_max_age_s": float("inf")},
    {"sniper_cl_max_age_s": 0},
    {"sniper_cl_max_age_s": -1.0},
])
def test_load_strategy_rejects_invalid_cl_guard(tmp_path, monkeypatch, overrides):
    path = tmp_path / "strategy_mint.json"
    _write(path, _strategy(**overrides))
    monkeypatch.setattr(mintbot, "STRATEGY_FILE", path)

    with pytest.raises(ValueError):
        mintbot.load_strategy()


def test_hot_reload_picks_up_cl_guard(tmp_path, monkeypatch):
    path = tmp_path / "strategy_mint.json"
    _write(path, _strategy(sniper_cl_guard=False))
    monkeypatch.setattr(mintbot, "STRATEGY_FILE", path)

    cfg_box = {}
    first = mintbot._reload_cfg(cfg_box)
    assert first["sniper_cl_guard"] is False

    # Change size and mtime so the stamp check sees a new file.
    _write(path, _strategy(sniper_cl_guard=True,
                           sniper_cl_guard_mode="projected",
                           sniper_cl_max_age_s=1.5))
    reloaded = mintbot._reload_cfg(cfg_box)
    assert reloaded["sniper_cl_guard"] is True
    assert reloaded["sniper_cl_guard_mode"] == "projected"
    assert reloaded["sniper_cl_max_age_s"] == 1.5
    assert cfg_box["cfg"]["sniper_cl_guard"] is True
    assert fivemin.settings(cfg_box["cfg"])["sniper_cl_guard"] is True
