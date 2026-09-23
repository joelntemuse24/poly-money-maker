#!/usr/bin/env python3
"""Move $5 of pUSD from wallet A to sister B's deposit wallet.

Wallet A signs a gasless PROXY relayer batch (the same path mintbot uses
to mint). The batch is one pUSD ``transfer`` to B's POLY_1271 deposit
wallet (``buy.sister_bid.COMPLEMENT_DEPOSIT``). It does not send native
USDC and it does not send to the Magic proxy.

The process reads mintbot's ``.env`` itself. It does not load
``.env.complement`` and it does not import ``mintbot``. Scrapbidder spawns
``--once --live`` when a hedge cannot be funded. A bare run does nothing.

Ops (after the operator has set sister ``dry_run`` false and ``topup_enabled``
true):

  .venv/bin/python sister_topup.py --once --reason manual
  .venv/bin/python sister_topup.py --once --live --reason manual

``--live`` still no-ops when ``strategy_scrapbid.json`` has ``dry_run`` true
or ``topup_enabled`` false. One accepted transfer per broke episode is
stored in gitignored ``positions_topup.json``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

from buy.chain import ChainReader
from buy.contracts import ContractCall, build_pusd_transfer_call
from buy.sister_bid import COMPLEMENT_DEPOSIT, COMPLEMENT_PROXY, SISTER_DEFAULTS
from buy.sister_topup import PUSD_ADDRESS, apply_topup, parse_env_file


REPO = Path(__file__).resolve().parent
ENV_FILE = REPO / ".env"
STRATEGY_FILE = REPO / "strategy_scrapbid.json"
STATE_FILE = REPO / "positions_topup.json"
LOCK_FILE = REPO / ".topup.lock"


def _log(event: str, **kwargs: Any) -> None:
    payload = {"ts": time.time(), "event": event, **kwargs}
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(payload: dict) -> None:
    temporary = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, STATE_FILE)


def _cfg() -> dict:
    cfg = dict(SISTER_DEFAULTS)
    if not STRATEGY_FILE.exists():
        return cfg
    try:
        raw = json.loads(STRATEGY_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    for key, value in raw.items():
        if key in cfg:
            cfg[key] = value
    return cfg


def _balance(rpc: str, owner: str) -> Optional[float]:
    try:
        return ChainReader(rpc, timeout=10.0).pUSD_balance(PUSD_ADDRESS, owner)
    except Exception:
        return None


def relayer_headers(env: dict, body: dict) -> Optional[dict]:
    """Builder or relayer headers from an explicit env dict. Never logs secrets."""
    relayer_key = str(env.get("RELAYER_API_KEY") or "")
    relayer_addr = str(env.get("RELAYER_API_KEY_ADDRESS") or "")
    if relayer_key and relayer_addr:
        return {
            "Content-Type": "application/json",
            "RELAYER_API_KEY": relayer_key,
            "RELAYER_API_KEY_ADDRESS": relayer_addr,
        }
    builder_key = str(env.get("POLY_BUILDER_API_KEY") or env.get("BUILDER_API_KEY") or "")
    builder_secret = str(env.get("POLY_BUILDER_SECRET") or env.get("BUILDER_SECRET") or "")
    builder_pass = str(env.get("POLY_BUILDER_PASSPHRASE") or env.get("BUILDER_PASS_PHRASE") or "")
    if not (builder_key and builder_secret and builder_pass):
        return None
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

    config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=builder_key,
            secret=builder_secret,
            passphrase=builder_pass,
        )
    )
    payload = config.generate_builder_headers(
        method="POST",
        path="/submit",
        body=json.dumps(body),
    )
    if payload is None:
        return None
    headers = dict(payload)
    headers["Content-Type"] = "application/json"
    return headers


def submit_proxy_transfer(env: dict, call: ContractCall) -> tuple[bool, str, str]:
    """PROXY batch: A's funder transfers pUSD. Returns ``(ok, tx_id, error)``."""
    private_key = str(env.get("PRIVATE_KEY") or "")
    funder = str(env.get("FUNDER_ADDRESS") or "")
    if not private_key or not funder:
        return False, "", "missing PRIVATE_KEY or FUNDER_ADDRESS"
    if funder.lower() in (COMPLEMENT_DEPOSIT.lower(), COMPLEMENT_PROXY.lower()):
        return False, "", "refusing complement funder"

    from py_builder_relayer_client.builder.proxy import build_proxy_transaction_request
    from py_builder_relayer_client.config import get_contract_config as get_relayer_contract_config
    from py_builder_relayer_client.encode.proxy import encode_proxy_transaction_data
    from py_builder_relayer_client.models import (
        CallType,
        ProxyTransaction,
        ProxyTransactionArgs,
    )
    from py_builder_relayer_client.signer import Signer as RelayerSigner

    try:
        chain_id = int(env.get("CHAIN_ID") or 137)
        relayer_url = str(env.get("RELAYER_URL") or "https://relayer-v2.polymarket.com").rstrip("/")
        signer = RelayerSigner(private_key, chain_id)
        eoa = signer.address()
        relayer_addr = str(env.get("RELAYER_API_KEY_ADDRESS") or "")
        if relayer_addr and relayer_addr.lower() != str(eoa).lower():
            return False, "", "RELAYER_API_KEY_ADDRESS does not match PRIVATE_KEY signer"
        nonce_r = requests.get(
            f"{relayer_url}/relay-payload",
            params={"address": eoa, "type": "PROXY"},
            timeout=15,
        )
        if nonce_r.status_code != 200:
            return False, "", f"relay payload fetch fail HTTP {nonce_r.status_code}"
        relay_payload = nonce_r.json()
        if not isinstance(relay_payload, dict):
            return False, "", "invalid relay payload"
        nonce = relay_payload.get("nonce")
        relay = relay_payload.get("address")
        if nonce is None or not relay:
            return False, "", "relay payload missing nonce/address"
        encoded_data = encode_proxy_transaction_data(
            [
                ProxyTransaction(
                    to=str(call.to),
                    type_code=CallType.Call,
                    data=str(call.data),
                    value="0",
                )
            ]
        )
        config = get_relayer_contract_config(chain_id)
        request = build_proxy_transaction_request(
            signer=signer,
            args=ProxyTransactionArgs(
                from_address=eoa,
                nonce=str(nonce),
                gas_price="0",
                data=encoded_data,
                relay=str(relay),
            ),
            config=config,
            metadata="sister-topup",
        )
        body = request.to_dict()
        if str(body.get("proxyWallet") or "").lower() != funder.lower():
            return False, "", "derived proxyWallet does not match FUNDER_ADDRESS"
        headers = relayer_headers(env, body)
        if headers is None:
            return False, "", "could not generate relayer authentication headers"
        submit_r = requests.post(
            f"{relayer_url}/submit",
            json=body,
            headers=headers,
            timeout=20,
        )
        if submit_r.status_code == 200:
            payload = submit_r.json()
            tx_id = payload.get("transactionID") if isinstance(payload, dict) else None
            if tx_id:
                return True, str(tx_id), ""
            return False, "", "relayer response missing transactionID"
        return False, "", f"HTTP {submit_r.status_code}"
    except Exception as exc:
        return False, "", f"relayer request failed: {str(exc)[:160]}"


def run_topup(
    *,
    live: bool,
    force_broke: bool,
    cleared: bool,
    reason: str,
    env: dict,
    state: dict,
    b_balance: Any,
    a_balance: Any,
    now_s: float,
) -> tuple[dict, dict]:
    """One decision. Live submit only when strategy and ``--live`` both allow it."""
    cfg = _cfg()
    dry = (not live) or bool(cfg.get("dry_run", True))
    amount = float(cfg.get("topup_usd") or 0)
    recipient = COMPLEMENT_DEPOSIT

    def _submit() -> tuple[bool, str, str]:
        call = build_pusd_transfer_call(
            pUSD_address=str(env.get("PUSD_ADDRESS") or PUSD_ADDRESS),
            recipient=recipient,
            usd=amount,
        )
        return submit_proxy_transfer(env, call)

    updated, decision = apply_topup(
        state,
        now_s=now_s,
        balance=b_balance,
        need_usd=float(cfg.get("topup_need_usd") or 0),
        amount_usd=amount,
        enabled=bool(cfg.get("topup_enabled", True)),
        dry_run=dry,
        force_broke=force_broke,
        cleared=cleared,
        recipient=recipient,
        a_balance=a_balance,
        retry_s=float(cfg.get("topup_retry_s") or 0),
        submitter=_submit if not dry else None,
    )
    decision["hook_reason"] = reason
    decision["asset"] = "pUSD"
    decision["via"] = "proxy_batch"
    return updated, decision


def _emit(state: dict, decision: dict) -> None:
    action = decision.get("action")
    reason = str(decision.get("reason") or "")
    if action == "transfer":
        _log(
            "sister_topup",
            reason=reason,
            hook=decision.get("hook_reason"),
            amount_usd=decision.get("amount_usd"),
            recipient=decision.get("recipient"),
            tx_id=decision.get("tx_id"),
            balance=decision.get("balance"),
            asset="pUSD",
            via="proxy_batch",
        )
        state["last_logged_reason"] = "submitted"
        return
    if action == "dry":
        _log(
            "sister_topup_dry",
            reason=reason,
            hook=decision.get("hook_reason"),
            amount_usd=decision.get("amount_usd"),
            recipient=decision.get("recipient"),
            balance=decision.get("balance"),
        )
        state["last_logged_reason"] = f"dry:{reason}"
        return
    token = f"skip:{reason}"
    if state.get("last_logged_reason") == token and reason in ("funded", "episode_open", "dry_throttled"):
        return
    event = "sister_topup_fail" if reason == "submit_fail" else "sister_topup_skip"
    _log(
        event,
        reason=reason,
        hook=decision.get("hook_reason"),
        balance=decision.get("balance"),
        error=decision.get("error"),
        recipient=COMPLEMENT_DEPOSIT,
    )
    state["last_logged_reason"] = token


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="One-shot A→B pUSD top-up")
    parser.add_argument("--once", action="store_true", help="run a single check")
    parser.add_argument("--live", action="store_true", help="submit when strategy dry_run is false")
    parser.add_argument("--force-broke", action="store_true", help="a hedge place failed balance/allowance")
    parser.add_argument("--cleared", action="store_true", help="a hedge place succeeded")
    parser.add_argument("--reason", default="manual")
    args = parser.parse_args(argv)
    if not args.once:
        parser.print_help()
        return 0
    if not ENV_FILE.exists():
        _log("sister_topup_skip", reason="missing_env", hook=args.reason)
        return 0
    try:
        env = parse_env_file(ENV_FILE.read_text(encoding="utf-8"))
    except OSError:
        _log("sister_topup_skip", reason="missing_env", hook=args.reason)
        return 0
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log("sister_topup_skip", reason="busy", hook=args.reason)
        return 0
    try:
        rpc = str(env.get("RPC_URL") or "https://polygon.drpc.org")
        funder = str(env.get("FUNDER_ADDRESS") or "")
        b_balance = _balance(rpc, COMPLEMENT_DEPOSIT)
        a_balance = _balance(rpc, funder) if funder else None
        state = _read_state()
        updated, decision = run_topup(
            live=bool(args.live),
            force_broke=bool(args.force_broke),
            cleared=bool(args.cleared),
            reason=str(args.reason or "manual"),
            env=env,
            state=state,
            b_balance=b_balance,
            a_balance=a_balance,
            now_s=time.time(),
        )
        _emit(updated, decision)
        _write_state(updated)
    finally:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
