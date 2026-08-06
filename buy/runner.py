from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Callable, Dict, Optional

from dotenv import load_dotenv
from eth_utils import to_checksum_address

from .chain import ChainReader
from .config import (
    ARM_FILE,
    ARM_PHRASE,
    DATA_DIR,
    LOG_FILE,
    STOP_FILE,
    BuyConfig,
    load_config,
)
from .contracts import build_atomic_mint_calls
from .market import MarketGateway, MintMarket
from .relayer import MintRelayer, RelayerStatusGateway
from .store import (
    free_disk_mb,
    load_state,
    save_state,
    single_instance_lock,
    trim_state,
    write_heartbeat,
)

ACTIVE_INTENT_STATES = {
    "submitting",
    "ambiguous",
    "pending",
    "executed",
    "mined",
    "confirmed_waiting_inventory",
    "confirmed",
}
TERMINAL_FAILURE_STATES = {"STATE_FAILED": "failed", "STATE_INVALID": "invalid"}
_shutdown = False


def _signal_handler(signum, frame):
    global _shutdown
    _shutdown = True


def setup_logging() -> logging.Logger:
    os.makedirs(DATA_DIR, exist_ok=True)
    logger = logging.getLogger("polybuy")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def notify(title: str, message: str, priority: str = "default") -> None:
    topic = os.getenv("NTFY_TOPIC_BUY") or os.getenv("NTFY_TOPIC")
    if not topic:
        return
    try:
        import requests

        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=5,
        )
    except Exception:
        return


def is_fresh_arm(config: BuyConfig, now: float) -> bool:
    try:
        if now - os.path.getmtime(ARM_FILE) > config.arm_max_age_s:
            return False
        with open(ARM_FILE, "r", encoding="utf-8") as handle:
            return handle.read().strip() == ARM_PHRASE
    except OSError:
        return False


def consume_arm() -> None:
    try:
        os.remove(ARM_FILE)
    except FileNotFoundError:
        return


def restore_arm() -> None:
    try:
        with open(ARM_FILE, "w", encoding="utf-8") as handle:
            handle.write(ARM_PHRASE)
    except OSError:
        pass


def eligible_markets(markets: list[MintMarket], config: BuyConfig, now: float) -> list[MintMarket]:
    eligible = []
    for market in markets:
        mts = market.minutes_to_start(now)
        if not config.enter_min_ttm_min <= mts <= config.enter_max_ttm_min:
            continue
        if not market.active or market.closed or market.neg_risk:
            continue
        eligible.append(market)
    return sorted(eligible, key=lambda market: market.start_ts)


def _today_start(now: float) -> float:
    current = datetime.fromtimestamp(now, tz=timezone.utc)
    return datetime(current.year, current.month, current.day, tzinfo=timezone.utc).timestamp()


def _portfolio_usage(
    markets: list[MintMarket],
    positions: Dict[str, float],
    state: dict,
    tolerance: float,
) -> tuple[int, float, set[str]]:
    owned = set()
    notional = 0.0
    by_condition = {market.condition_id: market for market in markets}
    for condition_id, market in by_condition.items():
        up = float(positions.get(market.up_token, 0))
        down = float(positions.get(market.dn_token, 0))
        if max(up, down) > tolerance:
            owned.add(condition_id)
            notional += max(up, down)
    active_intents = {
        condition_id
        for condition_id, intent in state.get("intents", {}).items()
        if intent.get("status") in ACTIVE_INTENT_STATES
    }
    pending_only = active_intents.difference(owned)
    for condition_id in pending_only:
        intent = state["intents"][condition_id]
        notional += float(intent.get("shares") or 0)
    return len(owned.union(active_intents)), notional, owned


def _daily_notional(state: dict, now: float) -> float:
    day_start = _today_start(now)
    return sum(
        float(intent.get("shares") or 0)
        for intent in state.get("intents", {}).values()
        if intent.get("status") not in ("failed", "invalid")
        and float(intent.get("submitted_at") or intent.get("created_at") or 0) >= day_start
    )


def reconcile_intents(
    *,
    config: BuyConfig,
    state: dict,
    funder_address: Optional[str],
    chain: ChainReader,
    status_gateway: RelayerStatusGateway,
    logger: logging.Logger,
    now: float,
) -> None:
    for intent in state.get("intents", {}).values():
        status = intent.get("status")
        if status == "submitting" and not intent.get("transaction_id"):
            intent["status"] = "ambiguous"
            intent["updated_at"] = now
            logger.error("ambiguous mint intent %s blocks new entry", intent.get("slug"))
            continue
        transaction_id = intent.get("transaction_id")
        if status in ("pending", "executed", "mined") and transaction_id:
            transaction = status_gateway.transaction(str(transaction_id))
            if transaction:
                relayer_state = str(transaction.get("state") or "")
                intent["relayer_state"] = relayer_state
                intent["transaction_hash"] = transaction.get("transactionHash")
                intent["updated_at"] = now
                if relayer_state in TERMINAL_FAILURE_STATES:
                    intent["status"] = TERMINAL_FAILURE_STATES[relayer_state]
                    notify("Polybuy mint failed", f"{intent.get('slug')} · {relayer_state}", "urgent")
                elif relayer_state == "STATE_CONFIRMED":
                    intent["status"] = "confirmed_waiting_inventory"
                elif relayer_state == "STATE_MINED":
                    intent["status"] = "mined"
                elif relayer_state == "STATE_EXECUTED":
                    intent["status"] = "executed"
        if intent.get("status") in ("ambiguous", "confirmed_waiting_inventory", "confirmed") and funder_address:
            up = chain.position_balance(config.ctf_address, funder_address, intent["up_token"])
            down = chain.position_balance(config.ctf_address, funder_address, intent["dn_token"])
            intent["observed_up"] = up
            intent["observed_dn"] = down
            intent["updated_at"] = now
            expected_up = float(intent.get("before_up") or 0) + float(intent["shares"])
            expected_dn = float(intent.get("before_dn") or 0) + float(intent["shares"])
            if up + config.position_tolerance >= expected_up and down + config.position_tolerance >= expected_dn:
                if intent.get("status") != "confirmed":
                    logger.info("MINT CONFIRMED %s up=%.6f down=%.6f", intent.get("slug"), up, down)
                    notify("Polybuy mint confirmed", f"{intent.get('slug')} · {intent['shares']:.2f} sets", "high")
                intent["status"] = "confirmed"
            elif now > float(intent.get("end_ts") or 0) and max(up, down) <= config.position_tolerance:
                intent["status"] = "completed"


def _credentials() -> dict:
    return {
        "private_key": os.getenv("PRIVATE_KEY") or "",
        "funder_address": os.getenv("FUNDER_ADDRESS") or "",
        "builder_key": os.getenv("BUILDER_API_KEY") or "",
        "builder_secret": os.getenv("BUILDER_SECRET") or "",
        "builder_passphrase": os.getenv("BUILDER_PASS_PHRASE") or "",
    }


def _require_live_credentials(credentials: dict) -> None:
    required = ["private_key", "funder_address"]
    missing = [key for key in required if not credentials.get(key)]
    if missing:
        raise RuntimeError("missing live credentials: " + ", ".join(missing))


def _record_dry_plan(
    state: dict,
    market: MintMarket,
    config: BuyConfig,
    now: float,
) -> None:
    state.setdefault("dry_plans", []).append(
        {
            "ts": now,
            "condition_id": market.condition_id,
            "slug": market.slug,
            "series_slug": market.series_slug,
            "shares": config.shares,
            "set_cost": 1.0,
            "total_cost": config.shares,
            "mts_min": market.minutes_to_start(now),
        }
    )


def run_once(
    *,
    config: BuyConfig,
    state: dict,
    logger: logging.Logger,
    force_plan: bool = False,
    now: Optional[float] = None,
    market_gateway: Optional[MarketGateway] = None,
    chain: Optional[ChainReader] = None,
    status_gateway: Optional[RelayerStatusGateway] = None,
    relayer_factory: Callable[..., MintRelayer] = MintRelayer,
) -> dict:
    now = time.time() if now is None else now
    dry_run = True if force_plan else config.dry_run
    enabled = config.enabled or force_plan
    if not enabled:
        return {"status": "disabled"}
    if os.path.exists(STOP_FILE):
        return {"status": "stopped", "reason": "kill_switch"}
    if free_disk_mb() < config.min_free_disk_mb:
        return {"status": "blocked", "reason": "disk"}
    if not dry_run:
        if not is_fresh_arm(config, now):
            return {"status": "disarmed", "reason": "fresh_arm_required"}
        consume_arm()

    try:
        market_gateway = market_gateway or MarketGateway(
            gamma_url=config.gamma_url,
            data_api_url=config.data_api_url,
        )
        chain = chain or ChainReader(config.rpc_url)
        status_gateway = status_gateway or RelayerStatusGateway(config.relayer_url)
        credentials = _credentials()
        funder_address = credentials["funder_address"] or None

        reconcile_intents(
            config=config,
            state=state,
            funder_address=funder_address,
            chain=chain,
            status_gateway=status_gateway,
            logger=logger,
            now=now,
        )
    except Exception:
        if not dry_run:
            restore_arm()
        raise
    if any(
        intent.get("status") == "ambiguous"
        for intent in state.get("intents", {}).values()
    ):
        return {"status": "blocked", "reason": "ambiguous_intent"}

    markets = market_gateway.discover(config.series_slug_list())
    candidates = eligible_markets(markets, config, now)
    if not candidates:
        return {"status": "idle", "reason": "no_candidate", "markets": len(markets)}

    positions = market_gateway.positions(funder_address) if funder_address else {}
    open_count, open_notional, owned = _portfolio_usage(
        markets, positions, state, config.position_tolerance
    )
    if open_count >= config.max_open_sets:
        return {"status": "capped", "reason": "open_sets", "open_sets": open_count}
    if open_notional + config.shares > config.max_open_notional + 1e-9:
        return {"status": "capped", "reason": "open_notional", "open_notional": open_notional}
    daily_notional = _daily_notional(state, now)
    if daily_notional + config.shares > config.max_daily_notional + 1e-9:
        return {"status": "capped", "reason": "daily_notional", "daily_notional": daily_notional}
    if 1.0 > config.max_set_cost + 1e-9:
        return {"status": "blocked", "reason": "set_cost"}

    intents = state.setdefault("intents", {})
    planned_conditions = {
        str(plan.get("condition_id")) for plan in state.get("dry_plans", [])
    }
    candidate = next(
        (
            market
            for market in candidates
            if market.condition_id not in owned
            and (not config.one_entry_per_market or market.condition_id not in intents)
            and (not dry_run or market.condition_id not in planned_conditions)
            and float(positions.get(market.up_token, 0)) <= config.position_tolerance
            and float(positions.get(market.dn_token, 0)) <= config.position_tolerance
        ),
        None,
    )
    if candidate is None:
        return {"status": "idle", "reason": "no_unowned_candidate"}

    if dry_run:
        _record_dry_plan(state, candidate, config, now)
        trim_state(state, config.max_state_intents, config.max_dry_plans)
        save_state(state)
        logger.info(
            "DRY MINT %s shares=%.6f cost=%.6f mts=%.2fm",
            candidate.slug,
            config.shares,
            config.shares,
            candidate.minutes_to_start(now),
        )
        return {
            "status": "planned",
            "condition_id": candidate.condition_id,
            "slug": candidate.slug,
        }

    _require_live_credentials(credentials)
    funder_address = to_checksum_address(credentials["funder_address"])
    relayer = relayer_factory(
        relayer_url=config.relayer_url,
        chain_id=config.chain_id,
        private_key=credentials["private_key"],
        builder_key=credentials["builder_key"],
        builder_secret=credentials["builder_secret"],
        builder_passphrase=credentials["builder_passphrase"],
    )
    expected_funder = to_checksum_address(relayer.expected_funder())
    if expected_funder != funder_address:
        raise RuntimeError("derived proxy does not match FUNDER_ADDRESS")
    if not chain.has_contract(config.pUSD_address):
        raise RuntimeError("pUSD contract not found")
    if not chain.has_contract(config.standard_adapter_address):
        raise RuntimeError("standard collateral adapter not found")
    if chain.outcome_slot_count(config.ctf_address, candidate.condition_id) != 2:
        raise RuntimeError("condition is not prepared as binary")
    balance = chain.pUSD_balance(config.pUSD_address, funder_address)
    if balance + 1e-9 < config.shares:
        return {"status": "blocked", "reason": "insufficient_balance", "balance": round(balance, 6)}
    before_up = chain.position_balance(config.ctf_address, funder_address, candidate.up_token)
    before_dn = chain.position_balance(config.ctf_address, funder_address, candidate.dn_token)
    if max(before_up, before_dn) > config.position_tolerance:
        return {"status": "blocked", "reason": "onchain_position_exists"}

    metadata = f"polybuy:mint:{candidate.condition_id}:{int(now)}"
    intent = {
        "created_at": now,
        "updated_at": now,
        "submitted_at": 0.0,
        "status": "submitting",
        "condition_id": candidate.condition_id,
        "slug": candidate.slug,
        "question": candidate.question,
        "series_slug": candidate.series_slug,
        "end_ts": candidate.end_ts,
        "up_token": candidate.up_token,
        "dn_token": candidate.dn_token,
        "shares": config.shares,
        "set_cost": 1.0,
        "total_cost": config.shares,
        "before_up": before_up,
        "before_dn": before_dn,
        "metadata": metadata,
        "transaction_id": None,
    }
    intents[candidate.condition_id] = intent
    trim_state(state, config.max_state_intents, config.max_dry_plans)
    save_state(state)
    calls = build_atomic_mint_calls(
        pUSD_address=config.pUSD_address,
        adapter_address=config.standard_adapter_address,
        condition_id=candidate.condition_id,
        shares=config.shares,
    )
    try:
        transaction_id = relayer.submit(calls, metadata)
        intent["transaction_id"] = transaction_id
        intent["submitted_at"] = time.time()
        intent["updated_at"] = time.time()
        intent["status"] = "pending"
        save_state(state)
        logger.warning(
            "LIVE MINT SUBMITTED %s shares=%.6f tx=%s",
            candidate.slug,
            config.shares,
            transaction_id,
        )
        notify(
            "Polybuy mint submitted",
            f"{candidate.slug} · {config.shares:.2f} sets · tx {transaction_id}",
            "urgent",
        )
        return {"status": "submitted", "transaction_id": transaction_id, "slug": candidate.slug}
    except Exception as exc:
        intent["status"] = "ambiguous"
        intent["updated_at"] = time.time()
        intent["error"] = str(exc)[:300]
        save_state(state)
        notify("Polybuy mint ambiguous", f"{candidate.slug} · manual reconciliation required", "urgent")
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--config")
    args = parser.parse_args()
    load_dotenv()
    config = load_config(args.config)
    logger = setup_logging()
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    with single_instance_lock():
        state = load_state()
        if args.status:
            print(json.dumps(state, indent=2, sort_keys=True))
            return 0
        logger.info(
            "POLYBUY START enabled=%s dry_run=%s series=%s shares=%.6f",
            config.enabled,
            config.dry_run,
            ",".join(config.series_slug_list()),
            config.shares,
        )
        while not _shutdown:
            try:
                result = run_once(
                    config=config,
                    state=state,
                    logger=logger,
                    force_plan=args.plan,
                )
                trim_state(state, config.max_state_intents, config.max_dry_plans)
                save_state(state)
                heartbeat_fields = dict(result)
                heartbeat_status = heartbeat_fields.pop("status", "unknown")
                write_heartbeat(heartbeat_status, **heartbeat_fields)
                logger.info("STATUS %s", json.dumps(result, sort_keys=True))
            except Exception as exc:
                logger.exception("cycle failed: %s", exc)
                write_heartbeat("error", error=str(exc)[:300])
            if args.once or args.plan:
                break
            time.sleep(config.poll_s)
    logger.info("POLYBUY STOP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
