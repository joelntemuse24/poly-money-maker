"""A→B pUSD top-up policy. No live RPC, no mintbot import, no .env read."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest import mock

from eth_abi import decode
from eth_utils import keccak, to_checksum_address

from buy.contracts import build_pusd_transfer_call, pusd_units
from buy.sister_bid import COMPLEMENT_DEPOSIT, COMPLEMENT_PROXY, MINTBOT_FUNDER
from buy.sister_topup import (
    PUSD_ADDRESS,
    apply_topup,
    parse_env_file,
    place_error_is_broke,
    recipient_ok,
    topup_decision,
)
from sister_topup import submit_proxy_transfer


ROOT = Path(__file__).resolve().parents[1]
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


class TopupPolicyTests(unittest.TestCase):
    def _decide(self, state=None, **overrides):
        kwargs = dict(
            now_s=1_000.0,
            balance=0.40,
            need_usd=1.50,
            amount_usd=5.0,
            enabled=True,
            dry_run=False,
            force_broke=False,
            cleared=False,
            recipient=COMPLEMENT_DEPOSIT,
            a_balance=20.0,
            retry_s=120.0,
        )
        kwargs.update(overrides)
        return topup_decision(state, **kwargs)

    def test_short_balance_transfers_once_per_episode(self):
        state, decision = self._decide()
        self.assertEqual(decision["action"], "transfer")
        self.assertEqual(decision["reason"], "b_short")
        self.assertFalse(state["episode_open"])
        calls = []

        def _submit():
            calls.append("go")
            return True, "tx-1", ""

        state, decision = apply_topup(
            state,
            now_s=1_000.0,
            balance=0.40,
            need_usd=1.50,
            amount_usd=5.0,
            enabled=True,
            dry_run=False,
            force_broke=False,
            cleared=False,
            recipient=COMPLEMENT_DEPOSIT,
            a_balance=20.0,
            submitter=_submit,
        )
        self.assertEqual(calls, ["go"])
        self.assertEqual(decision["action"], "transfer")
        self.assertTrue(state["episode_open"])
        self.assertEqual(state["last_tx"], "tx-1")
        state, decision = self._decide(state, now_s=1_010.0, balance=0.40)
        self.assertEqual(decision["action"], "skip")
        self.assertEqual(decision["reason"], "episode_open")
        state, decision = self._decide(state, now_s=1_020.0, balance=6.0)
        self.assertEqual(decision["reason"], "recovered")
        self.assertFalse(state["episode_open"])
        state, decision = self._decide(state, now_s=1_030.0, balance=0.20)
        self.assertEqual(decision["action"], "transfer")

    def test_place_failure_while_funded_waits_for_a_clear(self):
        state, decision = self._decide(balance=10.0, force_broke=True)
        self.assertEqual(decision["action"], "transfer")
        self.assertEqual(decision["reason"], "place_fail")
        self.assertTrue(decision["close_requires_clear"])
        state, decision = apply_topup(
            None,
            now_s=1_000.0,
            balance=10.0,
            need_usd=1.50,
            amount_usd=5.0,
            enabled=True,
            dry_run=False,
            force_broke=True,
            cleared=False,
            recipient=COMPLEMENT_DEPOSIT,
            a_balance=20.0,
            submitter=lambda: (True, "tx-2", ""),
        )
        self.assertTrue(state["close_requires_clear"])
        state, decision = self._decide(state, now_s=1_010.0, balance=15.0, force_broke=True)
        self.assertEqual(decision["reason"], "episode_open")
        state, decision = self._decide(state, now_s=1_020.0, balance=15.0, cleared=True)
        self.assertEqual(decision["reason"], "cleared")
        self.assertFalse(state["episode_open"])

    def test_failed_submit_backs_off_then_retries(self):
        state, _decision = apply_topup(
            None,
            now_s=1_000.0,
            balance=0.10,
            need_usd=1.50,
            amount_usd=5.0,
            enabled=True,
            dry_run=False,
            force_broke=False,
            cleared=False,
            recipient=COMPLEMENT_DEPOSIT,
            a_balance=20.0,
            retry_s=120.0,
            submitter=lambda: (False, "", "HTTP 500"),
        )
        self.assertEqual(state["last_result"], "fail")
        self.assertFalse(state["episode_open"])
        state, decision = self._decide(state, now_s=1_030.0)
        self.assertEqual(decision["reason"], "backoff")
        state, decision = self._decide(state, now_s=1_130.0)
        self.assertEqual(decision["action"], "transfer")

    def test_blocks_proxy_funder_and_a_short_wallet(self):
        self.assertEqual(recipient_ok(COMPLEMENT_PROXY)[1], "magic_proxy")
        self.assertEqual(recipient_ok(MINTBOT_FUNDER)[1], "mintbot_funder")
        self.assertEqual(recipient_ok(COMPLEMENT_DEPOSIT)[1], "ok")
        _state, decision = self._decide(recipient=COMPLEMENT_PROXY)
        self.assertEqual(decision["reason"], "magic_proxy")
        _state, decision = self._decide(a_balance=2.0)
        self.assertEqual(decision["reason"], "a_short")
        _state, decision = self._decide(a_balance=None)
        self.assertEqual(decision["reason"], "a_unknown")
        state, decision = self._decide(dry_run=True, a_balance=None)
        self.assertEqual(decision["action"], "dry")
        self.assertFalse(state["episode_open"])

    def test_place_error_words(self):
        self.assertTrue(place_error_is_broke("not enough balance / allowance"))
        self.assertTrue(place_error_is_broke("INVALID_ORDER_NOT_ENOUGH_BALANCE"))
        self.assertFalse(place_error_is_broke("invalid tick"))

    def test_transfer_calldata_is_five_pusd(self):
        self.assertEqual(pusd_units(5), 5_000_000)
        call = build_pusd_transfer_call(
            pUSD_address=PUSD_ADDRESS,
            recipient=COMPLEMENT_DEPOSIT,
            usd=5,
        )
        raw = bytes.fromhex(call.data[2:])
        self.assertEqual(raw[:4], keccak(b"transfer(address,uint256)")[:4])
        to, amount = decode(["address", "uint256"], raw[4:])
        self.assertEqual(to_checksum_address(to), to_checksum_address(COMPLEMENT_DEPOSIT))
        self.assertEqual(amount, 5_000_000)
        self.assertEqual(to_checksum_address(call.to), to_checksum_address(PUSD_ADDRESS))

    def test_env_parser_keeps_values_local(self):
        parsed = parse_env_file(
            "\n# comment\nexport PRIVATE_KEY='abc'\nFUNDER_ADDRESS=\"0x1\"\n\nNOPE\n"
        )
        self.assertEqual(parsed["PRIVATE_KEY"], "abc")
        self.assertEqual(parsed["FUNDER_ADDRESS"], "0x1")
        self.assertNotIn("NOPE", parsed)


class ProxySubmitGuardTests(unittest.TestCase):
    def test_refuses_complement_funder_without_network(self):
        from buy.contracts import ContractCall

        ok, tx, err = submit_proxy_transfer(
            {"PRIVATE_KEY": ANVIL_KEY, "FUNDER_ADDRESS": COMPLEMENT_DEPOSIT},
            ContractCall(to=PUSD_ADDRESS, data="0x"),
        )
        self.assertFalse(ok)
        self.assertEqual(tx, "")
        self.assertEqual(err, "refusing complement funder")
        ok, _tx, err = submit_proxy_transfer(
            {"PRIVATE_KEY": ANVIL_KEY, "FUNDER_ADDRESS": COMPLEMENT_PROXY},
            ContractCall(to=PUSD_ADDRESS, data="0x"),
        )
        self.assertFalse(ok)
        self.assertEqual(err, "refusing complement funder")

    def test_mismatched_proxy_does_not_post(self):
        from py_builder_relayer_client.builder.derive import derive_proxy_wallet
        from py_builder_relayer_client.config import get_contract_config
        from py_builder_relayer_client.signer import Signer as RelayerSigner

        from buy.contracts import build_pusd_transfer_call

        signer = RelayerSigner(ANVIL_KEY, 137)
        proxy = derive_proxy_wallet(
            signer.address(), get_contract_config(137).proxy_factory
        )
        call = build_pusd_transfer_call(
            pUSD_address=PUSD_ADDRESS, recipient=COMPLEMENT_DEPOSIT, usd=5,
        )
        env = {
            "PRIVATE_KEY": ANVIL_KEY,
            "FUNDER_ADDRESS": MINTBOT_FUNDER,
            "RELAYER_API_KEY": "test-key",
            "RELAYER_API_KEY_ADDRESS": signer.address(),
            "RELAYER_URL": "https://relayer.test",
            "CHAIN_ID": "137",
        }
        self.assertNotEqual(proxy.lower(), MINTBOT_FUNDER.lower())

        class _Resp:
            def __init__(self, status, payload):
                self.status_code = status
                self._payload = payload
                self.text = ""

            def json(self):
                return self._payload

        relay = get_contract_config(137).relay_hub
        with mock.patch("sister_topup.requests.get", return_value=_Resp(200, {"nonce": "1", "address": relay})) as get, mock.patch("sister_topup.requests.post") as post:
            ok, _tx, err = submit_proxy_transfer(env, call)
        self.assertFalse(ok)
        self.assertIn("does not match", err)
        get.assert_called_once()
        post.assert_not_called()

        env["FUNDER_ADDRESS"] = proxy
        with mock.patch("sister_topup.requests.get", return_value=_Resp(200, {"nonce": "1", "address": relay})), mock.patch(
            "sister_topup.requests.post",
            return_value=_Resp(200, {"transactionID": "tx-live"}),
        ) as post:
            ok, tx, err = submit_proxy_transfer(env, call)
        self.assertTrue(ok, err)
        self.assertEqual(tx, "tx-live")
        posted = post.call_args.kwargs["json"]
        self.assertEqual(posted["proxyWallet"].lower(), proxy.lower())
        self.assertNotIn(ANVIL_KEY, str(posted))

    def test_script_does_not_import_mintbot_or_complement_env(self):
        src = (ROOT / "sister_topup.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertNotIn("mintbot", imported)
        self.assertNotIn("load_dotenv", src)
        self.assertNotIn('".env.complement"', src)
        self.assertIn("COMPLEMENT_DEPOSIT", src)
        self.assertIn("proxy_batch", src)
        self.assertIn("--live", src)


if __name__ == "__main__":
    unittest.main()
