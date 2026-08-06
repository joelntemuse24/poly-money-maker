from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import List

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(REPO_ROOT, "strategy.buy.json")
DATA_DIR = os.environ.get("BUY_DATA_DIR") or os.path.join(REPO_ROOT, "buy_data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "polybuy.log")
LOCK_FILE = os.path.join(DATA_DIR, "polybuy.lock")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat.json")
STOP_FILE = os.path.join(DATA_DIR, "STOP")
ARM_FILE = os.path.join(DATA_DIR, "ARM")
ARM_PHRASE = "MINT_REAL_PUSD"


@dataclass(frozen=True)
class BuyConfig:
    enabled: bool = False
    dry_run: bool = True
    entry_method: str = "mint"
    series_slugs: str = "btc-up-or-down-15m"
    shares: float = 5.0
    enter_min_ttm_min: float = 0.0
    enter_max_ttm_min: float = 60.0
    max_set_cost: float = 1.0
    max_open_sets: int = 1
    max_open_notional: float = 5.0
    max_daily_notional: float = 10.0
    one_entry_per_market: bool = True
    poll_s: float = 15.0
    max_state_intents: int = 500
    max_dry_plans: int = 200
    min_free_disk_mb: float = 500.0
    require_funder_match: bool = True
    arm_max_age_s: float = 900.0
    position_tolerance: float = 0.001
    rpc_url: str = "https://polygon.drpc.org"
    gamma_url: str = "https://gamma-api.polymarket.com"
    data_api_url: str = "https://data-api.polymarket.com"
    relayer_url: str = "https://relayer-v2.polymarket.com"
    chain_id: int = 137
    pUSD_address: str = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    ctf_address: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    standard_adapter_address: str = "0xAdA100Db00Ca00073811820692005400218FcE1f"

    def series_slug_list(self) -> List[str]:
        return [part.strip() for part in self.series_slugs.split(",") if part.strip()]


def _coerce(current, value):
    if isinstance(current, bool):
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")
    return type(current)(value)


def load_config(path: str | None = None) -> BuyConfig:
    path = path or CONFIG_FILE
    values = asdict(BuyConfig())
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
        raw = raw.get("buy", raw)
        for key, value in raw.items():
            if key in values:
                values[key] = _coerce(values[key], value)
    config = BuyConfig(**values)
    validate_config(config)
    return config


def validate_config(config: BuyConfig) -> None:
    if config.entry_method != "mint":
        raise ValueError("entry_method must be mint")
    if not config.require_funder_match:
        raise ValueError("require_funder_match must remain true")
    if not config.series_slug_list():
        raise ValueError("series_slugs must not be empty")
    if config.shares <= 0 or round(config.shares, 6) != config.shares:
        raise ValueError("shares must be positive with at most six decimals")
    if config.max_set_cost < 1.0:
        raise ValueError("max_set_cost below 1.0 blocks deterministic mint entry")
    if config.enter_min_ttm_min < 0 or config.enter_max_ttm_min <= config.enter_min_ttm_min:
        raise ValueError("invalid entry TTM window")
    if config.max_open_sets < 1:
        raise ValueError("max_open_sets must be at least one")
    if config.max_open_notional < config.shares:
        raise ValueError("max_open_notional must cover one mint")
    if config.max_daily_notional < config.shares:
        raise ValueError("max_daily_notional must cover one mint")
    if config.poll_s < 5:
        raise ValueError("poll_s must be at least five seconds")
    if config.max_state_intents < 1 or config.max_dry_plans < 1:
        raise ValueError("state caps must be positive")
