#!/usr/bin/env python3
"""Diagnostic: check why the hourly buy bot hasn't minted anything.

Run on the VM:  .venv-buy/bin/python check_hourly_mint.py
"""
import json
import os
import time
from datetime import datetime, timezone

import requests

GAMMA_URL = "https://gamma-api.polymarket.com"
SERIES_SLUG = "btc-up-or-down-hourly"
ARM_FILE = "buy_data_hourly/ARM"
ARM_PHRASE = "MINT_REAL_PUSD"
STATE_FILE = "buy_data_hourly/state.json"
CONFIG_FILE = "strategy.buy.hourly.json"

# Config values (read from file)
with open(CONFIG_FILE) as f:
    cfg = json.load(f)["buy"]

print("=" * 70)
print("HOURLY MINT DIAGNOSTIC")
print("=" * 70)

now = time.time()
now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
print(f"\nCurrent time: {now_iso}")
print(f"Unix timestamp: {now:.0f}")

# 1. Check ARM file
print("\n--- 1. ARM FILE ---")
arm_path = os.path.join(os.getcwd(), ARM_FILE)
if os.path.exists(arm_path):
    mtime = os.path.getmtime(arm_path)
    age_s = now - mtime
    with open(arm_path) as f:
        content = f.read().strip()
    armed = content == ARM_PHRASE
    arm_max_age = cfg.get("arm_max_age_s", 3600)
    fresh = armed and age_s < arm_max_age
    print(f"  Path: {arm_path}")
    print(f"  Content: '{content}' (expected '{ARM_PHRASE}')")
    print(f"  Armed: {armed}")
    print(f"  Age: {age_s:.0f}s (max {arm_max_age:.0f}s)")
    print(f"  Fresh: {fresh}")
else:
    print(f"  NOT FOUND at {arm_path}")
    print("  -> Bot is DISARMED. Cron hasn't fired yet or ARM was consumed.")

# 2. Check state file
print("\n--- 2. BUY BOT STATE ---")
state_path = os.path.join(os.getcwd(), STATE_FILE)
if os.path.exists(state_path):
    with open(state_path) as f:
        state = json.load(f)
    intents = state.get("intents", {})
    print(f"  State file: {state_path}")
    print(f"  Total intents: {len(intents)}")
    for cid, intent in intents.items():
        print(f"    {intent.get('slug')}: status={intent.get('status')}, shares={intent.get('shares')}")
    dry_plans = state.get("dry_plans", [])
    print(f"  Dry plans: {len(dry_plans)}")
else:
    print(f"  State file NOT FOUND at {state_path}")
    print("  -> Bot may not have started correctly.")

# 3. Query Gamma API for hourly markets
print("\n--- 3. GAMMA API: HOURLY MARKETS ---")
try:
    resp = requests.get(
        f"{GAMMA_URL}/events",
        params={
            "series_slug": SERIES_SLUG,
            "active": "true",
            "closed": "false",
            "limit": "80",
        },
        timeout=15,
    )
    resp.raise_for_status()
    events = resp.json()
    if not isinstance(events, list):
        events = [events]
    print(f"  Events found: {len(events)}")
except Exception as e:
    print(f"  ERROR querying Gamma API: {e}")
    events = []

# 4. Parse and show eligibility
print("\n--- 4. MARKET ELIGIBILITY ---")
enter_min = cfg.get("enter_min_ttm_min", 0.0)
enter_max = cfg.get("enter_max_ttm_min", 60.0)
max_open_sets = cfg.get("max_open_sets", 1)
max_open_notional = cfg.get("max_open_notional", 30.0)
shares = cfg.get("shares", 30.0)
one_entry = cfg.get("one_entry_per_market", True)

print(f"  Config: shares={shares}, max_open_sets={max_open_sets}, max_open_notional={max_open_notional}")
print(f"  Config: enter_min_ttm={enter_min}min, enter_max_ttm={enter_max}min")
print(f"  Config: one_entry_per_market={one_entry}")
print()

eligible = []
for event in events:
    if not isinstance(event, dict):
        continue
    event_slug = event.get("slug", "?")
    event_title = event.get("title", "?")
    for market in event.get("markets") or []:
        condition_id = market.get("conditionId") or market.get("condition_id")
        slug = market.get("slug") or event_slug
        end_date = market.get("endDate") or event.get("endDate")
        start_date = market.get("startDate") or event.get("startDate") or end_date
        active = str(market.get("active", event.get("active", False))).lower() in ("1", "true", "yes")
        closed = str(market.get("closed", event.get("closed", False))).lower() in ("1", "true", "yes")
        neg_risk = str(market.get("negRisk", event.get("negRisk", False))).lower() in ("1", "true", "yes")

        if not end_date or not condition_id:
            continue

        try:
            end_ts = datetime.fromisoformat(str(end_date).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue

        try:
            start_ts = datetime.fromisoformat(str(start_date).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            start_ts = end_ts

        mts = (start_ts - now) / 60.0  # minutes to start
        ttm = (end_ts - now) / 60.0    # minutes to maturity

        is_eligible = True
        reasons = []

        if not (enter_min <= mts <= enter_max):
            is_eligible = False
            reasons.append(f"mts={mts:.1f}min not in [{enter_min}, {enter_max}]")
        if not active:
            is_eligible = False
            reasons.append("not active")
        if closed:
            is_eligible = False
            reasons.append("closed")
        if neg_risk:
            is_eligible = False
            reasons.append("neg_risk")

        status = "ELIGIBLE" if is_eligible else "SKIP"
        start_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%H:%M UTC")
        end_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%H:%M UTC")

        print(f"  [{status}] {slug}")
        print(f"    Title: {event_title}")
        print(f"    Start: {start_iso} | End: {end_iso}")
        print(f"    mts={mts:.1f}min, ttm={ttm:.1f}min, active={active}, closed={closed}, neg_risk={neg_risk}")
        if reasons:
            print(f"    Skip reasons: {', '.join(reasons)}")
        print()

        if is_eligible:
            eligible.append((slug, condition_id, mts))

# 5. Summary
print("--- 5. SUMMARY ---")
print(f"  Eligible markets: {len(eligible)}")
if eligible:
    for slug, cid, mts in eligible:
        print(f"    {slug} (starts in {mts:.1f}min)")
else:
    print("  -> NO eligible markets found!")
    print("  -> This explains why no mints have happened.")

print()
print(f"  ARM file exists: {os.path.exists(arm_path)}")
print(f"  State file exists: {os.path.exists(state_path)}")

# 6. Check cron schedule
print("\n--- 6. CRON EXPECTATION ---")
print("  Cron arms at :56 of every hour")
print("  At :56, the next hourly market starts in ~4 minutes")
print(f"  enter_max_ttm_min={enter_max} means markets starting within {enter_max}min are eligible")
print(f"  At :56, mts for next market ≈ 4min, which is within [0, {enter_max}] -> should be eligible")
print()
print("  If ARM file is missing, the cron may not have fired yet since the bot started.")
print("  Check: crontab -l")
print("  Manually arm: echo 'MINT_REAL_PUSD' > buy_data_hourly/ARM")
