from __future__ import annotations

import copy
import logging
import unittest
from dataclasses import replace
from unittest.mock import patch

from eth_abi import decode
from eth_utils import keccak, to_checksum_address

from .config import BuyConfig, validate_config
from .contracts import build_atomic_mint_calls
from .market import MarketGateway, MintMarket
from .runner import _daily_notional, eligible_markets, reconcile_intents, run_once

CONDITION = "0x" + "11" * 32
FUNDER = "0x1111111111111111111111111111111111111111"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"


class FakeMarketGateway:
    def __init__(self, market, positions=None, blocked=False):
        self.market = market
        self.position_values = positions or {}
        self.blocked = blocked
        self.position_calls = 0

    def geoblock(self):
        return {"blocked": self.blocked, "country": "IE", "region": "L"}

    def discover(self, series_slugs):
        return [self.market]

    def positions(self, funder_address):
        self.position_calls += 1
        return dict(self.position_values)


class FakeChain:
    def __init__(self):
        self.position_values = {}

    def position_balance(self, ctf, owner, token_id):
        return float(self.position_values.get(token_id, 0))

    def pUSD_balance(self, token, owner):
        return 100.0

    def outcome_slot_count(self, ctf, condition_id):
        return 2

    def has_contract(self, address):
        return True


class FakeStatusGateway:
    def transaction(self, transaction_id):
        return None


class FakeRelayer:
    def __init__(self):
        self.calls = None
        self.metadata = None

    def expected_funder(self):
        return FUNDER

    def submit(self, calls, metadata):
        self.calls = calls
        self.metadata = metadata
        return "tx-123"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}

    def get(self, *args, **kwargs):
        return FakeResponse(self.payload)


class MarketTests(unittest.TestCase):
    def test_discovery_accepts_gamma_list_of_events(self):
        payload = [{
            "active": True,
            "closed": False,
            "markets": [{
                "conditionId": CONDITION,
                "slug": "btc-updown-15m-test",
                "question": "Bitcoin Up or Down",
                "endDate": "2030-01-01T00:10:00Z",
                "clobTokenIds": '["101", "202"]',
                "outcomes": '["Up", "Down"]',
                "active": "true",
                "closed": "false",
                "acceptingOrders": "true",
                "negRisk": "false",
            }],
        }]
        gateway = MarketGateway(
            gamma_url="https://example.test",
            data_api_url="https://example.test",
            geoblock_url="https://example.test",
            session=FakeSession(payload),
        )
        markets = gateway.discover(["btc-up-or-down-15m"])
        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0].condition_id, CONDITION)
        self.assertTrue(markets[0].active)
        self.assertFalse(markets[0].closed)
        self.assertTrue(markets[0].accepting_orders)
        self.assertFalse(markets[0].neg_risk)


class ConfigTests(unittest.TestCase):
    def test_defaults_are_disabled_and_dry(self):
        config = BuyConfig()
        self.assertFalse(config.enabled)
        self.assertTrue(config.dry_run)
        self.assertEqual(config.entry_method, "mint")
        self.assertEqual(config.max_set_cost, 1.0)

    def test_rejects_sub_dollar_mint_gate(self):
        with self.assertRaisesRegex(ValueError, "blocks deterministic mint"):
            validate_config(replace(BuyConfig(), max_set_cost=0.99))

    def test_live_safety_invariants_cannot_be_disabled(self):
        with self.assertRaisesRegex(ValueError, "geoblock"):
            validate_config(replace(BuyConfig(), require_geoblock_clear=False))
        with self.assertRaisesRegex(ValueError, "funder"):
            validate_config(replace(BuyConfig(), require_funder_match=False))


class ContractTests(unittest.TestCase):
    def test_atomic_calls_approve_exact_amount_then_split(self):
        calls = build_atomic_mint_calls(
            pUSD_address=PUSD,
            adapter_address=ADAPTER,
            condition_id=CONDITION,
            shares=5.0,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].to, to_checksum_address(PUSD))
        self.assertEqual(calls[1].to, to_checksum_address(ADAPTER))
        approve = bytes.fromhex(calls[0].data[2:])
        self.assertEqual(approve[:4], keccak(b"approve(address,uint256)")[:4])
        spender, amount = decode(["address", "uint256"], approve[4:])
        self.assertEqual(to_checksum_address(spender), to_checksum_address(ADAPTER))
        self.assertEqual(amount, 5_000_000)
        split = bytes.fromhex(calls[1].data[2:])
        self.assertEqual(
            split[:4],
            keccak(b"splitPosition(address,bytes32,bytes32,uint256[],uint256)")[:4],
        )
        collateral, parent, condition, partition, split_amount = decode(
            ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
            split[4:],
        )
        self.assertEqual(to_checksum_address(collateral), to_checksum_address(PUSD))
        self.assertEqual(parent, bytes(32))
        self.assertEqual(condition, bytes.fromhex("11" * 32))
        self.assertEqual(tuple(partition), (1, 2))
        self.assertEqual(split_amount, 5_000_000)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0
        self.market = MintMarket(
            condition_id=CONDITION,
            slug="btc-updown-15m-test",
            question="Bitcoin Up or Down",
            end_ts=self.now + 600,
            series_slug="btc-up-or-down-15m",
            up_token="101",
            dn_token="202",
            active=True,
            closed=False,
            accepting_orders=True,
            neg_risk=False,
        )
        self.config = replace(
            BuyConfig(),
            enabled=True,
            dry_run=True,
            min_free_disk_mb=0,
        )
        self.logger = logging.getLogger("buy-tests")

    def test_eligibility_rejects_neg_risk(self):
        neg_risk = replace(self.market, neg_risk=True)
        self.assertEqual(eligible_markets([neg_risk], self.config, self.now), [])

    def test_disabled_does_not_touch_gateways(self):
        result = run_once(
            config=BuyConfig(),
            state={"intents": {}, "dry_plans": []},
            logger=self.logger,
            now=self.now,
        )
        self.assertEqual(result, {"status": "disabled"})

    @patch("buy.runner.save_state")
    @patch("buy.runner.free_disk_mb", return_value=10_000)
    def test_dry_plan_does_not_submit(self, free_disk, save_state):
        state = {"intents": {}, "dry_plans": []}
        result = run_once(
            config=self.config,
            state=state,
            logger=self.logger,
            now=self.now,
            market_gateway=FakeMarketGateway(self.market),
            chain=FakeChain(),
            status_gateway=FakeStatusGateway(),
        )
        self.assertEqual(result["status"], "planned")
        self.assertEqual(len(state["dry_plans"]), 1)
        self.assertEqual(state["intents"], {})
        save_state.assert_called_once()

    @patch("buy.runner.consume_arm")
    @patch("buy.runner.is_fresh_arm", return_value=True)
    @patch("buy.runner.free_disk_mb", return_value=10_000)
    def test_geoblock_stops_live_after_consuming_arm(
        self, free_disk, fresh_arm, consume_arm
    ):
        result = run_once(
            config=replace(self.config, dry_run=False),
            state={"intents": {}, "dry_plans": []},
            logger=self.logger,
            now=self.now,
            market_gateway=FakeMarketGateway(self.market, blocked=True),
            chain=FakeChain(),
            status_gateway=FakeStatusGateway(),
            relayer_factory=lambda **kwargs: self.fail("relayer must not be constructed"),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "geoblock")
        consume_arm.assert_called_once()

    @patch("buy.runner.free_disk_mb", return_value=10_000)
    def test_ambiguous_intent_freezes_new_entry(self, free_disk):
        state = {
            "intents": {
                CONDITION: {
                    "condition_id": CONDITION,
                    "slug": self.market.slug,
                    "status": "submitting",
                    "created_at": self.now - 1,
                    "transaction_id": None,
                }
            },
            "dry_plans": [],
        }
        result = run_once(
            config=self.config,
            state=state,
            logger=self.logger,
            now=self.now,
            market_gateway=FakeMarketGateway(self.market),
            chain=FakeChain(),
            status_gateway=FakeStatusGateway(),
        )
        self.assertEqual(result, {"status": "blocked", "reason": "ambiguous_intent"})
        self.assertEqual(state["intents"][CONDITION]["status"], "ambiguous")

    def test_ambiguous_intent_counts_against_daily_notional(self):
        state = {
            "intents": {
                CONDITION: {
                    "status": "ambiguous",
                    "created_at": self.now - 60,
                    "submitted_at": 0.0,
                    "shares": 5.0,
                }
            }
        }
        self.assertEqual(_daily_notional(state, self.now), 5.0)

    def test_ambiguous_intent_recovers_only_from_complete_onchain_inventory(self):
        chain = FakeChain()
        chain.position_values = {"101": 5.0, "202": 5.0}
        intent = {
            "condition_id": CONDITION,
            "slug": self.market.slug,
            "status": "ambiguous",
            "transaction_id": None,
            "up_token": "101",
            "dn_token": "202",
            "before_up": 0.0,
            "before_dn": 0.0,
            "shares": 5.0,
            "end_ts": self.market.end_ts,
        }
        state = {"intents": {CONDITION: intent}, "dry_plans": []}
        reconcile_intents(
            config=self.config,
            state=state,
            funder_address=FUNDER,
            chain=chain,
            status_gateway=FakeStatusGateway(),
            logger=self.logger,
            now=self.now,
        )
        self.assertEqual(intent["status"], "confirmed")

    @patch("buy.runner.consume_arm")
    @patch("buy.runner.is_fresh_arm", return_value=True)
    @patch("buy.runner.free_disk_mb", return_value=10_000)
    @patch("buy.runner._credentials", return_value={
        "private_key": "",
        "funder_address": "",
        "builder_key": "",
        "builder_secret": "",
        "builder_passphrase": "",
    })
    def test_live_arm_is_consumed_before_failed_preflight(
        self, credentials, free_disk, fresh_arm, consume_arm
    ):
        with self.assertRaisesRegex(RuntimeError, "missing live credentials"):
            run_once(
                config=replace(self.config, dry_run=False),
                state={"intents": {}, "dry_plans": []},
                logger=self.logger,
                now=self.now,
                market_gateway=FakeMarketGateway(self.market),
                chain=FakeChain(),
                status_gateway=FakeStatusGateway(),
            )
        consume_arm.assert_called_once()

    @patch("buy.runner.notify")
    @patch("buy.runner.consume_arm")
    @patch("buy.runner.is_fresh_arm", return_value=True)
    @patch("buy.runner.free_disk_mb", return_value=10_000)
    @patch("buy.runner._credentials")
    @patch("buy.runner.save_state")
    def test_live_mint_persists_before_submit_and_uses_atomic_batch(
        self,
        save_state,
        credentials,
        free_disk,
        fresh_arm,
        consume_arm,
        notify,
    ):
        credentials.return_value = {
            "private_key": "0x" + "22" * 32,
            "funder_address": FUNDER,
            "builder_key": "builder-key",
            "builder_secret": "builder-secret",
            "builder_passphrase": "builder-passphrase",
        }
        snapshots = []
        save_state.side_effect = lambda state: snapshots.append(copy.deepcopy(state))
        relayer = FakeRelayer()
        state = {"intents": {}, "dry_plans": []}
        result = run_once(
            config=replace(self.config, dry_run=False),
            state=state,
            logger=self.logger,
            now=self.now,
            market_gateway=FakeMarketGateway(self.market),
            chain=FakeChain(),
            status_gateway=FakeStatusGateway(),
            relayer_factory=lambda **kwargs: relayer,
        )
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(snapshots[0]["intents"][CONDITION]["status"], "submitting")
        self.assertIsNone(snapshots[0]["intents"][CONDITION]["transaction_id"])
        self.assertEqual(snapshots[-1]["intents"][CONDITION]["status"], "pending")
        self.assertEqual(snapshots[-1]["intents"][CONDITION]["transaction_id"], "tx-123")
        self.assertEqual(len(relayer.calls), 2)
        self.assertIn(CONDITION, relayer.metadata)
        consume_arm.assert_called_once()


if __name__ == "__main__":
    unittest.main()
