"""Complement-only CLOB adapter: deposit wallet as maker, signature_type 3.

py-clob-client-v2 L1 auth always sets POLY_ADDRESS to the EOA
(``create_level_1_headers`` / GitHub issues 70, 58, 75). Derive then
succeeds but the API key is bound to the EOA. Type-1/2 posts 400
``maker address not allowed``; type-3 posts 400 ``the order signer
address has to be the address of the API KEY``.

5m/15m/hourly stay on py-clob-client-v2 + Magic/proxy type 1. This
module is imported only by ``complementbot.py``. Trading goes through
``polymarket.SecureClient.create(private_key=..., wallet=funder,
api_key=RelayerApiKey(key=..., address=...))``.

Live complement ``FUNDER_ADDRESS`` / ``COMPLEMENT_WALLET`` must be the
deposit wallet ``0x2b2D1dA1a49E8BF73EbBC3EAC35D79cc88cd4ad2``. Cash may
still sit on the Magic proxy until the operator moves it. That proxy
(``0xCfF52577…``) still 400s ``maker address not allowed`` even when a
Relayer key is present — do not silently fall back to type 1.

Gamma names the deposit address ``proxyWallet``, so the official client
may classify it ``POLY_PROXY`` rather than ``DEPOSIT_WALLET``; both are
allowed when ``inner.wallet`` equals the funder.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import is_dataclass, replace
from typing import Any, Callable, Dict, Mapping, Optional

COMPLEMENT_SIGNATURE_TYPE = 3
# Live FUNDER / COMPLEMENT_WALLET must be this deposit wallet. Cash may
# still sit on MAGIC_PROXY_WALLET until the operator moves it.
DEPOSIT_WALLET_LIVE = "0x2b2D1dA1a49E8BF73EbBC3EAC35D79cc88cd4ad2"
# Verified on the VM: this Magic proxy still 400s ``maker address not
# allowed`` even when RelayerApiKey is present. Refuse it at startup.
MAGIC_PROXY_WALLET = "0xCfF52577f80222e4b36f03B5d58443781b9D2433"
# Gamma labels this account's deposit address ``proxyWallet``. Official
# SecureClient.create(wallet=funder) therefore classifies it POLY_PROXY,
# not DEPOSIT_WALLET. Either is allowed when inner.wallet == funder.
_ALLOWED_WALLET_TYPES = frozenset({"DEPOSIT_WALLET", "POLY_PROXY"})

SecureFactory = Callable[..., Any]


def _norm_addr(value: object) -> str:
    return str(value or "").strip().lower()


def _order_side(order_args: Any) -> str:
    side = getattr(order_args, "side", "BUY")
    if side in (0, "0"):
        return "BUY"
    if side in (1, "1"):
        return "SELL"
    text = str(getattr(side, "name", side) or "BUY").upper()
    if text in {"SELL", "ASK"}:
        return "SELL"
    return "BUY"


def _asset_type(params: Any) -> str:
    raw = getattr(params, "asset_type", None) if params is not None else None
    if raw is None:
        return "CONDITIONAL"
    if hasattr(raw, "value") and not isinstance(raw, str):
        raw = raw.value
    text = str(raw).upper()
    if text == "COLLATERAL":
        return "COLLATERAL"
    return "CONDITIONAL"


def creds_to_dict(creds: Any) -> Optional[Dict[str, str]]:
    """Normalize SDK creds to the py-clob ApiCreds field names. Never log these."""
    if creds is None:
        return None
    if isinstance(creds, dict):
        key = creds.get("api_key") or creds.get("key") or creds.get("apiKey")
        secret = creds.get("api_secret") or creds.get("secret")
        passphrase = creds.get("api_passphrase") or creds.get("passphrase")
    else:
        key = (
            getattr(creds, "api_key", None)
            or getattr(creds, "key", None)
            or getattr(creds, "apiKey", None)
        )
        secret = getattr(creds, "api_secret", None) or getattr(creds, "secret", None)
        passphrase = getattr(creds, "api_passphrase", None) or getattr(
            creds, "passphrase", None
        )
    if not key or not secret or not passphrase:
        return None
    return {
        "api_key": str(key),
        "api_secret": str(secret),
        "api_passphrase": str(passphrase),
    }


def creds_to_secure(creds: Any) -> Any:
    """Convert py-clob / dict creds into polymarket ``ApiKeyCreds`` when needed."""
    if creds is None:
        return None
    if type(creds).__module__.startswith("polymarket.") and type(creds).__name__ == "ApiKeyCreds":
        return creds
    payload = creds_to_dict(creds)
    if payload is None:
        return creds
    from polymarket.models.clob.api_key import ApiKeyCreds

    return ApiKeyCreds(
        key=payload["api_key"],
        secret=payload["api_secret"],
        passphrase=payload["api_passphrase"],
    )


def order_response_to_post_dict(result: Any) -> Dict[str, Any]:
    """Shape a SecureClient order result for ``complement_fill_from_post``."""
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    data: Dict[str, Any] = {}
    ok = getattr(result, "ok", None)
    if ok is False:
        msg = str(
            getattr(result, "message", None)
            or getattr(result, "error_msg", None)
            or getattr(result, "errorMsg", None)
            or ""
        )
        data["success"] = False
        data["errorMsg"] = msg
        data["error"] = msg
        code = getattr(result, "code", None)
        if code is not None:
            data["status"] = str(code)
        return data
    if ok is True:
        data["success"] = True
    elif hasattr(result, "success"):
        data["success"] = bool(result.success)
    status = getattr(result, "status", None)
    if status is not None:
        data["status"] = status
    oid = (
        getattr(result, "order_id", None)
        or getattr(result, "orderID", None)
        or getattr(result, "id", None)
    )
    if oid:
        data["orderID"] = str(oid)
        data["order_id"] = str(oid)
    taking = getattr(result, "taking_amount", None)
    if taking is None:
        taking = getattr(result, "takingAmount", None)
    if taking is not None:
        data["takingAmount"] = str(taking)
        data["taking_amount"] = str(taking)
    making = getattr(result, "making_amount", None)
    if making is None:
        making = getattr(result, "makingAmount", None)
    if making is not None:
        data["makingAmount"] = str(making)
    matched = getattr(result, "size_matched", None)
    if matched is not None:
        data["size_matched"] = str(matched)
    err = (
        getattr(result, "error_msg", None)
        or getattr(result, "errorMsg", None)
        or getattr(result, "message", None)
    )
    if err:
        data["errorMsg"] = str(err)
        data["error"] = str(err)
    return data


def _secure_create_params(create_fn: Any) -> Optional[Mapping[str, inspect.Parameter]]:
    try:
        return inspect.signature(create_fn).parameters
    except (TypeError, ValueError):
        return None


def _require_secure_create_accepts_relayer(create_fn: Any) -> None:
    """Fail closed if SecureClient.create cannot take Relayer ``api_key``."""
    params = _secure_create_params(create_fn)
    if params is None:
        return
    if "api_key" in params:
        return
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return
    raise RuntimeError(
        "SecureClient.create must accept api_key=RelayerApiKey(key=..., address=...). "
        "Refusing to start without Relayer on the CLOB client — "
        "will not fall back to Magic/proxy signature_type=1."
    )


def _optional_secure_create_kwargs(create_fn: Any) -> Dict[str, Any]:
    """Pass wallet_type / signature_type only if SecureClient.create accepts them."""
    extra: Dict[str, Any] = {}
    params = _secure_create_params(create_fn)
    if params is None:
        return extra
    if "wallet_type" in params:
        extra["wallet_type"] = "DEPOSIT_WALLET"
    if "signature_type" in params:
        extra["signature_type"] = COMPLEMENT_SIGNATURE_TYPE
    return extra


def complement_wallet_from_env(environ: Optional[Mapping[str, str]] = None) -> str:
    """Deposit wallet for SecureClient.create. ``COMPLEMENT_WALLET`` or ``FUNDER_ADDRESS``.

    Live funder must be the deposit wallet ``DEPOSIT_WALLET_LIVE``.
    Relayer vars are read from the process env — on the VM that is
    ``.env.complement`` (systemd EnvironmentFile), not the primary ``.env``.
    """
    env = os.environ if environ is None else environ
    return str(env.get("COMPLEMENT_WALLET") or env.get("FUNDER_ADDRESS") or "").strip()


def is_magic_proxy_wallet(value: object) -> bool:
    return _norm_addr(value) == _norm_addr(MAGIC_PROXY_WALLET)


def require_complement_deposit_wallet(wallet: object) -> str:
    """Refuse an empty funder or the known-bad Magic proxy.

    Does not hard-require ``DEPOSIT_WALLET_LIVE`` so ``COMPLEMENT_WALLET``
    can switch accounts without another code PR. The Magic proxy is the
    one address verified to 400 ``maker address not allowed`` with Relayer.
    """
    wallet_s = str(wallet or "").strip()
    if not wallet_s:
        raise RuntimeError(
            "complement CLOB requires COMPLEMENT_WALLET or FUNDER_ADDRESS "
            f"(deposit wallet {DEPOSIT_WALLET_LIVE})"
        )
    if is_magic_proxy_wallet(wallet_s):
        raise RuntimeError(
            "complement CLOB funder is the Magic proxy "
            f"({MAGIC_PROXY_WALLET}). That address 400s "
            "maker address not allowed even with Relayer. "
            "Set COMPLEMENT_WALLET / FUNDER_ADDRESS in .env.complement to "
            f"the deposit wallet {DEPOSIT_WALLET_LIVE} and move cash first."
        )
    return wallet_s


def relayer_api_key_from_env(environ: Optional[Mapping[str, str]] = None) -> Any:
    """Build ``RelayerApiKey(key=..., address=...)`` from env. Fail closed if missing.

    Requires ``RELAYER_API_KEY`` plus ``RELAYER_ADDRESS`` or
    ``RELAYER_API_KEY_ADDRESS`` (the Relayer EOA). Never falls back to
    Magic/proxy type 1.
    """
    env = os.environ if environ is None else environ
    key = str(
        env.get("RELAYER_API_KEY")
        or env.get("POLYMARKET_RELAYER_API_KEY")
        or ""
    ).strip()
    # Primary bots / official docs use RELAYER_API_KEY_ADDRESS. Complement
    # previously required RELAYER_ADDRESS only, so a copied .env.complement
    # failed closed at startup and never posted.
    address = str(
        env.get("RELAYER_ADDRESS")
        or env.get("RELAYER_API_KEY_ADDRESS")
        or env.get("POLYMARKET_RELAYER_API_KEY_ADDRESS")
        or ""
    ).strip()
    if not key or not address:
        raise RuntimeError(
            "complement CLOB requires RELAYER_API_KEY and RELAYER_ADDRESS "
            "(or RELAYER_API_KEY_ADDRESS). Refusing to start without Relayer — "
            "will not fall back to Magic/proxy signature_type=1."
        )
    from polymarket.auth import RelayerApiKey

    return RelayerApiKey(key=key, address=address)


def default_secure_factory(
    *,
    private_key: str,
    wallet: str,
    credentials: Any = None,
    api_key: Any = None,
) -> Any:
    """Build the official sync client with deposit ``wallet`` + Relayer ``api_key``."""
    from polymarket import SecureClient

    _require_secure_create_accepts_relayer(SecureClient.create)
    relayer = api_key if api_key is not None else relayer_api_key_from_env()
    require_complement_deposit_wallet(wallet)
    kwargs: Dict[str, Any] = {
        "private_key": private_key,
        "wallet": wallet,
        "api_key": relayer,
    }
    if credentials is not None:
        kwargs["credentials"] = creds_to_secure(credentials)
    kwargs.update(_optional_secure_create_kwargs(SecureClient.create))
    return SecureClient.create(**kwargs)


def _require_deposit_wallet(inner: Any, funder: str) -> None:
    wallet = str(getattr(inner, "wallet", "") or "")
    if not wallet or _norm_addr(wallet) != _norm_addr(funder):
        raise RuntimeError(
            "complement CLOB wallet must equal FUNDER_ADDRESS (deposit wallet)"
        )
    wallet_type = getattr(inner, "wallet_type", None)
    if wallet_type not in _ALLOWED_WALLET_TYPES:
        raise RuntimeError(
            "complement CLOB client wallet_type must be DEPOSIT_WALLET or "
            f"POLY_PROXY (Gamma proxyWallet), got {wallet_type!r}"
        )


def _with_order_type(signed: Any, order_type: str) -> Any:
    current = getattr(signed, "order_type", None)
    if current == order_type:
        return signed
    if is_dataclass(signed) and not isinstance(signed, type):
        return replace(signed, order_type=order_type)
    raise TypeError("signed complement order must be a dataclass with order_type")


class ComplementDepositClobClient:
    """Duck-typed ClobClient used by ``build_complement_clob_clients``.

    Constructor kwargs match py-clob ``ClobClient`` so the shared builder
    can construct derive + trading clients without knowing the SDK. Both
    constructions require ``signature_type=3`` and a non-empty ``funder``.
    ``_secure_factory`` is for tests; production uses ``SecureClient.create``.
    """

    def __init__(
        self,
        *,
        host: str,
        key: str,
        chain_id: int,
        creds: Any = None,
        signature_type: int,
        funder: str,
        retry_on_error: bool = False,
        _secure_factory: Optional[SecureFactory] = None,
    ) -> None:
        if int(signature_type) != COMPLEMENT_SIGNATURE_TYPE:
            raise ValueError(
                "complement CLOB must use signature_type="
                f"{COMPLEMENT_SIGNATURE_TYPE} (POLY_1271 deposit wallet), "
                f"got {signature_type!r}"
            )
        funder_s = str(funder or "").strip()
        if not funder_s:
            raise ValueError("complement CLOB requires funder (deposit wallet)")
        # Live FUNDER / COMPLEMENT_WALLET must be deposit 0x2b2D1dA1a49E8BF73EbBC3EAC35D79cc88cd4ad2.
        if not str(key or "").strip():
            raise ValueError("complement CLOB requires a private key")
        self.host = host
        self.key = key
        self.chain_id = int(chain_id)
        self.creds = creds
        self.signature_type = COMPLEMENT_SIGNATURE_TYPE
        self.funder = funder_s
        self.retry_on_error = bool(retry_on_error)
        self._secure_factory = _secure_factory or default_secure_factory
        self._secure: Any = None
        self.kwargs = {
            "host": host,
            "key": key,
            "chain_id": int(chain_id),
            "creds": creds,
            "signature_type": COMPLEMENT_SIGNATURE_TYPE,
            "funder": funder_s,
            "retry_on_error": bool(retry_on_error),
        }

    def _connect(self, *, credentials: Any) -> Any:
        inner = self._secure_factory(
            private_key=self.key,
            wallet=self.funder,
            credentials=credentials,
        )
        _require_deposit_wallet(inner, self.funder)
        return inner

    def _inner(self) -> Any:
        if self._secure is None:
            self._secure = self._connect(credentials=self.creds)
        return self._secure

    def create_or_derive_api_key(self) -> Dict[str, str]:
        inner = self._connect(credentials=None)
        payload = creds_to_dict(getattr(inner, "credentials", None))
        if payload is None:
            raise RuntimeError("SecureClient.create did not return API credentials")
        self.creds = payload
        self._secure = inner
        return payload

    def create_order(self, order_args: Any, options: Any = None) -> Any:
        del options
        return self._inner().create_limit_order(
            token_id=str(getattr(order_args, "token_id")),
            price=getattr(order_args, "price"),
            size=getattr(order_args, "size"),
            side=_order_side(order_args),
        )

    def post_order(self, signed: Any, order_type: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        del args, kwargs
        want = getattr(order_type, "value", order_type)
        if want is None:
            want = "FAK"
        signed = _with_order_type(signed, str(want))
        return order_response_to_post_dict(self._inner().post_order(signed))

    def get_order(self, order_id: str) -> Dict[str, Any]:
        details = self._inner().get_order(order_id=str(order_id))
        return order_response_to_post_dict(details)

    def get_balance_allowance(self, params: Any = None) -> Any:
        token_id = getattr(params, "token_id", None) if params is not None else None
        token = str(token_id) if token_id else None
        return self._inner().get_balance_allowance(
            asset_type=_asset_type(params),
            token_id=token,
        )

    def update_balance_allowance(self, params: Any = None) -> None:
        del params
        return None


__all__ = [
    "COMPLEMENT_SIGNATURE_TYPE",
    "DEPOSIT_WALLET_LIVE",
    "MAGIC_PROXY_WALLET",
    "ComplementDepositClobClient",
    "complement_wallet_from_env",
    "creds_to_dict",
    "creds_to_secure",
    "default_secure_factory",
    "is_magic_proxy_wallet",
    "order_response_to_post_dict",
    "relayer_api_key_from_env",
    "require_complement_deposit_wallet",
]
