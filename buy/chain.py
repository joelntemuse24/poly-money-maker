from __future__ import annotations

import itertools
from typing import Any

import requests
from eth_abi import encode
from eth_utils import keccak, to_checksum_address


class ChainReader:
    """Minimal Polygon eth_call helper for mint prechecks."""

    def __init__(self, rpc_url: str, timeout: float = 15.0):
        self.rpc_url = rpc_url
        self.timeout = timeout
        self._ids = itertools.count(1)

    def _rpc(self, method: str, params: list) -> Any:
        response = requests.post(
            self.rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": method,
                "params": params,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload.get("result")

    def _eth_call(self, target: str, data: bytes) -> bytes:
        result = self._rpc(
            "eth_call",
            [{"to": to_checksum_address(target), "data": "0x" + data.hex()}, "latest"],
        )
        if not isinstance(result, str) or not result.startswith("0x"):
            raise RuntimeError("invalid eth_call response")
        return bytes.fromhex(result[2:])

    def pUSD_balance(self, token: str, owner: str) -> float:
        data = keccak(b"balanceOf(address)")[:4] + encode(
            ["address"], [to_checksum_address(owner)]
        )
        raw = self._eth_call(token, data)
        return int.from_bytes(raw, "big") / 1_000_000

    def position_balance(self, ctf: str, owner: str, token_id: str) -> float:
        data = keccak(b"balanceOf(address,uint256)")[:4] + encode(
            ["address", "uint256"],
            [to_checksum_address(owner), int(token_id)],
        )
        raw = self._eth_call(ctf, data)
        return int.from_bytes(raw, "big") / 1_000_000

    def outcome_slot_count(self, ctf: str, condition_id: str) -> int:
        raw_id = condition_id.lower().removeprefix("0x")
        if len(raw_id) != 64:
            raise ValueError("condition_id must be bytes32 hex")
        data = keccak(b"getOutcomeSlotCount(bytes32)")[:4] + encode(
            ["bytes32"], [bytes.fromhex(raw_id)]
        )
        raw = self._eth_call(ctf, data)
        return int.from_bytes(raw, "big")

    def has_contract(self, address: str) -> bool:
        code = self._rpc("eth_getCode", [to_checksum_address(address), "latest"])
        return isinstance(code, str) and code not in ("0x", "0x0", "")
