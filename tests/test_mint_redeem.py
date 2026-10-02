"""Auto-redeem: job runner, calldata, chain reads. No mintbot import, no RPC."""

from __future__ import annotations

import ast
import json
import threading
import unittest
from pathlib import Path

from eth_abi import decode
from eth_utils import keccak

from buy.chain import ChainReader
from buy.contracts import (
    build_redeem_calls,
    encode_redeem_positions,
    encode_set_approval_for_all,
)
from buy.mint_redeem import (
    REDEEM_SLOW_POLL_S,
    RedeemDesk,
    RedeemIO,
    redeem_settings,
    retry_delay_s,
    sweep_jobs_from_positions,
    validate_redeem,
)

ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "mintbot.py"
MINT_EXAMPLE = ROOT / "strategy_mint.example.json"

CID = "0x" + "ab" * 32
CID2 = "0x" + "cd" * 32
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"
END = 1_800_000_000.0


def _cfg(**extra) -> dict:
    cfg = {
        "redeem_enabled": True,
        "dry_run": False,
        "redeem_poll_s": 15.0,
        "redeem_min_after_end_s": 60.0,
        "redeem_retry_s": 60.0,
        "redeem_max_attempts": 3,
        "redeem_tx_timeout_s": 300.0,
        "redeem_startup_sweep": False,
        "redeem_min_payout_usd": 0.01,
        "position_tolerance": 0.01,
    }
    cfg.update(extra)
    return cfg


def _bag(**extra) -> dict:
    row = {
        "status": "confirmed",
        "slug": "btc-updown-15m-1799999100",
        "start_ts": END - 900.0,
        "end_ts": END,
        "up_token": "111",
        "dn_token": "222",
        "shares": 200.0,
        "dry_run": False,
    }
    row.update(extra)
    return row


class FakeChain:
    """Balances per token, resolution per condition, relayer records per tx."""

    def __init__(self):
        self.balances = {"111": 0.0, "222": 100.0}
        self.den = {}
        self.nums = {}
        self.approved = True
        self.submits = []
        self.submit_results = []
        self.relayer = {}
        self.events = []
        self.notes = []
        self.rows = []
        self.positions_error = None
        self.reads = 0
        self.raise_on_balance = False

    def io(self) -> RedeemIO:
        return RedeemIO(
            payout_denominator=lambda cid: self.den.get(cid, 0),
            payout_numerator=lambda cid, i: self.nums.get(cid, [0, 0])[i],
            balance=self._balance,
            is_approved=lambda: self.approved,
            submit=self._submit,
            relayer_status=lambda tx: self.relayer.get(tx),
            log=lambda event, **kw: self.events.append({"event": event, **kw}),
            notify=lambda title, msg: self.notes.append((title, msg)),
            positions=self._positions,
        )

    def _balance(self, token):
        self.reads += 1
        if self.raise_on_balance:
            raise RuntimeError("rpc down")
        return self.balances.get(token, 0.0)

    def _submit(self, cid, approve):
        self.submits.append((cid, approve))
        if self.submit_results:
            return self.submit_results.pop(0)
        return (f"tx-{len(self.submits)}", None, {"gas_limit": 500_000, "gas_estimate": 430_000})

    def _positions(self):
        if self.positions_error:
            raise RuntimeError(self.positions_error)
        return list(self.rows)

    def named(self, name):
        return [row for row in self.events if row["event"] == name]

    def resolve(self, cid, nums):
        self.den[cid] = 1
        self.nums[cid] = list(nums)


def _desk(chain: FakeChain, saves: list | None = None) -> RedeemDesk:
    saves = saves if saves is not None else []
    return RedeemDesk(
        chain.io(),
        lock=threading.RLock(),
        save=lambda s: saves.append(json.loads(json.dumps(s))),
    )


class RedeemSettingsTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(redeem_settings({}).enabled)
        self.assertTrue(redeem_settings({}).dry_run)

    def test_validation(self):
        validate_redeem({})
        validate_redeem(_cfg())
        for key, bad in (
            ("redeem_poll_s", 0),
            ("redeem_retry_s", -1),
            ("redeem_tx_timeout_s", 0),
            ("redeem_min_after_end_s", -1),
            ("redeem_min_payout_usd", -0.5),
            ("redeem_max_attempts", 0),
            ("redeem_max_attempts", "x"),
        ):
            with self.assertRaises(ValueError, msg=key):
                validate_redeem(_cfg(**{key: bad}))

    def test_retry_backoff_doubles_and_caps(self):
        settings = redeem_settings(_cfg())
        self.assertEqual(retry_delay_s(settings, 1), 60.0)
        self.assertEqual(retry_delay_s(settings, 2), 120.0)
        self.assertEqual(retry_delay_s(settings, 3), 240.0)
        self.assertEqual(retry_delay_s(settings, 9), 900.0)

    def test_disabled_tick_does_no_io(self):
        chain = FakeChain()
        desk = _desk(chain)
        state = {"intents": {CID: _bag()}}
        self.assertEqual(desk.tick(state, _cfg(redeem_enabled=False), END + 600)["status"], "disabled")
        self.assertEqual(chain.reads, 0)
        self.assertEqual(chain.submits, [])
        self.assertNotIn("redeems", state)


class RedeemFlowTests(unittest.TestCase):
    def test_job_waits_for_end_plus_delay_and_skips_dry_failed_and_inflight(self):
        chain = FakeChain()
        desk = _desk(chain)
        state = {
            "intents": {
                CID: _bag(),
                "0x" + "01" * 32: _bag(dry_run=True, status="completed"),
                "0x" + "02" * 32: _bag(status="failed"),
                "0x" + "03" * 32: _bag(status="submitting"),
                "0x" + "04" * 32: _bag(end_ts=END + 900.0, start_ts=END),
            }
        }
        desk.tick(state, _cfg(), END + 59.0)
        self.assertEqual(state.get("redeems"), {})
        desk.tick(state, _cfg(), END + 60.0)
        self.assertEqual(list(state["redeems"]), [CID])
        self.assertEqual(state["redeems"][CID]["source"], "bag")

    def test_waits_for_resolution_then_redeems_and_completes_the_bag(self):
        chain = FakeChain()
        chain.approved = False
        saves: list = []
        desk = _desk(chain, saves)
        state = {"intents": {CID: _bag()}}
        now = END + 60.0
        desk.tick(state, _cfg(), now)
        job = state["redeems"][CID]
        self.assertEqual(job["status"], "waiting")
        self.assertEqual(chain.submits, [])
        self.assertEqual(len(chain.named("redeem_wait_resolution")), 1)

        now += 15.0
        desk.tick(state, _cfg(), now)
        self.assertEqual(len(chain.named("redeem_wait_resolution")), 1)

        chain.resolve(CID, [0, 1])
        now += 15.0
        desk.tick(state, _cfg(), now)
        job = state["redeems"][CID]
        self.assertEqual(job["status"], "submitted")
        self.assertEqual(job["tx_id"], "tx-1")
        self.assertEqual(job["attempts"], 1)
        self.assertAlmostEqual(job["payout_est"], 100.0)
        self.assertEqual(chain.submits, [(CID, True)])
        submitted = chain.named("redeem_submitted")[0]
        self.assertTrue(submitted["approve_adapter"])
        self.assertEqual(submitted["gas_limit"], 500_000)
        self.assertTrue(any(s["redeems"][CID]["status"] == "submitting" for s in saves))

        now += 15.0
        desk.tick(state, _cfg(), now)
        self.assertEqual(state["redeems"][CID]["status"], "submitted")

        chain.relayer["tx-1"] = {"state": "STATE_CONFIRMED", "transactionHash": "0xhash"}
        chain.balances["222"] = 0.0
        now += 15.0
        desk.tick(state, _cfg(), now)
        job = state["redeems"][CID]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["reason"], "redeemed")
        self.assertEqual(job["tx_hash"], "0xhash")
        intent = state["intents"][CID]
        self.assertEqual(intent["status"], "completed")
        self.assertTrue(intent["redeemed"])
        self.assertEqual(len(chain.named("redeem_confirmed")), 1)
        self.assertEqual(len(chain.notes), 1)

        for _ in range(3):
            now += 60.0
            desk.tick(state, _cfg(), now)
        self.assertEqual(len(chain.submits), 1)
        self.assertEqual(len(chain.named("redeem_confirmed")), 1)

    def test_approval_is_not_prepended_when_already_granted(self):
        chain = FakeChain()
        chain.resolve(CID, [0, 1])
        desk = _desk(chain)
        state = {"intents": {CID: _bag()}}
        desk.tick(state, _cfg(), END + 90.0)
        self.assertEqual(chain.submits, [(CID, False)])

    def test_fully_sold_bag_needs_two_zero_reads(self):
        chain = FakeChain()
        chain.balances = {"111": 0.0, "222": 0.0}
        desk = _desk(chain)
        state = {"intents": {CID: _bag(sold_winner=True, sold_loser=True)}}
        desk.tick(state, _cfg(), END + 60.0)
        self.assertEqual(state["redeems"][CID]["status"], "waiting")
        self.assertEqual(state["intents"][CID]["status"], "confirmed")
        desk.tick(state, _cfg(), END + 61.0)
        self.assertEqual(state["redeems"][CID]["status"], "waiting")
        desk.tick(state, _cfg(), END + 75.0)
        self.assertEqual(state["redeems"][CID]["status"], "done")
        self.assertEqual(state["redeems"][CID]["reason"], "nothing_held")
        self.assertEqual(state["intents"][CID]["status"], "completed")
        self.assertFalse(state["intents"][CID].get("redeemed"))
        self.assertEqual(chain.submits, [])

    def test_a_nonzero_read_resets_the_zero_count(self):
        chain = FakeChain()
        chain.balances = {"111": 0.0, "222": 0.0}
        desk = _desk(chain)
        state = {"intents": {CID: _bag()}}
        desk.tick(state, _cfg(), END + 60.0)
        chain.balances["222"] = 50.0
        desk.tick(state, _cfg(), END + 75.0)
        chain.balances["222"] = 0.0
        desk.tick(state, _cfg(), END + 90.0)
        self.assertEqual(state["redeems"][CID]["status"], "waiting")

    def test_losing_kept_leg_is_no_winner_and_sends_nothing(self):
        chain = FakeChain()
        chain.balances = {"111": 0.0, "222": 100.0}
        chain.resolve(CID, [1, 0])
        desk = _desk(chain)
        state = {"intents": {CID: _bag(sold_loser=True, sold_leg="up", sell_scrap_keep=100.0)}}
        desk.tick(state, _cfg(), END + 90.0)
        job = state["redeems"][CID]
        self.assertEqual(job["status"], "no_winner")
        self.assertEqual(chain.submits, [])
        self.assertEqual(state["intents"][CID]["status"], "completed")
        self.assertEqual(len(chain.named("redeem_no_winner")), 1)

    def test_kept_loser_that_won_and_unsold_winner_both_count(self):
        chain = FakeChain()
        chain.balances = {"111": 100.0, "222": 3.0}
        chain.resolve(CID, [1, 0])
        desk = _desk(chain)
        state = {"intents": {CID: _bag()}}
        desk.tick(state, _cfg(), END + 90.0)
        self.assertAlmostEqual(state["redeems"][CID]["payout_est"], 100.0)
        self.assertEqual(len(chain.submits), 1)

    def test_relayer_failure_retries_with_backoff_then_gives_up(self):
        chain = FakeChain()
        chain.resolve(CID, [0, 1])
        desk = _desk(chain)
        state = {"intents": {CID: _bag()}}
        now = END + 90.0
        desk.tick(state, _cfg(), now)
        chain.relayer["tx-1"] = {"state": "STATE_FAILED", "errorMsg": "execution reverted"}
        now += 15.0
        desk.tick(state, _cfg(), now)
        job = state["redeems"][CID]
        self.assertEqual(job["status"], "waiting")
        self.assertEqual(job["last_error"], "execution reverted")
        self.assertEqual(job["next_at"], now + 60.0)
        self.assertIsNone(job["tx_id"])

        desk.tick(state, _cfg(), now + 59.0)
        self.assertEqual(len(chain.submits), 1)
        now += 60.0
        desk.tick(state, _cfg(), now)
        self.assertEqual(len(chain.submits), 2)
        chain.relayer["tx-2"] = {"state": "STATE_INVALID"}
        now += 15.0
        desk.tick(state, _cfg(), now)
        self.assertEqual(state["redeems"][CID]["next_at"], now + 120.0)

        now += 120.0
        desk.tick(state, _cfg(), now)
        chain.relayer["tx-3"] = {"state": "STATE_FAILED", "errorMsg": "boom"}
        now += 15.0
        desk.tick(state, _cfg(), now)
        job = state["redeems"][CID]
        self.assertEqual(job["status"], "gave_up")
        self.assertEqual(len(chain.submits), 3)
        self.assertEqual(len(chain.named("redeem_submit_fail")), 3)
        self.assertEqual(len(chain.named("redeem_gave_up")), 1)
        self.assertTrue(state["intents"][CID]["redeem_gave_up"])
        self.assertEqual(state["intents"][CID]["status"], "confirmed")
        self.assertEqual(chain.notes[-1][0], "Redeem gave up")

        for _ in range(3):
            now += 900.0
            desk.tick(state, _cfg(), now)
        self.assertEqual(len(chain.submits), 3)

    def test_submit_error_without_tx_id_retries(self):
        chain = FakeChain()
        chain.resolve(CID, [0, 1])
        chain.submit_results = [(None, "HTTP 429 · rate", {"gas_limit": 650_000})]
        desk = _desk(chain)
        state = {"intents": {CID: _bag()}}
        now = END + 90.0
        desk.tick(state, _cfg(), now)
        job = state["redeems"][CID]
        self.assertEqual(job["status"], "waiting")
        self.assertEqual(job["attempts"], 1)
        fail = chain.named("redeem_submit_fail")[0]
        self.assertEqual(fail["error"], "HTTP 429 · rate")
        self.assertEqual(fail["gas_limit"], 650_000)
        desk.tick(state, _cfg(), now + 60.0)
        self.assertEqual(state["redeems"][CID]["status"], "submitted")

    def test_submit_exception_is_a_failed_attempt(self):
        chain = FakeChain()
        chain.resolve(CID, [0, 1])

        def boom(cid, approve):
            raise RuntimeError("socket closed")

        desk = _desk(chain)
        desk.io.submit = boom
        state = {"intents": {CID: _bag()}}
        desk.tick(state, _cfg(), END + 90.0)
        job = state["redeems"][CID]
        self.assertEqual(job["status"], "waiting")
        self.assertIn("socket closed", job["last_error"])

    def test_timeout_with_legs_cleared_is_done_and_with_legs_held_retries(self):
        chain = FakeChain()
        chain.resolve(CID, [0, 1])
        chain.resolve(CID2, [0, 1])
        desk = _desk(chain)
        state = {"intents": {CID: _bag(), CID2: _bag(up_token="333", dn_token="444")}}
        chain.balances.update({"333": 0.0, "444": 40.0})
        now = END + 90.0
        desk.tick(state, _cfg(), now)
        self.assertEqual(len(chain.submits), 1, "one submit per tick")
        desk.tick(state, _cfg(), now + 1.0)
        self.assertEqual(len(chain.submits), 2)
        chain.balances["222"] = 0.0
        now += 302.0
        desk.tick(state, _cfg(), now)
        self.assertEqual(state["redeems"][CID]["status"], "done")
        self.assertEqual(state["redeems"][CID2]["status"], "waiting")
        self.assertEqual(state["redeems"][CID2]["last_error"], "tx_timeout")

    def test_confirmed_but_not_cleared_waits_then_retries(self):
        chain = FakeChain()
        chain.resolve(CID, [0, 1])
        desk = _desk(chain)
        state = {"intents": {CID: _bag()}}
        now = END + 90.0
        desk.tick(state, _cfg(), now)
        chain.relayer["tx-1"] = {"state": "STATE_CONFIRMED"}
        desk.tick(state, _cfg(), now + 15.0)
        self.assertEqual(state["redeems"][CID]["status"], "submitted")
        desk.tick(state, _cfg(), now + 301.0)
        self.assertEqual(state["redeems"][CID]["last_error"], "confirmed_not_cleared")
        self.assertEqual(state["redeems"][CID]["status"], "waiting")

    def test_crash_while_submitting_recovers_after_timeout(self):
        chain = FakeChain()
        chain.resolve(CID, [0, 1])
        desk = _desk(chain)
        now = END + 90.0
        state = {
            "intents": {CID: _bag()},
            "redeems": {
                CID: {
                    "condition_id": CID,
                    "up_token": "111",
                    "dn_token": "222",
                    "status": "submitting",
                    "submitting_at": now,
                    "attempts": 1,
                    "created_at": now,
                    "next_at": now,
                    "tx_id": None,
                }
            },
        }
        desk.tick(state, _cfg(), now + 10.0)
        self.assertEqual(state["redeems"][CID]["status"], "submitting")
        self.assertEqual(chain.submits, [])
        desk.tick(state, _cfg(), now + 301.0)
        self.assertEqual(len(chain.named("redeem_submit_uncertain")), 1)
        self.assertEqual(state["redeems"][CID]["status"], "waiting")
        desk.tick(state, _cfg(), now + 302.0)
        self.assertEqual(len(chain.submits), 1)
        self.assertEqual(state["redeems"][CID]["attempts"], 2)

    def test_redeemed_elsewhere_after_uncertain_submit_is_done_without_resubmit(self):
        chain = FakeChain()
        chain.resolve(CID, [0, 1])
        desk = _desk(chain)
        state = {"intents": {CID: _bag()}}
        now = END + 90.0
        desk.tick(state, _cfg(), now)
        chain.balances = {"111": 0.0, "222": 0.0}
        now += 301.0
        desk.tick(state, _cfg(), now)
        self.assertEqual(state["redeems"][CID]["status"], "done")
        self.assertEqual(len(chain.submits), 1)

    def test_dry_run_logs_once_and_never_submits(self):
        chain = FakeChain()
        chain.resolve(CID, [0, 1])
        desk = _desk(chain)
        state = {"intents": {CID: _bag()}}
        now = END + 90.0
        desk.tick(state, _cfg(dry_run=True), now)
        job = state["redeems"][CID]
        self.assertEqual(job["status"], "waiting")
        self.assertEqual(job["next_at"], now + REDEEM_SLOW_POLL_S)
        desk.tick(state, _cfg(dry_run=True), now + REDEEM_SLOW_POLL_S)
        self.assertEqual(chain.submits, [])
        self.assertEqual(len(chain.named("redeem_dry_run")), 1)
        desk.tick(state, _cfg(), now + 2 * REDEEM_SLOW_POLL_S)
        self.assertEqual(len(chain.submits), 1)

    def test_rpc_error_is_logged_and_retried(self):
        chain = FakeChain()
        chain.raise_on_balance = True
        desk = _desk(chain)
        state = {"intents": {CID: _bag()}}
        desk.tick(state, _cfg(), END + 60.0)
        self.assertEqual(len(chain.named("redeem_check_fail")), 1)
        self.assertEqual(state["redeems"][CID]["next_at"], END + 75.0)
        chain.raise_on_balance = False
        chain.resolve(CID, [0, 1])
        desk.tick(state, _cfg(), END + 75.0)
        self.assertEqual(len(chain.submits), 1)

    def test_final_jobs_are_pruned_after_two_days_and_not_recreated(self):
        chain = FakeChain()
        chain.balances = {"111": 0.0, "222": 0.0}
        desk = _desk(chain)
        state = {"intents": {CID: _bag()}}
        desk.tick(state, _cfg(), END + 60.0)
        desk.tick(state, _cfg(), END + 75.0)
        self.assertEqual(state["redeems"][CID]["status"], "done")
        desk.tick(state, _cfg(), END + 3 * 86400.0)
        self.assertNotIn(CID, state["redeems"])
        self.assertEqual(state["intents"][CID]["status"], "completed")


class StartupSweepTests(unittest.TestCase):
    def _row(self, cid, asset, opposite, index, **extra):
        row = {
            "conditionId": cid,
            "asset": asset,
            "oppositeAsset": opposite,
            "outcomeIndex": index,
            "size": 12.5,
            "redeemable": True,
            "negativeRisk": False,
            "slug": "old-market",
        }
        row.update(extra)
        return row

    def test_rows_group_per_condition_and_map_up_to_outcome_zero(self):
        jobs, neg = sweep_jobs_from_positions(
            [
                self._row(CID, "222", "111", 1),
                self._row(CID, "111", "222", 0),
                self._row(CID2, "555", "666", 0, negativeRisk=True),
                self._row("0x" + "ee" * 32, "7", "8", 0, redeemable=False),
                self._row("0x" + "ff" * 32, "9", "10", 0, size=0),
                {"conditionId": "bad"},
            ]
        )
        self.assertEqual(neg, 1)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["condition_id"], CID)
        self.assertEqual(jobs[0]["up_token"], "111")
        self.assertEqual(jobs[0]["dn_token"], "222")

    def test_sweep_runs_once_and_redeems_leftovers(self):
        chain = FakeChain()
        chain.rows = [self._row(CID2, "333", "444", 0)]
        chain.balances.update({"333": 7.0, "444": 0.0})
        chain.resolve(CID2, [1, 0])
        desk = _desk(chain)
        state = {"intents": {}}
        cfg = _cfg(redeem_startup_sweep=True)
        desk.tick(state, cfg, END)
        self.assertEqual(state["redeems"][CID2]["source"], "startup")
        self.assertEqual(chain.submits, [(CID2, False)])
        sweep = chain.named("redeem_sweep")
        self.assertEqual(sweep[0]["added"], 1)
        chain.rows.append(self._row("0x" + "77" * 32, "9", "10", 0))
        desk.tick(state, cfg, END + 15.0)
        self.assertEqual(len(chain.named("redeem_sweep")), 1)
        self.assertEqual(len(state["redeems"]), 1)

    def test_sweep_failure_retries_later(self):
        chain = FakeChain()
        chain.positions_error = "HTTP 503"
        desk = _desk(chain)
        state = {"intents": {}}
        cfg = _cfg(redeem_startup_sweep=True)
        desk.tick(state, cfg, END)
        self.assertEqual(len(chain.named("redeem_sweep_fail")), 1)
        desk.tick(state, cfg, END + 60.0)
        self.assertEqual(len(chain.named("redeem_sweep_fail")), 1)
        chain.positions_error = None
        desk.tick(state, cfg, END + 300.0)
        self.assertEqual(len(chain.named("redeem_sweep")), 1)

    def test_sweep_skips_conditions_already_redeemed(self):
        chain = FakeChain()
        chain.rows = [self._row(CID, "111", "222", 0)]
        desk = _desk(chain)
        state = {"intents": {CID: _bag(status="completed", redeemed=True)}}
        desk.tick(state, _cfg(redeem_startup_sweep=True), END + 600.0)
        self.assertEqual(state["redeems"], {})


class RedeemCalldataTests(unittest.TestCase):
    def test_redeem_positions_matches_the_adapter_selector_and_args(self):
        data = encode_redeem_positions(collateral=PUSD, condition_id=CID)
        raw = bytes.fromhex(data[2:])
        self.assertEqual(raw[:4].hex(), "01b7037c")
        self.assertEqual(
            raw[:4], keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
        )
        collateral, parent, cid, index_sets = decode(
            ["address", "bytes32", "bytes32", "uint256[]"], raw[4:]
        )
        self.assertEqual(collateral.lower(), PUSD.lower())
        self.assertEqual(parent, bytes(32))
        self.assertEqual(cid, bytes.fromhex(CID[2:]))
        self.assertEqual(list(index_sets), [1, 2])

    def test_set_approval_for_all(self):
        raw = bytes.fromhex(encode_set_approval_for_all(ADAPTER)[2:])
        self.assertEqual(raw[:4].hex(), "a22cb465")
        operator, approved = decode(["address", "bool"], raw[4:])
        self.assertEqual(operator.lower(), ADAPTER.lower())
        self.assertTrue(approved)

    def test_build_redeem_calls_targets(self):
        calls = build_redeem_calls(
            pUSD_address=PUSD,
            adapter_address=ADAPTER,
            ctf_address=CTF,
            condition_id=CID,
            approve_adapter=False,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].to.lower(), ADAPTER.lower())
        with_approval = build_redeem_calls(
            pUSD_address=PUSD,
            adapter_address=ADAPTER,
            ctf_address=CTF,
            condition_id=CID,
            approve_adapter=True,
        )
        self.assertEqual([c.to.lower() for c in with_approval], [CTF.lower(), ADAPTER.lower()])
        self.assertTrue(with_approval[0].data.startswith("0xa22cb465"))
        self.assertTrue(with_approval[1].data.startswith("0x01b7037c"))

    def test_bad_condition_id_raises(self):
        with self.assertRaises(ValueError):
            encode_redeem_positions(collateral=PUSD, condition_id="0x1234")


class ChainReadTests(unittest.TestCase):
    def _reader(self, word: int):
        reader = ChainReader("http://127.0.0.1:9")
        calls = []

        def fake_rpc(method, params):
            calls.append((method, params))
            return "0x" + word.to_bytes(32, "big").hex()

        reader._rpc = fake_rpc
        return reader, calls

    def test_payout_reads_encode_the_ctf_views(self):
        reader, calls = self._reader(1)
        self.assertEqual(reader.payout_denominator(CTF, CID), 1)
        self.assertEqual(reader.payout_numerator(CTF, CID, 1), 1)
        self.assertTrue(reader.is_approved_for_all(CTF, PUSD, ADAPTER))
        selectors = [params[0]["data"][:10] for _method, params in calls]
        self.assertEqual(
            selectors,
            [
                "0x" + keccak(b"payoutDenominator(bytes32)")[:4].hex(),
                "0x" + keccak(b"payoutNumerators(bytes32,uint256)")[:4].hex(),
                "0x" + keccak(b"isApprovedForAll(address,address)")[:4].hex(),
            ],
        )
        self.assertTrue(calls[1][1][0]["data"].endswith("1".rjust(64, "0")))

    def test_unresolved_and_not_approved(self):
        reader, _calls = self._reader(0)
        self.assertEqual(reader.payout_denominator(CTF, CID), 0)
        self.assertFalse(reader.is_approved_for_all(CTF, PUSD, ADAPTER))


class RedeemWiringTests(unittest.TestCase):
    def _src(self, name: str) -> str:
        src = MINT.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(src, node) or ""
        raise AssertionError(name)

    def test_defaults_and_example_keep_redeem_off(self):
        tree = ast.parse(MINT.read_text(encoding="utf-8"))
        defaults = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "DEFAULTS" for t in node.targets)
        )
        example = json.loads(MINT_EXAMPLE.read_text(encoding="utf-8"))
        for blob in (defaults, example):
            self.assertIs(blob["redeem_enabled"], False)
            self.assertEqual(blob["redeem_poll_s"], 15.0)
            self.assertEqual(blob["redeem_min_after_end_s"], 60.0)
            self.assertEqual(blob["redeem_max_attempts"], 6)
            validate_redeem(blob)

    def test_redeem_runs_on_its_own_thread_and_shares_the_submit_lock(self):
        main = self._src("main")
        self.assertIn('name="mintbot-redeem"', main)
        self.assertIn("run_redeem_cycle", main)
        redeem = self._src("submit_redeem")
        self.assertIn("with RELAY_SUBMIT_LOCK:", redeem)
        self.assertIn("mintbot:redeem:", redeem)
        self.assertIn("build_redeem_calls", redeem)
        mint = self._src("run_mint_cycle")
        self.assertIn("with RELAY_SUBMIT_LOCK:", mint)
        for name in ("run_sell_cycle", "manage_sells", "_manage_sells_locked"):
            body = self._src(name)
            for token in ("submit_redeem", "RedeemDesk", "run_redeem_cycle", "RELAY_SUBMIT_LOCK"):
                self.assertNotIn(token, body, f"{name} contains {token}")

    def test_redeem_cycle_does_not_touch_the_heartbeat(self):
        self.assertNotIn("write_loop_heartbeat", self._src("run_redeem_cycle"))


if __name__ == "__main__":
    unittest.main()
