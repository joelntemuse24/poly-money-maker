from __future__ import annotations

import os
from dataclasses import asdict
from typing import Optional

import requests

from .contracts import ContractCall


class RelayerStatusGateway:
    def __init__(self, relayer_url: str, timeout: float = 15.0):
        self.relayer_url = relayer_url.rstrip("/")
        self.timeout = timeout

    def transaction(self, transaction_id: str) -> Optional[dict]:
        response = requests.get(
            f"{self.relayer_url}/transaction",
            params={"id": transaction_id},
            timeout=self.timeout,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return payload[0] if payload else None
        return payload if isinstance(payload, dict) else None


class MintRelayer:
    """Submits mint transactions via the Polymarket relayer using HTTP headers.

    Uses the same RELAYER_API_KEY / RELAYER_API_KEY_ADDRESS approach as the bot's
    submit_proxy_tx function, bypassing the builder SDK which requires separate
    builder credentials.
    """

    def __init__(
        self,
        *,
        relayer_url: str,
        chain_id: int,
        private_key: str,
        builder_key: str = "",
        builder_secret: str = "",
        builder_passphrase: str = "",
    ):
        from eth_account import Account

        self.relayer_url = relayer_url.rstrip("/")
        self.chain_id = chain_id
        self.eoa = Account.from_key(private_key).address
        self.headers = {
            "Content-Type": "application/json",
            "RELAYER_API_KEY": os.getenv("RELAYER_API_KEY", "019df62f-45bc-796e-975c-3f434472b163"),
            "RELAYER_API_KEY_ADDRESS": os.getenv("RELAYER_API_KEY_ADDRESS", "0x42aec4505559c0613f7ce2541d9d29741bc5e195"),
        }

    def expected_funder(self) -> str:
        # The funder address is the proxy wallet derived from the EOA.
        # We trust the FUNDER_ADDRESS env var instead of deriving it.
        return os.getenv("FUNDER_ADDRESS", "")

    def submit(self, calls: list[ContractCall], metadata: str) -> str:
        # Submit each call as a separate proxy transaction, then return the last tx ID.
        tx_id = None
        for call in calls:
            nonce_r = requests.get(
                f"{self.relayer_url}/nonce",
                params={"address": self.eoa, "type": "PROXY"},
                headers=self.headers,
                timeout=15,
            )
            if nonce_r.status_code != 200:
                raise RuntimeError(f"nonce fetch failed: HTTP {nonce_r.status_code} {nonce_r.text[:100]}")
            body = {
                "type": "PROXY",
                "from": self.eoa,
                "to": call.to,
                "nonce": nonce_r.json().get("nonce", "0"),
                "data": call.data,
                "value": call.value,
            }
            submit_r = requests.post(
                f"{self.relayer_url}/submit",
                json=body,
                headers=self.headers,
                timeout=15,
            )
            if submit_r.status_code != 200:
                raise RuntimeError(f"relayer submit failed: HTTP {submit_r.status_code} {submit_r.text[:200]}")
            tx_id = submit_r.json().get("transactionID")
        if not tx_id:
            raise RuntimeError("relayer returned no transaction ID")
        return str(tx_id)
