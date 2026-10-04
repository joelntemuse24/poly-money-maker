"""PROXY relayer submit for lockbot redeems.

Same batch shape mintbot uses (gas estimate, proxy factory, signer nonce).
Lockbot imports this instead of mintbot, which loads credentials and takes
the mint process lock at import.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests

from buy.contracts import ContractCall
from buy.mint_gas import choose_mint_relay_gas


def relayer_headers(body: dict) -> Optional[dict]:
    """Builder or relayer headers from the process environment. Never logs secrets."""
    relayer_key = os.getenv("RELAYER_API_KEY") or ""
    relayer_addr = os.getenv("RELAYER_API_KEY_ADDRESS") or ""
    if relayer_key and relayer_addr:
        return {
            "Content-Type": "application/json",
            "RELAYER_API_KEY": relayer_key,
            "RELAYER_API_KEY_ADDRESS": relayer_addr,
        }
    builder_key = os.getenv("POLY_BUILDER_API_KEY") or os.getenv("BUILDER_API_KEY") or ""
    builder_secret = os.getenv("POLY_BUILDER_SECRET") or os.getenv("BUILDER_SECRET") or ""
    builder_pass = os.getenv("POLY_BUILDER_PASSPHRASE") or os.getenv("BUILDER_PASS_PHRASE") or ""
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
        body=__import__("json").dumps(body),
    )
    if payload is None:
        return None
    headers = dict(payload)
    headers["Content-Type"] = "application/json"
    return headers


def submit_proxy_batch(
    calls: list[ContractCall],
    metadata: str,
    *,
    rpc: Any = None,
    gas_margin: float = 0.15,
    gas_fallback: int = 650_000,
    gas_cap: int = 650_000,
) -> tuple[Optional[str], Optional[str], dict]:
    """Submit one PROXY batch. Returns ``(tx_id, error, gas_log)``."""
    private_key = os.getenv("PRIVATE_KEY") or ""
    funder = os.getenv("FUNDER_ADDRESS") or ""
    if not private_key or not funder:
        return None, "missing PRIVATE_KEY or FUNDER_ADDRESS", {}
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
        chain_id = int(os.getenv("CHAIN_ID") or 137)
        relayer_url = (os.getenv("RELAYER_URL") or "https://relayer-v2.polymarket.com").rstrip("/")
        signer = RelayerSigner(private_key, chain_id)
        eoa = signer.address()
        relayer_addr = os.getenv("RELAYER_API_KEY_ADDRESS") or ""
        if relayer_addr and relayer_addr.lower() != str(eoa).lower():
            return None, "RELAYER_API_KEY_ADDRESS does not match PRIVATE_KEY signer", {}
        nonce_r = requests.get(
            f"{relayer_url}/relay-payload",
            params={"address": eoa, "type": "PROXY"},
            timeout=15,
        )
        if nonce_r.status_code != 200:
            return None, f"relay payload fetch fail HTTP {nonce_r.status_code}", {}
        relay_payload = nonce_r.json()
        if not isinstance(relay_payload, dict):
            return None, "invalid relay payload", {}
        nonce = relay_payload.get("nonce")
        relay = relay_payload.get("address")
        if nonce is None or not relay:
            return None, "relay payload missing nonce/address", {}
        encoded = encode_proxy_transaction_data(
            [
                ProxyTransaction(
                    to=str(call.to),
                    type_code=CallType.Call,
                    data=str(call.data),
                    value="0",
                )
                for call in calls
            ]
        )
        config = get_relayer_contract_config(chain_id)
        gas_plan = choose_mint_relay_gas(
            rpc,
            from_address=eoa,
            to=config.proxy_factory,
            data=encoded,
            margin=gas_margin,
            fallback=gas_fallback,
            cap=gas_cap,
        )
        gas_log = gas_plan.as_log()
        request = build_proxy_transaction_request(
            signer=signer,
            args=ProxyTransactionArgs(
                from_address=eoa,
                nonce=str(nonce),
                gas_price="0",
                data=encoded,
                relay=str(relay),
                gas_limit=gas_plan.relay_arg(),
            ),
            config=config,
            metadata=metadata,
        )
        body = request.to_dict()
        if str(body.get("proxyWallet") or "").lower() != funder.lower():
            return None, "derived proxyWallet does not match FUNDER_ADDRESS", gas_log
        headers = relayer_headers(body)
        if headers is None:
            return None, "could not generate relayer authentication headers", gas_log
        submit_r = requests.post(f"{relayer_url}/submit", json=body, headers=headers, timeout=20)
        if submit_r.status_code == 200:
            payload = submit_r.json()
            tx_id = payload.get("transactionID") if isinstance(payload, dict) else None
            if tx_id:
                return str(tx_id), None, gas_log
            return None, "relayer response missing transactionID", gas_log
        return None, f"HTTP {submit_r.status_code} · {submit_r.text[:120]}", gas_log
    except Exception as exc:
        return None, f"relayer request failed: {str(exc)[:200]}", {}


def fetch_relayer_transaction(relayer_url: str, transaction_id: str) -> Optional[dict]:
    try:
        response = requests.get(
            f"{str(relayer_url).rstrip('/')}/transaction",
            params={"id": transaction_id},
            timeout=15,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None
