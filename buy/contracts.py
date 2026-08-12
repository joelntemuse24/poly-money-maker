from __future__ import annotations

from dataclasses import dataclass

from eth_abi import encode
from eth_utils import keccak, to_checksum_address


@dataclass(frozen=True)
class ContractCall:
    to: str
    data: str
    value: str = "0"


def _condition_bytes(condition_id: str) -> bytes:
    raw = condition_id.lower().removeprefix("0x")
    if len(raw) != 64:
        raise ValueError("condition_id must be bytes32 hex")
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError("condition_id must be bytes32 hex") from exc


def encode_approve(spender: str, amount: int) -> str:
    if amount <= 0:
        raise ValueError("approval amount must be positive")
    selector = keccak(b"approve(address,uint256)")[:4]
    payload = selector + encode(
        ["address", "uint256"],
        [to_checksum_address(spender), amount],
    )
    return "0x" + payload.hex()


def encode_split_position(
    *,
    collateral: str,
    condition_id: str,
    amount: int,
) -> str:
    if amount <= 0:
        raise ValueError("split amount must be positive")
    selector = keccak(
        b"splitPosition(address,bytes32,bytes32,uint256[],uint256)"
    )[:4]
    payload = selector + encode(
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [
            to_checksum_address(collateral),
            bytes(32),
            _condition_bytes(condition_id),
            [1, 2],
            amount,
        ],
    )
    return "0x" + payload.hex()


def build_atomic_mint_calls(
    *,
    pUSD_address: str,
    adapter_address: str,
    condition_id: str,
    shares: float,
) -> list[ContractCall]:
    """Approve adapter + splitPosition → equal Up/Down inventory."""
    amount = int(round(shares * 1_000_000))
    if amount <= 0 or abs(amount / 1_000_000 - shares) > 1e-9:
        raise ValueError("shares must map exactly to six-decimal pUSD units")
    return [
        ContractCall(
            to=to_checksum_address(pUSD_address),
            data=encode_approve(adapter_address, amount),
        ),
        ContractCall(
            to=to_checksum_address(adapter_address),
            data=encode_split_position(
                collateral=pUSD_address,
                condition_id=condition_id,
                amount=amount,
            ),
        ),
    ]
