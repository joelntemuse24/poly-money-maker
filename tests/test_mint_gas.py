"""Mint relay gas limit. No mintbot import, no live RPC, no .env read."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from unittest import mock

from buy.contracts import ContractCall, build_atomic_mint_calls
from buy.mint_gas import (
    RELAY_HUB_GAS_MAX,
    choose_mint_relay_gas,
    estimate_proxy_call_gas,
    plan_mint_gas,
    validate_mint_gas,
)


ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"
MINT_EXAMPLE = ROOT / "strategy_mint.example.json"
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"
CONDITION = "0x" + "ab" * 32


def _assign(name: str):
    tree = ast.parse(MINT.read_text(), filename=str(MINT))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found")


def _fn(name: str, extras: dict | None = None):
    tree = ast.parse(MINT.read_text(), filename=str(MINT))
    want = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            want = node
            break
    if want is None:
        raise AssertionError(f"{name} not found")
    future = ast.parse("from __future__ import annotations").body[0]
    ns: dict = {}
    if extras:
        ns.update(extras)
    module = ast.Module(body=[future, want], type_ignores=[])
    exec(compile(module, str(MINT), "exec"), ns)
    return ns[name]


class _Resp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class PlanMintGasTests(unittest.TestCase):
    def test_estimate_552k_adds_fifteen_percent_and_stays_under_cap(self):
        """04:15-style market: estimate 552k reverts at 500k and succeeds at 560k."""
        plan = plan_mint_gas(552_000, margin=0.15, fallback=650_000, cap=650_000)
        self.assertEqual(plan.gas_limit, 634_800)
        self.assertEqual(plan.estimate, 552_000)
        self.assertEqual(plan.source, "estimate")
        self.assertFalse(plan.clamped)
        self.assertGreater(plan.gas_limit, 560_000)
        self.assertLess(plan.gas_limit, RELAY_HUB_GAS_MAX)
        self.assertEqual(plan.relay_arg(), "634800")

    def test_estimate_failure_uses_650k_fallback(self):
        plan = plan_mint_gas(None, margin=0.15, fallback=650_000, cap=650_000)
        self.assertEqual(plan.gas_limit, 650_000)
        self.assertIsNone(plan.estimate)
        self.assertEqual(plan.source, "fallback")
        self.assertFalse(plan.clamped)
        self.assertEqual(plan.relay_arg(), "650000")

        zero = plan_mint_gas(0, margin=0.15, fallback=650_000, cap=650_000)
        self.assertEqual(zero.source, "fallback")
        self.assertEqual(zero.gas_limit, 650_000)

    def test_high_estimate_and_over_cap_clamp_to_hub_budget(self):
        """04:30-style market: estimate 583k reverts at 560k and succeeds at 650k."""
        plan = plan_mint_gas(583_000, margin=0.15, fallback=650_000, cap=650_000)
        self.assertEqual(plan.gas_limit, RELAY_HUB_GAS_MAX)
        self.assertEqual(plan.estimate, 583_000)
        self.assertTrue(plan.clamped)
        self.assertEqual(plan.source, "estimate")
        self.assertEqual(plan.cap, RELAY_HUB_GAS_MAX)

        tighter = plan_mint_gas(583_000, margin=0.15, fallback=650_000, cap=600_000)
        self.assertEqual(tighter.gas_limit, 600_000)
        self.assertTrue(tighter.clamped)
        self.assertEqual(tighter.cap, 600_000)

        above_hub = plan_mint_gas(583_000, margin=0.15, fallback=900_000, cap=900_000)
        self.assertEqual(above_hub.gas_limit, RELAY_HUB_GAS_MAX)
        self.assertEqual(above_hub.cap, RELAY_HUB_GAS_MAX)
        self.assertTrue(above_hub.clamped)

        fallback_over = plan_mint_gas(None, margin=0.15, fallback=700_000, cap=640_000)
        self.assertEqual(fallback_over.gas_limit, 640_000)
        self.assertTrue(fallback_over.clamped)
        self.assertEqual(fallback_over.source, "fallback")
        self.assertIsNone(fallback_over.estimate)

    def test_log_fields_include_limit_and_estimate(self):
        planned = plan_mint_gas(552_000, margin=0.15, fallback=650_000, cap=650_000)
        fields = planned.as_log()
        self.assertEqual(fields["gas_limit"], 634_800)
        self.assertEqual(fields["gas_estimate"], 552_000)
        self.assertEqual(fields["gas_source"], "estimate")
        self.assertIs(fields["gas_clamped"], False)
        missed = plan_mint_gas(None, margin=0.15, fallback=650_000, cap=650_000)
        blank = missed.as_log()
        self.assertEqual(blank["gas_limit"], 650_000)
        self.assertIsNone(blank["gas_estimate"])
        self.assertEqual(blank["gas_source"], "fallback")


class EstimateRpcTests(unittest.TestCase):
    def test_estimate_reads_hex_quantity_without_a_gas_cap(self):
        seen = {}

        def rpc(method, params):
            seen["method"] = method
            seen["params"] = params
            return "0x87010"

        gas = estimate_proxy_call_gas(rpc, "0xfrom", "0xfactory", "0xdata")
        self.assertEqual(gas, 552_976)
        self.assertEqual(seen["method"], "eth_estimateGas")
        call = seen["params"][0]
        self.assertEqual(call["from"], "0xfrom")
        self.assertEqual(call["to"], "0xfactory")
        self.assertEqual(call["data"], "0xdata")
        self.assertNotIn("gas", call)

    def test_rpc_failure_returns_none_for_the_fallback_path(self):
        def boom(method, params):
            raise RuntimeError("rpc down")

        self.assertIsNone(
            estimate_proxy_call_gas(boom, "0xfrom", "0xfactory", "0xdata")
        )

        def bad(method, params):
            return {"error": "nope"}

        self.assertIsNone(
            estimate_proxy_call_gas(bad, "0xfrom", "0xfactory", "0xdata")
        )
        self.assertIsNone(
            estimate_proxy_call_gas(lambda *_a: "0x0", "0xfrom", "0xfactory", "0x")
        )

    def test_choose_combines_estimate_margin_and_fallback(self):
        chosen = choose_mint_relay_gas(
            lambda *_a: "0x87010",
            from_address="0xfrom",
            to="0xfactory",
            data="0xdata",
            margin=0.15,
            fallback=650_000,
            cap=650_000,
        )
        self.assertEqual(chosen.estimate, 552_976)
        self.assertEqual(chosen.source, "estimate")
        self.assertEqual(chosen.gas_limit, (552_976 * 11_500 + 9_999) // 10_000)

        failed = choose_mint_relay_gas(
            lambda *_a: (_ for _ in ()).throw(RuntimeError("down")),
            from_address="0xfrom",
            to="0xfactory",
            data="0xdata",
            margin=0.15,
            fallback=650_000,
            cap=650_000,
        )
        self.assertEqual(failed.source, "fallback")
        self.assertEqual(failed.gas_limit, 650_000)
        self.assertIsNone(failed.estimate)


class RelayPayloadTests(unittest.TestCase):
    def test_gas_limit_reaches_proxy_args_and_submit_body(self):
        from py_builder_relayer_client.builder.derive import derive_proxy_wallet
        from py_builder_relayer_client.builder.proxy import build_proxy_transaction_request
        from py_builder_relayer_client.config import get_contract_config
        from py_builder_relayer_client.models import ProxyTransactionArgs
        from py_builder_relayer_client.signer import Signer as RelayerSigner

        plan = plan_mint_gas(552_000, margin=0.15, fallback=650_000, cap=650_000)
        signer = RelayerSigner(ANVIL_KEY, 137)
        config = get_contract_config(137)
        request = build_proxy_transaction_request(
            signer=signer,
            args=ProxyTransactionArgs(
                from_address=signer.address(),
                nonce="1",
                gas_price="0",
                data="0x1234",
                relay=config.relay_hub,
                gas_limit=plan.relay_arg(),
            ),
            config=config,
            metadata="mintbot:test",
        )
        body = request.to_dict()
        self.assertEqual(body["signatureParams"]["gasLimit"], "634800")
        self.assertNotEqual(body["signatureParams"]["gasLimit"], "500000")

        calls = build_atomic_mint_calls(
            pUSD_address=PUSD,
            adapter_address=ADAPTER,
            condition_id=CONDITION,
            shares=5,
        )
        proxy = derive_proxy_wallet(signer.address(), config.proxy_factory)
        env = {
            "PRIVATE_KEY": ANVIL_KEY,
            "FUNDER_ADDRESS": proxy,
            "RELAYER_API_KEY": "test-key",
            "RELAYER_API_KEY_ADDRESS": signer.address(),
            "RELAYER_URL": "https://relayer.test",
            "CHAIN_ID": "137",
        }

        class _Os:
            @staticmethod
            def getenv(key, default=None):
                return env.get(key, default)

        posted = {}

        def get(url, params=None, timeout=None):
            self.assertIn("/relay-payload", url)
            return _Resp(200, {"nonce": "7", "address": config.relay_hub})

        def post(url, json=None, headers=None, timeout=None):
            posted["url"] = url
            posted["json"] = json
            posted["headers"] = headers
            return _Resp(200, {"transactionID": "mint-tx"})

        def rpc(method, params):
            self.assertEqual(method, "eth_estimateGas")
            self.assertEqual(params[0]["from"], signer.address())
            self.assertEqual(params[0]["to"], config.proxy_factory)
            self.assertTrue(str(params[0]["data"]).startswith("0x"))
            self.assertNotIn("gas", params[0])
            return hex(552_000)

        submit = _fn(
            "submit_mint_batch",
            {
                "os": _Os,
                "requests": mock.Mock(get=get, post=post),
                "get_relayer_headers": lambda _body: {"Content-Type": "application/json"},
            },
        )
        tx_id, err, gas = submit(
            calls,
            "mintbot:test",
            rpc=rpc,
            gas_margin=0.15,
            gas_fallback=650_000,
            gas_cap=650_000,
        )
        self.assertEqual(err, None, err)
        self.assertEqual(tx_id, "mint-tx")
        self.assertEqual(posted["json"]["signatureParams"]["gasLimit"], "634800")
        self.assertEqual(posted["json"]["to"], config.proxy_factory)
        self.assertEqual(gas["gas_limit"], 634_800)
        self.assertEqual(gas["gas_estimate"], 552_000)
        self.assertEqual(gas["gas_source"], "estimate")
        self.assertIs(gas["gas_clamped"], False)
        self.assertNotIn(ANVIL_KEY, json.dumps(posted["json"]))

    def test_submit_fallback_when_rpc_raises(self):
        from py_builder_relayer_client.builder.derive import derive_proxy_wallet
        from py_builder_relayer_client.config import get_contract_config
        from py_builder_relayer_client.signer import Signer as RelayerSigner

        signer = RelayerSigner(ANVIL_KEY, 137)
        config = get_contract_config(137)
        proxy = derive_proxy_wallet(signer.address(), config.proxy_factory)
        env = {
            "PRIVATE_KEY": ANVIL_KEY,
            "FUNDER_ADDRESS": proxy,
            "RELAYER_API_KEY": "test-key",
            "RELAYER_API_KEY_ADDRESS": signer.address(),
            "RELAYER_URL": "https://relayer.test",
            "CHAIN_ID": "137",
        }

        class _Os:
            @staticmethod
            def getenv(key, default=None):
                return env.get(key, default)

        posted = {}

        def post(url, json=None, headers=None, timeout=None):
            posted["json"] = json
            return _Resp(200, {"transactionID": "mint-tx"})

        submit = _fn(
            "submit_mint_batch",
            {
                "os": _Os,
                "requests": mock.Mock(
                    get=lambda *a, **k: _Resp(200, {"nonce": "7", "address": config.relay_hub}),
                    post=post,
                ),
                "get_relayer_headers": lambda _body: {"Content-Type": "application/json"},
            },
        )
        calls = [
            ContractCall(to=PUSD, data="0xdead"),
        ]
        _tx, err, gas = submit(
            calls,
            "mintbot:test",
            rpc=lambda *_a: (_ for _ in ()).throw(RuntimeError("estimate failed")),
            gas_margin=0.15,
            gas_fallback=650_000,
            gas_cap=650_000,
        )
        self.assertIsNone(err)
        self.assertEqual(posted["json"]["signatureParams"]["gasLimit"], "650000")
        self.assertEqual(gas["gas_source"], "fallback")
        self.assertIsNone(gas["gas_estimate"])
        self.assertEqual(gas["gas_limit"], 650_000)

    def test_submit_clamps_to_configured_cap(self):
        from py_builder_relayer_client.builder.derive import derive_proxy_wallet
        from py_builder_relayer_client.config import get_contract_config
        from py_builder_relayer_client.signer import Signer as RelayerSigner

        signer = RelayerSigner(ANVIL_KEY, 137)
        config = get_contract_config(137)
        proxy = derive_proxy_wallet(signer.address(), config.proxy_factory)
        env = {
            "PRIVATE_KEY": ANVIL_KEY,
            "FUNDER_ADDRESS": proxy,
            "RELAYER_API_KEY": "test-key",
            "RELAYER_API_KEY_ADDRESS": signer.address(),
            "RELAYER_URL": "https://relayer.test",
            "CHAIN_ID": "137",
        }

        class _Os:
            @staticmethod
            def getenv(key, default=None):
                return env.get(key, default)

        posted = {}

        def post(url, json=None, headers=None, timeout=None):
            posted["json"] = json
            return _Resp(200, {"transactionID": "mint-tx"})

        submit = _fn(
            "submit_mint_batch",
            {
                "os": _Os,
                "requests": mock.Mock(
                    get=lambda *a, **k: _Resp(200, {"nonce": "7", "address": config.relay_hub}),
                    post=post,
                ),
                "get_relayer_headers": lambda _body: {"Content-Type": "application/json"},
            },
        )
        _tx, err, gas = submit(
            [ContractCall(to=PUSD, data="0xdead")],
            "mintbot:test",
            rpc=lambda *_a: hex(583_000),
            gas_margin=0.15,
            gas_fallback=650_000,
            gas_cap=600_000,
        )
        self.assertIsNone(err)
        self.assertEqual(posted["json"]["signatureParams"]["gasLimit"], "600000")
        self.assertTrue(gas["gas_clamped"])
        self.assertEqual(gas["gas_estimate"], 583_000)
        self.assertEqual(gas["gas_cap"], 600_000)


class MintGasConfigTests(unittest.TestCase):
    def test_defaults_and_legacy_config_without_new_keys(self):
        defaults = _assign("DEFAULTS")
        example = json.loads(MINT_EXAMPLE.read_text())
        self.assertEqual(defaults["mint_gas_margin"], 0.15)
        self.assertEqual(defaults["mint_gas_fallback"], 650_000)
        self.assertEqual(defaults["mint_gas_cap"], 650_000)
        self.assertEqual(example["mint_gas_margin"], 0.15)
        self.assertEqual(example["mint_gas_fallback"], 650_000)
        self.assertEqual(example["mint_gas_cap"], 650_000)
        validate_mint_gas(defaults)
        validate_mint_gas(example)
        legacy = dict(example)
        legacy.pop("mint_gas_margin")
        legacy.pop("mint_gas_fallback")
        legacy.pop("mint_gas_cap")
        validate_mint_gas(legacy)
        validate = _fn("validate_strategy", {"validate_mint_gas": validate_mint_gas})
        validate(legacy)
        validate(defaults)

    def test_invalid_gas_knobs_raise(self):
        defaults = _assign("DEFAULTS")
        for key, value in (
            ("mint_gas_margin", 1.5),
            ("mint_gas_margin", -0.01),
            ("mint_gas_fallback", 0),
            ("mint_gas_fallback", -5),
            ("mint_gas_cap", 0),
            ("mint_gas_cap", True),
        ):
            bad = dict(defaults)
            bad[key] = value
            with self.assertRaises(ValueError) as caught:
                validate_mint_gas(bad)
            self.assertIn(key, str(caught.exception))

    def test_sell_loop_does_not_estimate_gas_and_mint_logs_it(self):
        src = MINT.read_text()
        sell = src[src.find("def run_sell_cycle") : src.find("\ndef run_mint_cycle")]
        sells = src[src.find("def manage_sells") : src.find("def _claim_mint_intent")]
        mint = src[src.find("def run_mint_cycle") : src.find("\ndef _reload_cfg")]
        self.assertNotIn("eth_estimateGas", sell)
        self.assertNotIn("estimate_proxy", sell)
        self.assertNotIn("plan_mint_gas", sell)
        self.assertNotIn("choose_mint_relay_gas", sell)
        self.assertNotIn("eth_estimateGas", sells)
        self.assertNotIn("choose_mint_relay_gas", sells)
        self.assertIn("gas_limit", mint)
        self.assertIn("gas_estimate", mint)
        self.assertIn("mint_submitted", mint)
        self.assertIn("mint_submit_fail", mint)
        submit = src[src.find("def submit_mint_batch") : src.find("def get_relayer_transaction")]
        self.assertIn("gas_limit=", submit)
        self.assertIn("choose_mint_relay_gas", submit)
        self.assertNotIn("submit_mint_batch", sell)
