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


def encode_erc20_transfer(to: str, amount: int) -> str:
    """ERC-20 ``transfer(address,uint256)`` calldata. ``amount`` is raw units."""
    if amount <= 0:
        raise ValueError("transfer amount must be positive")
    selector = keccak(b"transfer(address,uint256)")[:4]
    payload = selector + encode(
        ["address", "uint256"],
        [to_checksum_address(to), amount],
    )
    return "0x" + payload.hex()


def pusd_units(usd: float) -> int:
    """Six-decimal pUSD units. Rejects values that are not an exact unit count."""
    units = int(round(float(usd) * 1_000_000))
    if units <= 0 or abs(units / 1_000_000 - float(usd)) > 1e-9:
        raise ValueError("usd must map exactly to six-decimal pUSD units")
    return units


def build_pusd_transfer_call(
    *,
    pUSD_address: str,
    recipient: str,
    usd: float,
) -> ContractCall:
    """One pUSD ``transfer`` from the signing wallet to ``recipient``."""
    return ContractCall(
        to=to_checksum_address(pUSD_address),
        data=encode_erc20_transfer(recipient, pusd_units(usd)),
    )


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


def encode_redeem_positions(*, collateral: str, condition_id: str) -> str:
    """``redeemPositions(collateral, 0x0, conditionId, [1, 2])``.

    Sent to the collateral adapter, which burns both outcome balances of
    the caller and pays the winning side out in ``collateral`` (pUSD).
    """
    selector = keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    payload = selector + encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [
            to_checksum_address(collateral),
            bytes(32),
            _condition_bytes(condition_id),
            [1, 2],
        ],
    )
    return "0x" + payload.hex()


def encode_set_approval_for_all(operator: str, approved: bool = True) -> str:
    selector = keccak(b"setApprovalForAll(address,bool)")[:4]
    payload = selector + encode(
        ["address", "bool"],
        [to_checksum_address(operator), bool(approved)],
    )
    return "0x" + payload.hex()


def build_redeem_calls(
    *,
    pUSD_address: str,
    adapter_address: str,
    ctf_address: str,
    condition_id: str,
    approve_adapter: bool,
) -> list[ContractCall]:
    """Redeem a resolved binary market through the collateral adapter.

    Same target and arguments as the Polymarket SDK's ``redeem_positions``
    for a non-neg-risk CTF market. ``approve_adapter`` prepends the CTF
    ``setApprovalForAll(adapter)`` the adapter needs to pull the outcome
    tokens, for a wallet that has not granted it yet.
    """
    calls: list[ContractCall] = []
    if approve_adapter:
        calls.append(
            ContractCall(
                to=to_checksum_address(ctf_address),
                data=encode_set_approval_for_all(adapter_address, True),
            )
        )
    calls.append(
        ContractCall(
            to=to_checksum_address(adapter_address),
            data=encode_redeem_positions(collateral=pUSD_address, condition_id=condition_id),
        )
    )
    return calls


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
