from __future__ import annotations

import os
from dataclasses import asdict
from typing import Optional

import requests
from eth_abi import encode as abi_encode
from eth_abi.packed import encode_packed
from eth_utils import keccak, to_bytes, to_checksum_address
from hexbytes import HexBytes
from eth_account import Account
from eth_account.messages import encode_defunct

from .contracts import ContractCall

# Polygon mainnet contract config for PROXY transactions
PROXY_FACTORY = to_checksum_address("0xaB45c5A4B0c941a2F231C04C3f49182e1A254052")
RELAY_HUB = to_checksum_address("0xD216153c06E857cD7f72665E0aF1d7D82172F494")
PROXY_INIT_CODE_HASH = "0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b"
DEFAULT_GAS_LIMIT = "500000"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _get_create2_address(bytecode_hash: str, from_address: str, salt: bytes) -> str:
    if bytecode_hash.startswith("0x"):
        bytecode_hash = bytecode_hash[2:]
    bytecode_hash_bytes = to_bytes(hexstr=bytecode_hash)
    from_address_bytes = to_bytes(hexstr=from_address)
    address_hash = keccak(b"\xff" + from_address_bytes + salt + bytecode_hash_bytes)
    return to_checksum_address(address_hash[-20:].hex())


def _derive_proxy_wallet(eoa: str) -> str:
    salt = keccak(encode_packed(["address"], [to_checksum_address(eoa)]))
    return _get_create2_address(PROXY_INIT_CODE_HASH, PROXY_FACTORY, salt)


def _encode_proxy_data(calls: list[ContractCall]) -> str:
    """Encode calls into proxy((uint8,address,uint256,bytes)[]) format."""
    selector = keccak(b"proxy((uint8,address,uint256,bytes)[])")[:4]
    tuples = []
    for call in calls:
        to_addr = to_checksum_address(call.to)
        data_bytes = to_bytes(hexstr=call.data)
        tuples.append((1, to_addr, int(call.value), data_bytes))
    encoded = abi_encode(["(uint8,address,uint256,bytes)[]"], [tuples])
    return "0x" + (selector + encoded).hex()


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
        self._private_key = private_key
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
        # Encode all calls into a single proxy batch transaction.
        encoded_data = _encode_proxy_data(calls)

        relay_r = requests.get(
            f"{self.relayer_url}/relay-payload",
            params={"address": self.eoa, "type": "PROXY"},
            headers=self.headers,
            timeout=15,
        )
        if relay_r.status_code != 200:
            raise RuntimeError(f"relay-payload fetch failed: HTTP {relay_r.status_code} {relay_r.text[:100]}")
        payload = relay_r.json()
        nonce = payload.get("nonce")
        relay_address = payload.get("address")
        if nonce is None or relay_address is None:
            raise RuntimeError("invalid relay payload received")

        # Build the proxy struct hash for signing
        data_bytes = to_bytes(hexstr=encoded_data)
        struct_hash = self._proxy_struct_hash(
            from_address=self.eoa,
            to=PROXY_FACTORY,
            data=data_bytes,
            nonce=nonce,
            relay=relay_address,
        )
        signature = self._sign(struct_hash)

        body = {
            "type": "PROXY",
            "from": self.eoa,
            "to": PROXY_FACTORY,
            "proxyWallet": _derive_proxy_wallet(self.eoa),
            "data": encoded_data,
            "nonce": nonce,
            "signature": signature,
            "signatureParams": {
                "gasPrice": "0",
                "gasLimit": DEFAULT_GAS_LIMIT,
                "relayerFee": "0",
                "relayHub": RELAY_HUB,
                "relay": relay_address,
            },
        }
        body["metadata"] = metadata or ""

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

    @staticmethod
    def _proxy_struct_hash(from_address: str, to: str, data: bytes, nonce: str, relay: str) -> str:
        prefix = b"rlx:"
        from_bytes = HexBytes(to_checksum_address(from_address))
        to_bytes_ = HexBytes(to_checksum_address(to))
        tx_fee_bytes = int("0").to_bytes(32, "big")
        gas_price_bytes = int("0").to_bytes(32, "big")
        gas_limit_bytes = int(DEFAULT_GAS_LIMIT).to_bytes(32, "big")
        nonce_bytes = int(nonce).to_bytes(32, "big")
        relay_hub_bytes = HexBytes(RELAY_HUB)
        relay_bytes = HexBytes(to_checksum_address(relay))
        message = (
            prefix
            + from_bytes
            + to_bytes_
            + data
            + tx_fee_bytes
            + gas_price_bytes
            + gas_limit_bytes
            + nonce_bytes
            + relay_hub_bytes
            + relay_bytes
        )
        return "0x" + keccak(message).hex()

    def _sign(self, struct_hash: str) -> str:
        msg = encode_defunct(HexBytes(struct_hash))
        sig = Account.sign_message(msg, self._private_key).signature.hex()
        return "0x" + sig
