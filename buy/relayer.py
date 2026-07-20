from __future__ import annotations

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
    def __init__(
        self,
        *,
        relayer_url: str,
        chain_id: int,
        private_key: str,
        builder_key: str,
        builder_secret: str,
        builder_passphrase: str,
    ):
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.models import RelayerTxType
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

        builder_config = BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=builder_key,
                secret=builder_secret,
                passphrase=builder_passphrase,
            )
        )
        self.client = RelayClient(
            relayer_url,
            chain_id,
            private_key,
            builder_config,
            relay_tx_type=RelayerTxType.PROXY,
        )

    def expected_funder(self) -> str:
        return str(self.client.get_expected_proxy_wallet())

    def submit(self, calls: list[ContractCall], metadata: str) -> str:
        from py_builder_relayer_client.models import Transaction

        transactions = [Transaction(**asdict(call)) for call in calls]
        response = self.client.execute(transactions, metadata)
        transaction_id = getattr(response, "transaction_id", None)
        if not transaction_id:
            raise RuntimeError("relayer returned no transaction ID")
        return str(transaction_id)
