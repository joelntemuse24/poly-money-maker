"""Complement deposit-wallet CLOB adapter (no network, no secrets)."""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from buy.complement_clob import (
    COMPLEMENT_SIGNATURE_TYPE,
    ComplementDepositClobClient,
    order_response_to_post_dict,
)
from buy.complement_gate import build_complement_clob_clients


FUNDER = "0xCfF52577f80222e4b36f03B5d58443781b9D2433"
HOST = "https://clob.polymarket.com"


@dataclass(frozen=True)
class _Signed:
    order_type: str
    maker: str
    signer: str
    signature_type: int


class _FakeSecure:
    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        type(self)._calls.append(dict(kwargs))
        self.wallet = kwargs["wallet"]
        self.wallet_type = "DEPOSIT_WALLET"
        self.credentials = SimpleNamespace(
            key="derived-key",
            secret="derived-secret",
            passphrase="derived-pass",
        )
        self.limit_orders = []
        self.posted = []
        self.balance_calls = []

    def create_limit_order(self, **kwargs):
        self.limit_orders.append(kwargs)
        return _Signed(
            order_type="GTC",
            maker=self.wallet,
            signer=self.wallet,
            signature_type=3,
        )

    def post_order(self, signed):
        self.posted.append(signed)
        return SimpleNamespace(
            ok=True,
            order_id="ord-1",
            status="live",
            taking_amount="0",
            making_amount="0.01",
        )

    def get_order(self, *, order_id):
        return SimpleNamespace(
            id=order_id,
            status="live",
            size_matched="0",
        )

    def get_balance_allowance(self, *, asset_type, token_id=None):
        self.balance_calls.append({"asset_type": asset_type, "token_id": token_id})
        return SimpleNamespace(balance=0)


class _RecordingFactory:
    def __init__(self):
        self.client_cls = _FakeSecure

    def __call__(self, **kwargs):
        return self.client_cls(**kwargs)


class ComplementDepositClobClientTests(unittest.TestCase):
    def setUp(self):
        _FakeSecure._calls = []
        self.factory = _RecordingFactory()

    def _client(self, **kwargs):
        defaults = dict(
            host=HOST,
            key="0xpriv",
            chain_id=137,
            creds=None,
            signature_type=COMPLEMENT_SIGNATURE_TYPE,
            funder=FUNDER,
            retry_on_error=False,
            _secure_factory=self.factory,
        )
        defaults.update(kwargs)
        return ComplementDepositClobClient(**defaults)

    def test_rejects_proxy_signature_type(self):
        with self.assertRaises(ValueError) as ctx:
            self._client(signature_type=1)
        self.assertIn("signature_type=3", str(ctx.exception))

    def test_rejects_empty_funder(self):
        with self.assertRaises(ValueError):
            self._client(funder="")

    def test_derive_and_trading_pass_funder_as_wallet(self):
        constructed = []

        def ctor(**kwargs):
            kwargs["_secure_factory"] = self.factory
            client = ComplementDepositClobClient(**kwargs)
            constructed.append(client)
            return client

        creds, client = build_complement_clob_clients(
            ctor,
            host=HOST,
            key="0xpriv",
            chain_id=137,
            creds=None,
            signature_type=COMPLEMENT_SIGNATURE_TYPE,
            funder=FUNDER,
        )
        self.assertEqual(creds["api_key"], "derived-key")
        self.assertEqual(len(constructed), 2)
        derive_client, trading_client = constructed
        self.assertIs(client, trading_client)
        for inst in constructed:
            self.assertEqual(inst.signature_type, 3)
            self.assertEqual(inst.funder, FUNDER)
            self.assertEqual(inst.kwargs["signature_type"], 3)
            self.assertEqual(inst.kwargs["funder"], FUNDER)
            self.assertEqual(inst.kwargs["chain_id"], 137)
        trading_client.create_order(
            SimpleNamespace(token_id="tok", price=0.01, size=1.0, side="BUY")
        )
        self.assertEqual(len(_FakeSecure._calls), 2)
        derive_kwargs, trading_kwargs = _FakeSecure._calls
        for kwargs in (derive_kwargs, trading_kwargs):
            self.assertEqual(kwargs["wallet"], FUNDER)
            self.assertEqual(kwargs["private_key"], "0xpriv")
        self.assertIsNone(derive_kwargs["credentials"])
        self.assertEqual(trading_kwargs["credentials"]["api_key"], "derived-key")

    def test_pregenerated_creds_still_bind_trading_client_to_funder(self):
        preset = {
            "api_key": "preset-key",
            "api_secret": "preset-secret",
            "api_passphrase": "preset-pass",
        }
        creds, client = build_complement_clob_clients(
            lambda **kwargs: ComplementDepositClobClient(
                _secure_factory=self.factory, **kwargs
            ),
            host=HOST,
            key="0xpriv",
            chain_id=137,
            creds=preset,
            signature_type=3,
            funder=FUNDER,
        )
        self.assertIs(creds, preset)
        self.assertEqual(len(_FakeSecure._calls), 0)
        signed = client.create_order(
            SimpleNamespace(token_id="tok", price=0.01, size=1.0, side="BUY")
        )
        self.assertEqual(signed.maker, FUNDER)
        self.assertEqual(signed.signer, FUNDER)
        self.assertEqual(signed.signature_type, 3)
        self.assertEqual(len(_FakeSecure._calls), 1)
        self.assertEqual(_FakeSecure._calls[0]["wallet"], FUNDER)
        self.assertEqual(_FakeSecure._calls[0]["credentials"]["api_key"], "preset-key")

    def test_post_order_forces_fak_and_maps_response(self):
        client = self._client()
        signed = client.create_order(
            SimpleNamespace(token_id="tok", price=0.01, size=1.0, side="BUY")
        )
        self.assertEqual(signed.order_type, "GTC")
        posted = client.post_order(signed, order_type="FAK")
        self.assertEqual(posted["orderID"], "ord-1")
        self.assertEqual(posted["status"], "live")
        inner = client._inner()
        self.assertEqual(inner.limit_orders[0]["token_id"], "tok")
        self.assertEqual(inner.limit_orders[0]["price"], 0.01)
        self.assertEqual(inner.limit_orders[0]["side"], "BUY")
        self.assertEqual(inner.posted[0].order_type, "FAK")
        self.assertEqual(inner.posted[0].maker, FUNDER)

    def test_wrong_wallet_type_is_rejected(self):
        class _Proxy(_FakeSecure):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.wallet_type = "POLY_PROXY"

        self.factory.client_cls = _Proxy
        client = self._client()
        with self.assertRaises(RuntimeError) as ctx:
            client.create_or_derive_api_key()
        self.assertIn("DEPOSIT_WALLET", str(ctx.exception))

    def test_rejected_fak_maps_error_text(self):
        data = order_response_to_post_dict(
            SimpleNamespace(
                ok=False,
                code="fak_not_filled",
                message="no orders found to match with FAK order",
            )
        )
        self.assertFalse(data["success"])
        self.assertIn("no orders found to match", data["errorMsg"])

    def test_default_factory_passes_deposit_wallet(self):
        created = {}

        class _SecureClient:
            @staticmethod
            def create(**kwargs):
                created.update(kwargs)
                return SimpleNamespace(
                    wallet=kwargs["wallet"],
                    wallet_type="DEPOSIT_WALLET",
                )

        with patch.dict(sys.modules, {"polymarket": SimpleNamespace(SecureClient=_SecureClient)}):
            from buy.complement_clob import default_secure_factory

            default_secure_factory(private_key="0xpriv", wallet=FUNDER)
        self.assertEqual(created["wallet"], FUNDER)
        self.assertEqual(created["private_key"], "0xpriv")
        self.assertNotIn("credentials", created)

    def test_signature_type_constant_is_poly_1271(self):
        self.assertEqual(COMPLEMENT_SIGNATURE_TYPE, 3)

    def test_buy_bots_stay_on_py_clob_proxy_type_1(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("buybot.py", "buybot5m.py", "buybothourly.py"):
            src = (root / name).read_text()
            self.assertIn("signature_type=1", src, name)
            self.assertNotIn("ComplementDepositClobClient", src, name)
            self.assertNotIn("polymarket.SecureClient", src, name)


if __name__ == "__main__":
    unittest.main()
